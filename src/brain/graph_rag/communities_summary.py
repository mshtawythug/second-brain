"""Eager community summaries + embeddings (wave G3-c, spec §17c Q10).

The second half of ``brain graphrag communities build|refresh`` (G3-f wires it in
after :func:`brain.graph_rag.communities.build_communities`). G3-b detects +
persists the tenant's communities (memberships, stats, the per-community
``members_hash`` identity); THIS module gives each community a natural-language
``summary`` and a ``summary_embedding`` so the global-retrieval RRF (§17c Q4/Q5)
has an FTS leg and a vector leg to rank over.

**EAGER, not per-query (§17c Q10).** "Lazy embedded summaries" in §4 D5 means
deferred relative to *ingest* — batched at community build/refresh — NOT lazy
relative to the query. Query time NEVER calls Ollama for a community summary; it
only embeds the *query* and degrades to FTS-only when ``summary_embedding`` is
NULL. So summaries/embeddings are produced here, ahead of any query.

**Best-effort, never-raise (§17c Q10 / §7).** Summary + embedding generation is a
hard live-Ollama dependency we refuse to let break the build:

* ``enricher.summarize_group(...) -> None`` (Ollama down / timeout / invalid /
  empty) → the community's summary fields are left NULL and
  ``summary_members_hash`` is NOT set, so the community stays a candidate and is
  retried on the next run. The build still succeeds.
* An embedding failure (Ollama down, dim mismatch, …) → ``summary_embedding`` is
  left NULL (the global path degrades that community to FTS-only) while the
  ``summary`` text is still written. Retried next run.
* ``enricher`` is ``None`` → the whole pass is a logged no-op
  (``skipped=True``): summaries cannot be produced without it. ``embedder`` is
  ``None`` (but ``enricher`` present) → summaries are STILL written and only the
  embedding phase is skipped (``summary_embedding`` left NULL → the global path
  degrades that community to FTS-only; ``skipped=False``). The summary and
  embedding phases are decoupled (§17c Q10): a broken/missing embedder must NOT
  block summaries. Production injects
  :func:`brain.enrichment.make_enricher` / :func:`brain.embeddings.make_embedder`
  via G3-f; tests inject fakes. Either way the call never raises (an empty
  ``tenant`` is the one exception — a caller bug, mirroring
  :func:`~brain.graph_rag.communities.build_communities`).

**Staleness predicate (§17c Q3/Q10).** A community NEEDS a (re)summary when
``summary IS NULL OR summary_members_hash IS DISTINCT FROM members_hash``: a
never-summarized community, or one whose membership changed since its last
summary. The G3-b delta-gate moves ``members_hash`` on a membership change while
PRESERVING the old summary, and migration 014's ``summary_members_hash`` records
which membership the live summary was built from — so staleness is detectable
WITHOUT ever blanking the live summary (it stays queryable until a fresh one
replaces it). ``IS DISTINCT FROM`` makes the predicate NULL-safe.

**Idempotent.** A second run with no membership change is a no-op: every
community already has ``summary_members_hash == members_hash`` (excluded from the
summary candidates) and a non-NULL ``summary_embedding`` (excluded from the embed
candidates). Mirrors ``brain enrich --backfill``'s NULL-only idempotency.

**Tenant-scoped + DRY embeddings.** Every read/write carries ``tenant_id``. The
embedding leg reuses the generalized dim-reconciliation machinery
(:func:`brain.db.ensure_embedding_column` over the
``('graph_communities','summary_embedding')`` allowlist entry) rather than
hand-rolling embedding DDL/SQL — exactly as ``brain reembed`` does for
``chunks.embedding``. ``summary_embedding`` stays NULLABLE with no HNSW (small
community counts → sequential cosine scan, spec §5; the global path guards on
``IS NOT NULL``), so there is nothing to ``finalize_embedding_index`` — a
NOT NULL constraint would break the best-effort contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from ..config import Config
from ..db import ensure_embedding_column
from ..errors import GraphTenantError
from ..ingest import Embedder

__all__ = [
    "CommunitySummaryResult",
    "summarize_communities",
]

_logger = logging.getLogger(__name__)

# Prompt-budget caps. A community can have many members / documents; the summary
# prompt only needs the most-central entities + the most-mentioning documents to
# characterize the cluster. Capping keeps the Ollama prompt bounded regardless of
# community size (the ops budget on TOTAL communities is the §17c Q8
# graph_community_max cap applied at detection time).
_SUMMARY_ENTITY_LIMIT = 20
_SUMMARY_DOC_LIMIT = 10


class _CommunitySummarizer(Protocol):
    """Structural type for the injected summary backend (DI seam).

    The summary pass depends only on ``summarize_group`` (best-effort,
    never-raises — returns ``None`` + WARN on Ollama failure) and the ``model``
    fingerprint recorded onto ``graph_communities.summary_model``. Production
    injects a :class:`brain.enrichment.OllamaEnricher`; tests inject a fake.
    Neither is imported here, mirroring :class:`brain.graph_rag.themes.
    _GroupSummarizer`.
    """

    @property
    def model(self) -> str: ...

    def summarize_group(
        self,
        *,
        person: str | None,
        entity_names: list[str],
        doc_titles: list[str],
    ) -> str | None: ...


@dataclass(frozen=True)
class CommunitySummaryResult:
    """Tally of a :func:`summarize_communities` run.

    ``candidates`` is the number of communities found NEEDING a summary this run
    (after the optional ``limit`` cap). ``summarized`` counts summaries actually
    written; ``summary_failures`` counts candidates where the enricher returned
    ``None`` / raised (left NULL, retried next run) — so
    ``summarized + summary_failures == candidates``. ``embedded`` counts
    ``summary_embedding`` vectors written; ``embed_failures`` counts communities
    that had a summary but whose embedding step failed (left NULL, retried next
    run). ``skipped`` is True only when the whole pass was a no-op because
    ``enricher`` was ``None`` (no summaries possible). A ``None`` ``embedder``
    does NOT set ``skipped`` — summaries are still written and only the embedding
    phase is skipped (``embedded == 0``, ``summary_embedding`` left NULL).
    """

    tenant_id: str
    candidates: int = 0
    summarized: int = 0
    summary_failures: int = 0
    embedded: int = 0
    embed_failures: int = 0
    skipped: bool = False


def summarize_communities(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    tenant: str,
    enricher: _CommunitySummarizer | None = None,
    embedder: Embedder | None = None,
    limit: int | None = None,
) -> CommunitySummaryResult:
    """Eagerly (re)summarize + embed the tenant's stale/new communities (G3-c).

    Two best-effort phases over the communities NEEDING a summary
    (``summary IS NULL OR summary_members_hash IS DISTINCT FROM members_hash``),
    ordered by ``community_key`` for determinism and capped by ``limit`` when
    given:

    1. **Summaries.** For each candidate, gather its representative entity
       display-names (top members by ``member_rank``) + representative document
       titles (docs whose mentions include the community's entities), then call
       ``enricher.summarize_group(person=None, ...)``. On a non-``None`` summary,
       write ``summary`` / ``summary_model`` / ``summary_at`` /
       ``summary_members_hash = members_hash`` and reset ``summary_embedding`` to
       NULL (it must be re-embedded from the fresh text). On ``None`` (Ollama
       failure), leave everything NULL and do NOT set ``summary_members_hash`` —
       the community stays a candidate and is retried next run.
    2. **Embeddings.** Reconcile ``graph_communities.summary_embedding`` to the
       active embedder's dim via :func:`brain.db.ensure_embedding_column`, then
       embed every community with ``summary IS NOT NULL AND summary_embedding IS
       NULL`` (the freshly-summarized ones plus any whose prior embed failed).
       Best-effort: any failure leaves those embeddings NULL (the global path
       degrades to FTS-only) and the build still succeeds.

    ``enricher`` / ``embedder`` are injected (production: ``make_enricher(cfg)`` /
    ``make_embedder(cfg)``; tests: fakes). A ``None`` ``enricher`` skips the whole
    pass (``skipped=True`` — no summaries possible); a ``None`` ``embedder`` (with
    the enricher present) still writes summaries and only skips the embedding
    phase (``summary_embedding`` stays NULL, ``skipped=False``) — the two phases
    are decoupled so a broken/missing embedder never blocks summaries. Never
    raises on an Ollama / embedding failure; an empty ``tenant`` is a caller bug
    and raises :class:`brain.errors.GraphTenantError` before any DB work (mirrors
    :func:`brain.graph_rag.communities.build_communities`).

    ``limit`` caps how many stale/new communities are (re)summarized this run;
    when ``None`` it falls back to ``cfg.graph_community_max`` (the §17c Q8 ops
    cap, itself ``None`` == unlimited by default). The universe is already
    bounded because detection (G3-b) materializes at most
    ``graph_community_max`` communities, so this fallback is a behavior-neutral
    safety net rather than a second independent cap.
    """
    if not tenant:
        raise GraphTenantError(
            "summarize_communities requires a non-empty tenant_id "
            "(resolve via brain.graph_rag.tenancy.resolve_tenant first)"
        )

    effective_limit = limit if limit is not None else cfg.graph_community_max

    if enricher is None:
        _logger.warning(
            "summarize_communities: enricher is None — skipping the whole "
            "community summary/embedding pass (cannot summarize without an "
            "enricher; best-effort no-op, no fields written)"
        )
        return CommunitySummaryResult(tenant_id=tenant, skipped=True)

    summarized, summary_failures, candidates = _run_summary_phase(
        conn, tenant=tenant, enricher=enricher, limit=effective_limit
    )

    if embedder is None:
        # Decoupled phases (§17c Q10): a missing/unavailable embedder must NOT
        # block summaries. The summary text/model/at + summary_members_hash are
        # already written above; only the embedding phase is skipped, leaving
        # summary_embedding NULL (the global path degrades that community to
        # FTS-only) — re-embedded on the next run once an embedder is available.
        _logger.warning(
            "summarize_communities: embedder is None — summaries written but "
            "the embedding phase was skipped (summary_embedding left NULL; "
            "global retrieval degrades to FTS-only; retried next run)"
        )
        return CommunitySummaryResult(
            tenant_id=tenant,
            candidates=candidates,
            summarized=summarized,
            summary_failures=summary_failures,
            embedded=0,
            embed_failures=0,
            skipped=False,
        )

    embedded, embed_failures = _run_embedding_phase(
        conn, tenant=tenant, embedder=embedder
    )
    return CommunitySummaryResult(
        tenant_id=tenant,
        candidates=candidates,
        summarized=summarized,
        summary_failures=summary_failures,
        embedded=embedded,
        embed_failures=embed_failures,
        skipped=False,
    )


# --------------------------------------------------------------------------- #
# Phase 1 — summaries (best-effort, never-raise).
# --------------------------------------------------------------------------- #
def _run_summary_phase(
    conn: psycopg.Connection[Any],
    *,
    tenant: str,
    enricher: _CommunitySummarizer,
    limit: int | None,
) -> tuple[int, int, int]:
    """Generate + persist summaries for stale/new communities.

    Returns ``(summarized, summary_failures, candidates)``. The Ollama calls run
    OUTSIDE any open DB transaction (so a slow model never holds a write lock);
    successes are then written in a single transaction. Each
    ``summarize_group`` call is wrapped in a defence-in-depth ``try/except``
    (it already returns ``None`` on failure; the guard covers a misbehaving
    injected fake) so the phase never raises.
    """
    candidate_rows = _read_summary_candidates(conn, tenant=tenant, limit=limit)
    if not candidate_rows:
        return 0, 0, 0

    # (community_key, summary_text, model, members_hash) for each success.
    writes: list[tuple[str, str, str, str]] = []
    summary_failures = 0
    for community_key, members_hash in candidate_rows:
        entity_names = _representative_entities(conn, tenant=tenant, key=community_key)
        doc_titles = _representative_doc_titles(conn, tenant=tenant, key=community_key)
        try:
            summary = enricher.summarize_group(
                person=None, entity_names=entity_names, doc_titles=doc_titles
            )
        except Exception as exc:  # noqa: BLE001 — best-effort: never fail the build
            _logger.warning(
                "summarize_communities: summary for community %s failed (%s); "
                "leaving NULL (retried next run)",
                community_key,
                exc,
            )
            summary = None
        if summary is None:
            summary_failures += 1
            continue
        writes.append((community_key, summary, enricher.model, members_hash))

    if writes:
        with conn.transaction():
            for community_key, summary, model, members_hash in writes:
                conn.execute(
                    "UPDATE graph_communities SET "
                    "summary = %s, summary_model = %s, summary_at = NOW(), "
                    "summary_members_hash = %s, summary_embedding = NULL "
                    "WHERE tenant_id = %s AND community_key = %s",
                    (summary, model, members_hash, tenant, community_key),
                )
    return len(writes), summary_failures, len(candidate_rows)


def _read_summary_candidates(
    conn: psycopg.Connection[Any], *, tenant: str, limit: int | None
) -> list[tuple[str, str]]:
    """Return ``(community_key, members_hash)`` for communities needing a summary.

    Staleness predicate (§17c Q3/Q10): ``summary IS NULL`` (never summarized) OR
    ``summary_members_hash IS DISTINCT FROM members_hash`` (membership changed
    since the last summary). Ordered by ``community_key`` for deterministic,
    resumable processing; capped by ``limit`` when given (``None`` == all
    candidates — the universe is already bounded by the §17c Q8
    ``graph_community_max`` materialization cap applied at detection).
    """
    base = (
        "SELECT community_key::text, members_hash FROM graph_communities "
        "WHERE tenant_id = %s "
        "AND (summary IS NULL OR summary_members_hash IS DISTINCT FROM members_hash) "
        "ORDER BY community_key"
    )
    if limit is not None:
        rows = conn.execute(base + " LIMIT %s", (tenant, limit)).fetchall()
    else:
        rows = conn.execute(base, (tenant,)).fetchall()
    return [(str(key), str(members_hash)) for key, members_hash in rows]


def _representative_entities(
    conn: psycopg.Connection[Any], *, tenant: str, key: str
) -> list[str]:
    """Top member entity display-names for the summary prompt (by ``member_rank``).

    ``member_rank`` is 0-based most-central-first (G3-b ranks by weighted degree),
    so ``ORDER BY member_rank`` surfaces the cluster's hub entities. Capped at
    :data:`_SUMMARY_ENTITY_LIMIT` to keep the prompt bounded.
    """
    rows = conn.execute(
        "SELECT ge.name FROM graph_community_members cm "
        "JOIN graph_entities ge "
        "  ON ge.tenant_id = cm.tenant_id AND ge.id = cm.entity_id "
        "WHERE cm.tenant_id = %s AND cm.community_key = %s "
        "ORDER BY cm.member_rank ASC, ge.name ASC "
        "LIMIT %s",
        (tenant, key, _SUMMARY_ENTITY_LIMIT),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _representative_doc_titles(
    conn: psycopg.Connection[Any], *, tenant: str, key: str
) -> list[str]:
    """Titles of the documents that most mention the community's entities.

    Joins ``graph_community_members`` → ``graph_entity_mentions`` →
    ``documents`` (all tenant-scoped) and ranks documents by how many of the
    community's entities they mention (``COUNT(*)`` desc, title asc for a
    deterministic tie-break). Capped at :data:`_SUMMARY_DOC_LIMIT`. No document
    BODY is read — only titles feed the prompt (mirrors
    :func:`brain.graph_rag.themes._fetch_doc_titles`).
    """
    rows = conn.execute(
        "SELECT d.title, COUNT(*) AS n FROM graph_community_members cm "
        "JOIN graph_entity_mentions m "
        "  ON m.tenant_id = cm.tenant_id AND m.entity_id = cm.entity_id "
        "JOIN documents d ON d.id = m.document_id "
        "WHERE cm.tenant_id = %s AND cm.community_key = %s "
        "GROUP BY d.id, d.title "
        "ORDER BY n DESC, d.title ASC "
        "LIMIT %s",
        (tenant, key, _SUMMARY_DOC_LIMIT),
    ).fetchall()
    return [str(row[0]) for row in rows]


# --------------------------------------------------------------------------- #
# Phase 2 — embeddings (best-effort, never-raise; reuses the dim machinery).
# --------------------------------------------------------------------------- #
def _run_embedding_phase(
    conn: psycopg.Connection[Any],
    *,
    tenant: str,
    embedder: Embedder,
) -> tuple[int, int]:
    """Embed every summary that lacks a ``summary_embedding``. Best-effort.

    Returns ``(embedded, embed_failures)``. Reconciles the
    ``graph_communities.summary_embedding`` dim to the active embedder via
    :func:`brain.db.ensure_embedding_column` (the same generalized machinery
    ``brain reembed`` uses for ``chunks.embedding``), reads the tenant's
    ``summary IS NOT NULL AND summary_embedding IS NULL`` rows, embeds their
    summary text (``input_type="document"``), and writes the vectors in one
    transaction. Any failure (Ollama down, dim mismatch with populated
    embeddings, transport error) is caught: a WARN is logged, the affected
    embeddings stay NULL (the global path degrades to FTS-only for those
    communities), and the build still succeeds — nothing re-raises.
    """
    pending: list[tuple[str, str]] = []
    try:
        # Reconcile dim FIRST so the column matches the active backend before any
        # vector is bound. On a fresh DB with no populated summary embeddings this
        # is a cheap no-op (matching dim) or a safe drop+re-add (dim change, zero
        # rows to lose); with populated embeddings at a different dim it raises —
        # caught below and surfaced as a WARN (a destructive backend swap, not an
        # Ollama hiccup, but still best-effort so the build never breaks).
        ensure_embedding_column(
            conn, embedder, "graph_communities", "summary_embedding"
        )
        pending = conn.execute(
            "SELECT community_key::text, summary FROM graph_communities "
            "WHERE tenant_id = %s "
            "AND summary IS NOT NULL AND summary_embedding IS NULL "
            "ORDER BY community_key",
            (tenant,),
        ).fetchall()
        if not pending:
            return 0, 0
        vectors = embedder.embed(
            [str(summary) for _key, summary in pending], input_type="document"
        )
        with conn.transaction():
            for (community_key, _summary), vector in zip(
                pending, vectors, strict=True
            ):
                conn.execute(
                    "UPDATE graph_communities SET summary_embedding = %s "
                    "WHERE tenant_id = %s AND community_key = %s",
                    (vector, tenant, str(community_key)),
                )
    except Exception as exc:  # noqa: BLE001 — best-effort: never fail the build
        _logger.warning(
            "summarize_communities: embedding pass failed (%s); leaving "
            "summary_embedding NULL for %d community/-ies (retried next run)",
            exc,
            len(pending),
        )
        return 0, len(pending)
    return len(pending), 0
