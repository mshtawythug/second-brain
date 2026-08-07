"""SQL helpers for ``brain review scan`` (Plan 03 — contradiction + staleness).

One reason to change: the shape of ``elicitation_gaps`` or the GraphRAG tables
this module reads (``graph_entities`` / ``graph_entity_mentions`` /
``documents`` / ``chunks``). All queries are parameterized and tenant-scoped
where a tenant column exists. The pure scan logic lives in :mod:`brain.review.scans`.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

# Review-finding signal kinds owned by this surface. ``brain review list`` /
# ``dismiss`` only ever touch these two; the other ``elicitation_gaps`` kinds
# (delta / orphan / contradiction-from-elicit / user_flagged) belong to
# ``brain elicit``.
REVIEW_SIGNAL_KINDS: tuple[str, ...] = ("contradiction", "stale")

# Document content types that are historical records, not living claims — never
# eligible as staleness candidates (a transcript of a past meeting does not go
# "stale" the way a compensation-range note does).
_STALE_EXCLUDED_CONTENT_TYPES: tuple[str, ...] = ("transcript", "krisp_action_items")


@dataclass(frozen=True)
class EntityCandidate:
    """One entity surviving the conflict-scan graph prefilter (Step 1)."""

    canonical_key: str
    name: str
    entity_type: str
    doc_ids: list[str]


@dataclass(frozen=True)
class StaleCandidate:
    """One aged document eligible for the staleness scan (Step 1)."""

    doc_id: str
    title: str
    age_days: int
    content_type: str


@dataclass(frozen=True)
class SupersedingDoc:
    """A newer document sharing an entity with a stale candidate (Step 2)."""

    doc_id: str
    title: str


@dataclass(frozen=True)
class QueueRow:
    """One row of the ``brain review list`` queue read-back."""

    id: str
    signal_kind: str
    target_type: str
    target_id: str
    score: float
    evidence_ids: list[str]
    rationale: str
    status: str


def iter_entities_for_conflict_scan(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    min_docs: int,
    limit: int,
) -> list[EntityCandidate]:
    """Graph prefilter: entities mentioned in >= ``min_docs`` summarized docs.

    Only non-draft documents with a non-null ``summary`` count toward the
    threshold (the summary is the evidence text fed to the LLM). Ordered by
    distinct-doc count DESC and capped at ``limit`` so the LLM-call budget is
    bounded. Pure SQL — no LLM, no embedder.
    """
    rows = conn.execute(
        """
        SELECT ge.canonical_key, ge.name, ge.entity_type,
               array_agg(DISTINCT gem.document_id::text) AS doc_ids
        FROM graph_entity_mentions gem
        JOIN graph_entities ge
          ON ge.id = gem.entity_id AND ge.tenant_id = gem.tenant_id
        JOIN documents d ON d.id = gem.document_id
        WHERE gem.tenant_id = %s
          AND d.summary IS NOT NULL
          AND d.draft IS NOT TRUE
        GROUP BY ge.id, ge.canonical_key, ge.name, ge.entity_type
        HAVING count(DISTINCT gem.document_id) >= %s
        ORDER BY count(DISTINCT gem.document_id) DESC
        LIMIT %s
        """,
        (tenant_id, min_docs, limit),
    ).fetchall()
    return [
        EntityCandidate(
            canonical_key=ckey,
            name=name,
            entity_type=etype,
            doc_ids=list(doc_ids),
        )
        for (ckey, name, etype, doc_ids) in rows
    ]


def count_conflict_docs_missing_summary(
    conn: psycopg.Connection[Any], *, tenant_id: str
) -> int:
    """Count non-draft docs referenced by an entity that still lack a summary.

    Surfaced by ``brain review scan --conflicts --dry-run`` to nudge the user
    toward ``brain enrich --backfill`` — these documents are silently excluded
    from the graph prefilter above.
    """
    row = conn.execute(
        """
        SELECT count(DISTINCT d.id)
        FROM graph_entity_mentions gem
        JOIN documents d ON d.id = gem.document_id
        WHERE gem.tenant_id = %s
          AND d.draft IS NOT TRUE
          AND d.summary IS NULL
        """,
        (tenant_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def iter_docs_for_staleness_scan(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    stale_age_days: int,
    limit: int,
) -> list[StaleCandidate]:
    """Age candidates: non-draft, summarized, non-transcript docs older than N days.

    ``documents`` has no tenant column, so the scan is tenant-scoped via an
    ``EXISTS`` on ``graph_entity_mentions``: only docs that participate in this
    tenant's entity graph are candidates. That is exactly the set that could
    ever yield a finding — the superseding-doc lookup (Step 2) requires a
    tenant-shared entity — so the ``EXISTS`` both enforces tenant isolation and
    skips docs that can never supersede. Oldest first; capped at ``limit``.
    """
    rows = conn.execute(
        """
        SELECT d.id::text, d.title,
               (now()::date - d.ingested_at::date) AS age_days,
               d.content_type
        FROM documents d
        WHERE d.ingested_at < now() - make_interval(days => %s)
          AND d.content_type <> ALL(%s)
          AND d.draft IS NOT TRUE
          AND d.summary IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM graph_entity_mentions gem
              WHERE gem.document_id = d.id AND gem.tenant_id = %s
          )
        ORDER BY d.ingested_at ASC
        LIMIT %s
        """,
        (stale_age_days, list(_STALE_EXCLUDED_CONTENT_TYPES), tenant_id, limit),
    ).fetchall()
    return [
        StaleCandidate(
            doc_id=doc_id,
            title=title,
            age_days=int(age_days),
            content_type=content_type,
        )
        for (doc_id, title, age_days, content_type) in rows
    ]


def count_stale_docs_missing_summary(
    conn: psycopg.Connection[Any], *, tenant_id: str, stale_age_days: int
) -> int:
    """Count aged non-draft, non-transcript docs that still lack a summary.

    Tenant-scoped via the same ``graph_entity_mentions`` ``EXISTS`` as
    :func:`iter_docs_for_staleness_scan`, so the
    ``brain review scan --stale --dry-run`` nudge counts only docs in the active
    tenant's graph that never enter the staleness pipeline for want of a summary.
    """
    row = conn.execute(
        """
        SELECT count(*)
        FROM documents d
        WHERE d.ingested_at < now() - make_interval(days => %s)
          AND d.content_type <> ALL(%s)
          AND d.draft IS NOT TRUE
          AND d.summary IS NULL
          AND EXISTS (
              SELECT 1 FROM graph_entity_mentions gem
              WHERE gem.document_id = d.id AND gem.tenant_id = %s
          )
        """,
        (stale_age_days, list(_STALE_EXCLUDED_CONTENT_TYPES), tenant_id),
    ).fetchone()
    return int(row[0]) if row else 0


@dataclass(frozen=True)
class StaleCandidateDiagnosis:
    """Why the staleness scan has no candidates — the three causes, separated.

    ``brain review scan`` printing ``No findings.`` is ambiguous between "your
    corpus is healthy" and "your corpus cannot produce a finding at all", and
    the two demand opposite responses. This makes them distinguishable.

    The counts narrow at each step, mirroring the order
    :func:`iter_docs_for_staleness_scan` applies its predicates, so the FIRST
    zero is the binding constraint.
    """

    aged: int
    """Docs past ``stale_age_days`` that are non-draft and not transcripts."""
    in_graph: int
    """Of :attr:`aged`, those with at least one entity mention in this tenant."""
    summarized: int
    """Of :attr:`in_graph`, those that also have a summary — the true candidates."""

    @property
    def reason(self) -> str | None:
        """Stable machine code for the binding constraint, or ``None``."""
        if self.aged == 0:
            return "no_aged_docs"
        if self.in_graph == 0:
            return "no_graph_entities"
        if self.summarized == 0:
            return "no_summaries"
        return None

    @property
    def hint(self) -> str | None:
        """One actionable line, or ``None`` when candidates genuinely exist.

        ``None`` is the important case: it means the scan really did run over a
        populated candidate set and found nothing stale, which is the only
        situation where ``No findings.`` should be read as good news.
        """
        return {
            "no_aged_docs": (
                "no documents are older than the staleness threshold yet — "
                "nothing to scan."
            ),
            "no_graph_entities": (
                "no aged documents are in the entity graph, so none can be "
                "matched against a superseding note. Run `brain graphrag build`."
            ),
            "no_summaries": (
                "aged documents are in the graph but none have a summary. "
                "Run `brain enrich --backfill`."
            ),
        }.get(self.reason or "")


def diagnose_stale_candidates(
    conn: psycopg.Connection[Any], *, tenant_id: str, stale_age_days: int
) -> StaleCandidateDiagnosis:
    """Explain an empty staleness candidate set in one round trip.

    Deliberately **not** gated on the ``graph_entity_mentions`` EXISTS the way
    :func:`count_stale_docs_missing_summary` is. That gating is why the existing
    missing-summary nudge is silent on the most common starting state — a corpus
    whose graph was never built has zero entity mentions, so a count scoped
    *through* those mentions is always zero and warns about nothing.

    Here each stage is counted independently so the empty stage itself is
    identifiable.
    """
    row = conn.execute(
        """
        WITH aged AS (
            SELECT d.id
            FROM documents d
            WHERE d.ingested_at < now() - make_interval(days => %s)
              AND d.content_type <> ALL(%s)
              AND d.draft IS NOT TRUE
        ),
        graphed AS (
            SELECT a.id FROM aged a
            WHERE EXISTS (
                SELECT 1 FROM graph_entity_mentions gem
                WHERE gem.document_id = a.id AND gem.tenant_id = %s
            )
        )
        SELECT (SELECT count(*) FROM aged),
               (SELECT count(*) FROM graphed),
               (SELECT count(*) FROM graphed g
                JOIN documents d ON d.id = g.id
                WHERE d.summary IS NOT NULL)
        """,
        (stale_age_days, list(_STALE_EXCLUDED_CONTENT_TYPES), tenant_id),
    ).fetchone()
    if row is None:  # pragma: no cover — an aggregate always returns one row
        return StaleCandidateDiagnosis(aged=0, in_graph=0, summarized=0)
    return StaleCandidateDiagnosis(
        aged=int(row[0]), in_graph=int(row[1]), summarized=int(row[2])
    )


def fetch_superseding_docs(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    doc_id: str,
    window_days: int,
) -> list[SupersedingDoc]:
    """Newer non-draft docs sharing >= 1 entity with ``doc_id``, ingested recently.

    A superseding candidate must (a) share at least one graph entity with the
    stale doc, (b) be ingested within ``window_days`` of now, and (c) not be the
    stale doc itself. Tenant-scoped via ``graph_entity_mentions``.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT d2.id::text, d2.title
        FROM graph_entity_mentions m1
        JOIN graph_entity_mentions m2
          ON m2.tenant_id = m1.tenant_id AND m2.entity_id = m1.entity_id
        JOIN documents d2 ON d2.id = m2.document_id
        WHERE m1.tenant_id = %s
          AND m1.document_id = %s
          AND m2.document_id <> %s
          AND d2.ingested_at >= now() - make_interval(days => %s)
          AND d2.draft IS NOT TRUE
        """,
        (tenant_id, doc_id, doc_id, window_days),
    ).fetchall()
    return [SupersedingDoc(doc_id=did, title=title) for (did, title) in rows]


def fetch_best_chunk_embeddings(
    conn: psycopg.Connection[Any], *, document_ids: Sequence[str]
) -> dict[str, list[float]]:
    """Map each document id to its lead-chunk embedding (``chunk_index`` 0).

    ``chunks`` has no ``created_at`` column, so the lead chunk is selected by
    ``ORDER BY document_id, chunk_index`` under ``DISTINCT ON (document_id)``.
    Documents whose chunks are all NULL-embedded are absent from the result.
    Vectors are returned as plain ``list[float]`` (the ``pgvector`` adapter
    yields a numpy array; we copy it out so the pure-Python cosine never depends
    on numpy).
    """
    if not document_ids:
        return {}
    rows = conn.execute(
        """
        SELECT DISTINCT ON (document_id) document_id::text, embedding
        FROM chunks
        WHERE document_id = ANY(%s::uuid[])
          AND embedding IS NOT NULL
        ORDER BY document_id, chunk_index
        """,
        (list(document_ids),),
    ).fetchall()
    return {doc_id: [float(x) for x in vec] for (doc_id, vec) in rows}


def fetch_doc_summaries(
    conn: psycopg.Connection[Any], *, document_ids: Sequence[str]
) -> dict[str, str]:
    """Map each document id to its non-null ``summary`` text.

    Documents with a NULL summary are omitted, so a caller can treat "absent"
    and "no summary" identically. Used to assemble the LLM evidence pair.
    """
    if not document_ids:
        return {}
    rows = conn.execute(
        """
        SELECT id::text, summary
        FROM documents
        WHERE id = ANY(%s::uuid[]) AND summary IS NOT NULL
        """,
        (list(document_ids),),
    ).fetchall()
    return {doc_id: summary for (doc_id, summary) in rows}


def existing_finding_statuses(
    conn: psycopg.Connection[Any], *, tenant_id: str, signal_kind: str
) -> dict[str, str]:
    """Map ``target_id`` -> ``status`` for every non-resolved finding of a kind.

    Backs the idempotency check (Step 2 of both scans): a target already
    surfaced / snoozed / dismissed is skipped (never re-adjudicated); only a
    ``resolved`` target (absent here) or a brand-new target is rescanned. The
    partial unique index on ``elicitation_gaps`` guarantees at most one
    non-resolved row per ``(tenant_id, signal_kind, target_id)``.
    """
    rows = conn.execute(
        """
        SELECT target_id, status
        FROM elicitation_gaps
        WHERE tenant_id = %s AND signal_kind = %s AND status <> 'resolved'
        """,
        (tenant_id, signal_kind),
    ).fetchall()
    return {target_id: status for (target_id, status) in rows}


def upsert_review_finding(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    signal_kind: str,
    target_type: str,
    target_id: str,
    score: float,
    evidence_ids: Sequence[str],
    rationale: str,
) -> None:
    """Upsert one finding, never overwriting a user-dismissed row.

    The partial unique index ``WHERE status <> 'resolved'`` covers dismissed
    rows, so a naive upsert would clobber them. The ``DO UPDATE ... WHERE
    elicitation_gaps.status IN ('surfaced','snoozed')`` guard detects the
    conflict (dismissed != resolved → in the index) but skips the UPDATE for a
    dismissed row, leaving the user's dismissal intact.
    """
    conn.execute(
        """
        INSERT INTO elicitation_gaps
            (tenant_id, signal_kind, target_type, target_id,
             score, evidence_ids, rationale)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, signal_kind, target_id) WHERE status <> 'resolved'
        DO UPDATE SET
            score        = EXCLUDED.score,
            evidence_ids = EXCLUDED.evidence_ids,
            rationale    = EXCLUDED.rationale,
            updated_at   = now()
        WHERE elicitation_gaps.status IN ('surfaced', 'snoozed')
        """,
        (
            tenant_id,
            signal_kind,
            target_type,
            target_id,
            score,
            list(evidence_ids),
            rationale,
        ),
    )


def list_review_queue(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    signal_kinds: Sequence[str],
    limit: int,
    include_snoozed: bool = False,
) -> list[QueueRow]:
    """Read the open review queue (surfaced / snoozed) for the given kinds.

    Scoped to ``signal_kinds`` (a subset of :data:`REVIEW_SIGNAL_KINDS`) so the
    review queue stays independent of the ``brain elicit`` gap queue. Ordered by
    score DESC. Snoozed rows whose ``snoozed_until`` is still in the future are
    held back, mirroring ``brain elicit list``.

    ``include_snoozed=True`` lifts that hold-back so a still-snoozed finding can
    be seen again (``brain review list --include-snoozed``). Without it a snooze
    is a one-way door: the row is invisible until its deadline passes and there
    is no way to review or reverse the decision in the meantime.

    **There is deliberately no un-snooze verb.** With this flag the row is
    visible, and ``brain review resolve <id>`` on it is an adequate escape
    hatch; snoozes additionally self-heal on expiry. A third verb would earn its
    keep only if someone asks for it — recorded here so the absence reads as a
    decision rather than an oversight.
    """
    snooze_clause = (
        "" if include_snoozed else "AND (snoozed_until IS NULL OR snoozed_until < now())"
    )
    rows = conn.execute(
        f"""
        SELECT id::text, signal_kind, target_type, target_id,
               score, evidence_ids, rationale, status
        FROM elicitation_gaps
        WHERE tenant_id = %s
          AND signal_kind = ANY(%s)
          AND status IN ('surfaced', 'snoozed')
          {snooze_clause}
        ORDER BY score DESC
        LIMIT %s
        """,  # noqa: S608 — snooze_clause is a module constant, never user input
        (tenant_id, list(signal_kinds), limit),
    ).fetchall()
    return [
        QueueRow(
            id=r[0],
            signal_kind=r[1],
            target_type=r[2],
            target_id=r[3],
            score=float(r[4]),
            evidence_ids=list(r[5]),
            rationale=r[6],
            status=r[7],
        )
        for r in rows
    ]


def _resolve_finding_prefix(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    id_prefix: str,
    include_resolved: bool = False,
) -> str:
    """Resolve ``id_prefix`` to exactly one review finding id.

    Shared by every ``brain review`` status writer (dismiss / snooze / resolve),
    all of which address a finding by the short prefix ``brain review list``
    prints. Scoped to :data:`REVIEW_SIGNAL_KINDS`, so the ``brain elicit`` gap
    kinds living in the same table are unreachable from this surface.

    ``include_resolved`` widens the lookup to closed findings; only
    :func:`resolve_review_finding` passes it, so re-resolving is idempotent
    while snoozing an already-closed finding stays an error. Raises
    :class:`ValueError` when the prefix matches nothing or is ambiguous — the
    CLI / MCP layer maps that to a user-facing error.
    """
    matches = conn.execute(
        """
        SELECT id::text
        FROM elicitation_gaps
        WHERE tenant_id = %s
          AND signal_kind = ANY(%s)
          AND (%s OR status <> 'resolved')
          AND id::text LIKE %s
        LIMIT 2
        """,
        (tenant_id, list(REVIEW_SIGNAL_KINDS), include_resolved, f"{id_prefix}%"),
    ).fetchall()
    if not matches:
        raise ValueError(f"no review finding matches prefix {id_prefix!r}")
    if len(matches) > 1:
        raise ValueError(f"review finding prefix {id_prefix!r} is ambiguous")
    return str(matches[0][0])


def dismiss_review_finding(
    conn: psycopg.Connection[Any], *, tenant_id: str, id_prefix: str
) -> str:
    """Set ``status='dismissed'`` on the review finding matching ``id_prefix``.

    Resolves ``id_prefix`` against open / dismissed review findings only
    (``signal_kind`` in :data:`REVIEW_SIGNAL_KINDS`, ``status <> 'resolved'``).
    Idempotent: dismissing an already-dismissed finding is a no-op that returns
    its id. Returns the full finding id. Raises :class:`ValueError` when the
    prefix matches no finding or is ambiguous — the CLI / MCP layer maps that to
    a user-facing error.
    """
    finding_id = _resolve_finding_prefix(conn, tenant_id=tenant_id, id_prefix=id_prefix)
    conn.execute(
        """
        UPDATE elicitation_gaps
        SET status = 'dismissed', updated_at = now()
        WHERE id = %s::uuid
        """,
        (finding_id,),
    )
    return finding_id


def snooze_review_finding(
    conn: psycopg.Connection[Any], *, tenant_id: str, id_prefix: str, days: int
) -> str:
    """Set ``status='snoozed'`` and push ``snoozed_until`` out by ``days``.

    The missing writer for a state :func:`list_review_queue` has always read: a
    snoozed finding drops out of the open queue and comes back on its own once
    ``snoozed_until`` passes — no second command required. The deadline is
    computed **in SQL** (``now() + make_interval``), mirroring
    ``brain.elicit.session._snooze``, so the database clock is the single
    authority and no app/DB skew can land a snooze in the past.

    A resolved finding is closed and cannot be snoozed. Returns the full finding
    id; raises :class:`ValueError` for ``days < 1`` and for a prefix matching
    nothing or more than one finding.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    finding_id = _resolve_finding_prefix(conn, tenant_id=tenant_id, id_prefix=id_prefix)
    conn.execute(
        """
        UPDATE elicitation_gaps
        SET status = 'snoozed',
            snoozed_until = now() + make_interval(days => %s),
            updated_at = now()
        WHERE id = %s::uuid
        """,
        (days, finding_id),
    )
    return finding_id


def resolve_review_finding(
    conn: psycopg.Connection[Any], *, tenant_id: str, id_prefix: str
) -> str:
    """Set ``status='resolved'`` on the review finding matching ``id_prefix``.

    "Resolved" means the user acted on the finding — unlike ``dismissed``, which
    means it was noise. Resolving also releases the row from the partial unique
    index ``WHERE status <> 'resolved'``, so a later scan may legitimately
    re-surface the same target if it goes stale or conflicting again.

    Idempotent: the lookup includes already-resolved findings, so re-resolving
    returns the same id rather than failing with "no review finding".
    """
    finding_id = _resolve_finding_prefix(
        conn, tenant_id=tenant_id, id_prefix=id_prefix, include_resolved=True
    )
    conn.execute(
        """
        UPDATE elicitation_gaps
        SET status = 'resolved', updated_at = now()
        WHERE id = %s::uuid
        """,
        (finding_id,),
    )
    return finding_id
