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
) -> list[QueueRow]:
    """Read the open review queue (surfaced / snoozed) for the given kinds.

    Scoped to ``signal_kinds`` (a subset of :data:`REVIEW_SIGNAL_KINDS`) so the
    review queue stays independent of the ``brain elicit`` gap queue. Ordered by
    score DESC. Snoozed rows whose ``snoozed_until`` is still in the future are
    held back, mirroring ``brain elicit list``.
    """
    rows = conn.execute(
        """
        SELECT id::text, signal_kind, target_type, target_id,
               score, evidence_ids, rationale, status
        FROM elicitation_gaps
        WHERE tenant_id = %s
          AND signal_kind = ANY(%s)
          AND status IN ('surfaced', 'snoozed')
          AND (snoozed_until IS NULL OR snoozed_until < now())
        ORDER BY score DESC
        LIMIT %s
        """,
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
    matches = conn.execute(
        """
        SELECT id::text
        FROM elicitation_gaps
        WHERE tenant_id = %s
          AND signal_kind = ANY(%s)
          AND status <> 'resolved'
          AND id::text LIKE %s
        LIMIT 2
        """,
        (tenant_id, list(REVIEW_SIGNAL_KINDS), f"{id_prefix}%"),
    ).fetchall()
    if not matches:
        raise ValueError(f"no review finding matches prefix {id_prefix!r}")
    if len(matches) > 1:
        raise ValueError(f"review finding prefix {id_prefix!r} is ambiguous")
    finding_id = str(matches[0][0])
    conn.execute(
        """
        UPDATE elicitation_gaps
        SET status = 'dismissed', updated_at = now()
        WHERE id = %s::uuid
        """,
        (finding_id,),
    )
    return finding_id
