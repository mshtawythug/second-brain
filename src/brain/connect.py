"""Scoring core for `brain connect` — proactive auto-link suggestions (Plan 07).

This module owns the *logic*: it scores candidate (source_doc → target_doc)
wikilink pairs by an RRF blend of an entity-graph affinity leg
(normalized entity overlap over ``graph_entity_mentions``) and an embedding
affinity leg (cosine over ``chunks.embedding``), gates by a confidence floor,
dedups against already-linked pairs, and upserts the survivors into
``link_suggestions``. It also exposes the accept/reject status mutations and the
Typer-free ``## See Also`` vault-writeback primitives shared by the CLI and the
MCP server (so neither layer duplicates the SQL or the file logic).

It deliberately carries no Typer / MCP imports: the CLI (``cli_connect.py``)
and the MCP server map the plain :mod:`brain.errors` exceptions raised here to
their own frameworks.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from .config import Config
from .errors import ConnectError
from .vault._atomic import atomic_write_text
from .vault.paths import safe_wikilink_alias, strip_md_extension
from .wiki.build_related import _avg_embedding, _eligible_source_docs

_logger = logging.getLogger(__name__)

# Suggestion-id prefixes (like document-id prefixes) are hex digits + hyphens
# only; reject anything else before it reaches SQL so a user-supplied ``_`` /
# ``%`` cannot act as a LIKE wildcard. Mirrors ``queries._UUID_PREFIX_RE``.
_UUID_PREFIX_RE = re.compile(r"[0-9a-f-]+")
_MIN_PREFIX_LEN = 6

# Markdown heading the ``accept --write`` path appends suggested wikilinks under.
_SEE_ALSO_HEADING = "## See Also"

# --------------------------------------------------------------------------- #
# ``embedding_affinity`` KNN tuning (Task 4.1 — HNSW-friendly rewrite).
# --------------------------------------------------------------------------- #

# Starting KNN batch size = ``candidate_limit`` × this multiplier. On the live
# corpus nearly every doc sits above the cosine floor, so the first batch yields
# far more than ``candidate_limit`` distinct docs and the adaptive re-query
# below never fires; the multiplier is headroom for a rare cold / sparse source
# whose nearest chunks cluster onto a handful of docs.
_KNN_CANDIDATE_MULTIPLIER = 8

# Max adaptive doublings before falling back to the exhaustive scan. Bounds the
# worst case; correctness holds either way (the fallback is exact).
_KNN_MAX_ITERATIONS = 4

# pgvector 0.8.2 GUC. A *filtered* HNSW scan under a post-filter ``LIMIT``
# under-returns by default: the index hands the planner only ``hnsw.ef_search``
# rows, most of which the JOIN + eligibility filters then drop (measured on
# prod: a ``LIMIT 400`` yielded only 27 rows). ``strict_order`` makes the scan
# ITERATE until the ``LIMIT`` is satisfied AND keeps rows in exact ascending-
# distance order, which the per-doc-max exactness argument in
# :func:`_embedding_affinity_knn` relies on. ``SET LOCAL`` scopes it to the one
# transaction — never a session/global ``SET``. Verified against pgvector 0.8.2:
# the enum is ``{off, relaxed_order, strict_order}``; ``relaxed_order`` would be
# faster but breaks the exact-order precondition, so we require ``strict_order``.
_SET_ITERATIVE_SCAN = "SET LOCAL hnsw.iterative_scan = strict_order"


@dataclass(frozen=True)
class SuggestionRow:
    """One row of the ``link_suggestions`` review queue, joined to doc titles."""

    id: str
    source_doc_id: str
    target_doc_id: str
    source_title: str
    target_title: str
    score: float
    graph_score: float | None
    embed_score: float | None
    status: str
    suggested_at: datetime | None


@dataclass(frozen=True)
class RefreshResult:
    """Outcome counts for a ``brain connect refresh`` pass."""

    source_docs: int = 0
    candidates: int = 0
    written: int = 0
    dry_run: bool = False


@dataclass(frozen=True)
class ActionResult:
    """Result of an accept/reject status mutation + writeback metadata.

    ``source_vault_path`` / ``target_vault_path`` / ``target_title`` carry the
    metadata the caller needs to build + insert the ``## See Also`` wikilink
    for ``accept --write`` without a second round-trip. ``wikilink_written`` is
    populated by the caller after it performs (or skips) the file write.
    """

    suggestion_id: str
    status: str
    source_doc_id: str
    target_doc_id: str
    source_vault_path: str | None
    target_vault_path: str | None
    target_title: str
    wikilink_written: bool = False


# --------------------------------------------------------------------------- #
# Pure scoring helpers (no I/O).
# --------------------------------------------------------------------------- #


def normalized_overlap(shared: int, src_count: int, tgt_count: int) -> float:
    """Normalized entity overlap ``shared / min(src_count, tgt_count)`` ∈ [0, 1].

    This is the graph-affinity signal: the fraction of the *smaller* doc's
    entities that the two docs share. A full subset (one doc's entities ⊂ the
    other's) scores ``1.0``; disjoint sets score ``0.0``. Returns ``0.0`` when
    either side has no entities (the denominator would be zero).
    """
    denom = min(src_count, tgt_count)
    if denom <= 0:
        return 0.0
    return shared / denom


def score_doc_pair(
    graph_signal: float | None, embed_signal: float | None
) -> float:
    """Blend the two affinity legs into one confidence score in [0, 1].

    The legs are combined as ``(graph + embed) / 2`` with an *absent* leg
    counting as ``0.0`` (fixed denominator of 2). This rewards corroboration: a
    pair confirmed by BOTH the entity-graph leg and the embedding leg outscores
    an otherwise-equal pair seen in only one leg — the whole point of the
    feature. The result is directly comparable to ``cfg.connect_min_score`` and
    is what the review table's ``Score`` column shows (the mean of the displayed
    ``Graph`` / ``Embed`` legs).

    Design note: the plan's prose mentioned ``rrf_contribution`` of leg *ranks*,
    but raw RRF tops out at ~``2/(RRF_K+1)`` ≈ 0.033 — far below any usable
    ``connect_min_score`` floor (nothing would ever clear the gate) and
    inconsistent with the plan's display table, where ``Score`` is exactly the
    mean of the raw ``Graph`` / ``Embed`` signals (0.72 = (0.81 + 0.63) / 2).
    This linear blend of the normalized leg signals is the interpretation those
    concrete artifacts pin down.
    """
    graph = graph_signal if graph_signal is not None else 0.0
    embed = embed_signal if embed_signal is not None else 0.0
    return (graph + embed) / 2.0


# --------------------------------------------------------------------------- #
# Affinity legs (DB reads).
# --------------------------------------------------------------------------- #


def graph_affinity(
    conn: psycopg.Connection[Any],
    *,
    source_doc_id: str,
    tenant_id: str,
    candidate_limit: int,
) -> dict[str, float]:
    """Return ``{target_doc_id: graph_score}`` for one source doc.

    Single set-based query (no per-candidate fanout): joins the source doc's
    entity mentions against every other non-draft, vault-backed doc's mentions,
    counts shared entities, and scores via :func:`normalized_overlap`. Pairs
    with no shared entities are absent (they receive ``graph_score = 0.0`` /
    no graph leg in the blend). Scoped to ``tenant_id`` (the relational
    source-of-truth is multi-tenant; migration 012). Capped at
    ``candidate_limit``. Returns an empty dict when the graph is unbuilt /
    AGE-less — the feature degrades to embedding-only.
    """
    if candidate_limit < 1:
        raise ConnectError(f"candidate_limit must be >= 1 (got {candidate_limit})")
    rows = conn.execute(
        """
        WITH src_entities AS (
            SELECT gem.entity_id,
                   COUNT(*) OVER () AS src_count
            FROM graph_entity_mentions gem
            WHERE gem.document_id = %(source)s::uuid
              AND gem.tenant_id = %(tenant)s
        ),
        tgt_entities AS (
            SELECT gem.document_id AS tgt_id,
                   gem.entity_id,
                   COUNT(*) OVER (PARTITION BY gem.document_id) AS tgt_count
            FROM graph_entity_mentions gem
            JOIN documents d ON d.id = gem.document_id
            WHERE gem.tenant_id = %(tenant)s
              AND d.draft = FALSE
              AND d.vault_path IS NOT NULL
              AND d.id <> %(source)s::uuid
        ),
        shared AS (
            SELECT te.tgt_id,
                   COUNT(*)          AS shared_count,
                   MAX(te.tgt_count) AS tgt_count,
                   MAX(se.src_count) AS src_count
            FROM src_entities se
            JOIN tgt_entities te USING (entity_id)
            GROUP BY te.tgt_id
        )
        SELECT tgt_id::text, shared_count, src_count, tgt_count
        FROM shared
        WHERE shared_count > 0
        ORDER BY (shared_count::float / LEAST(src_count, tgt_count)) DESC, tgt_id
        LIMIT %(limit)s
        """,
        {
            "source": source_doc_id,
            "tenant": tenant_id,
            "limit": candidate_limit,
        },
    ).fetchall()
    scored: list[tuple[str, float]] = [
        (
            str(r[0]),
            normalized_overlap(int(r[1]), int(r[2]), int(r[3])),
        )
        for r in rows
    ]
    # Re-sort in Python so the dict insertion order (= rank order) matches the
    # Python-computed scores exactly, independent of any FP drift between the
    # SQL ORDER BY expression and ``normalized_overlap``.
    scored.sort(key=lambda item: (-item[1], item[0]))
    return {tgt_id: score for tgt_id, score in scored}


def embedding_affinity(
    conn: psycopg.Connection[Any],
    *,
    source_doc_id: str,
    candidate_limit: int,
    vector_sim_floor: float,
    knn_multiplier: int = _KNN_CANDIDATE_MULTIPLIER,
) -> dict[str, float]:
    """Return ``{target_doc_id: best_cosine}`` for one source doc, rank-ordered.

    The source doc's average chunk embedding is the query vector (reusing
    :func:`brain.wiki.build_related._avg_embedding`); each candidate doc's score
    is its best per-chunk cosine similarity, floored at ``vector_sim_floor``
    (the same floor runtime ``brain search`` uses). Dict insertion order is the
    cosine-descending rank order (ties broken by target-doc id). Empty when the
    source doc has no embedded chunks (cold corpus) — the feature degrades to
    graph-only.

    Task 4.1 rewrite: the primary path (:func:`_embedding_affinity_knn`) rides
    the ``chunks_embedding_idx`` HNSW index via a top-K nearest-neighbour scan
    with the eligibility predicates IN the SQL, instead of the pre-4.1
    ``MAX(...) GROUP BY`` + range-predicate query that seq-scanned every chunk
    (measured ~1.2s/doc on prod → ~40ms). When the adaptive re-query cannot
    converge within :data:`_KNN_MAX_ITERATIONS` (a cold / sparse source whose
    nearest chunks pile onto a handful of docs) it falls back to the exact
    :func:`_embedding_affinity_exhaustive` — correctness beats speed. The
    output contract is identical across both paths.

    ``knn_multiplier`` sets the first KNN batch (``candidate_limit`` ×
    multiplier); it exists so tests can force truncation with a small value and
    prove the adaptive re-query converges to the exhaustive result. Production
    callers leave it at the default.
    """
    if candidate_limit < 1:
        raise ConnectError(f"candidate_limit must be >= 1 (got {candidate_limit})")
    if knn_multiplier < 1:
        raise ConnectError(f"knn_multiplier must be >= 1 (got {knn_multiplier})")
    src_embedding = _avg_embedding(conn, source_doc_id)
    if src_embedding is None:
        return {}
    knn = _embedding_affinity_knn(
        conn,
        src_embedding=src_embedding,
        source_doc_id=source_doc_id,
        candidate_limit=candidate_limit,
        vector_sim_floor=vector_sim_floor,
        knn_multiplier=knn_multiplier,
    )
    if knn is not None:
        return knn
    return _embedding_affinity_exhaustive(
        conn,
        src_embedding=src_embedding,
        source_doc_id=source_doc_id,
        candidate_limit=candidate_limit,
        vector_sim_floor=vector_sim_floor,
    )


# KNN leg: the eligibility predicates live IN the query so ``LIMIT k`` counts
# only eligible chunks and the HNSW index (``chunks_embedding_idx``) drives the
# ordering. Paired with :data:`_SET_ITERATIVE_SCAN` so the filtered scan does
# not under-return.
_KNN_SQL = """
    SELECT c.document_id::text AS doc_id,
           1 - (c.embedding <=> %(vec)s::vector) AS cosine
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.draft = FALSE
      AND d.vault_path IS NOT NULL
      AND d.id <> %(source)s::uuid
      AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> %(vec)s::vector
    LIMIT %(k)s
"""


def _embedding_affinity_knn(
    conn: psycopg.Connection[Any],
    *,
    src_embedding: Any,
    source_doc_id: str,
    candidate_limit: int,
    vector_sim_floor: float,
    knn_multiplier: int,
) -> dict[str, float] | None:
    """Ride ``chunks_embedding_idx``: fetch K nearest eligible chunks → floor →
    per-doc max → sort → truncate. Returns the ``{doc: best_cosine}`` mapping, or
    ``None`` to signal "did not converge — use the exhaustive fallback".

    Exactness argument (why the returned docs match the exhaustive scan): with
    ``strict_order`` the ``LIMIT k`` rows are the true k nearest eligible chunks
    in exact ascending-distance (descending-cosine) order. So:

    * Every doc that APPEARS has its globally-best chunk among the k — that
      chunk's cosine is ≥ the k-th (smallest returned) cosine, and any other
      chunk of the doc is ≤ its best, so nothing better sits beyond the cut.
      Its per-doc max is therefore exact.
    * Every doc NOT among the k has all chunks below the k-th cosine, hence a
      true best below every captured doc's best.

    Consequently, once the k nearest chunks contain ≥ ``candidate_limit``
    distinct floor-passing docs, the top ``candidate_limit`` by best cosine are
    fully and exactly determined (no unseen doc can outrank them). We also stop
    early when the batch is provably complete: it under-filled (all eligible
    chunks seen) or its smallest returned cosine already fell below the floor
    (nothing further can pass). Otherwise we double k and retry, capped at
    :data:`_KNN_MAX_ITERATIONS` before conceding to the exhaustive fallback.

    ``SET LOCAL`` is scoped by :meth:`psycopg.Connection.transaction`; under the
    autocommit connection the CLI uses this opens a real ``BEGIN``/``COMMIT`` so
    the GUC never leaks past the scan. (Assumes the eligible chunk count stays
    within ``hnsw.max_scan_tuples`` so an under-fill means true exhaustion —
    true for the current corpus; the exhaustive fallback backstops the rest.)
    """
    k = candidate_limit * knn_multiplier
    with conn.transaction():
        conn.execute(_SET_ITERATIVE_SCAN)
        for _ in range(_KNN_MAX_ITERATIONS):
            rows = conn.execute(
                _KNN_SQL,
                {"vec": src_embedding, "source": source_doc_id, "k": k},
            ).fetchall()
            floor_rows = [
                (str(r[0]), float(r[1]))
                for r in rows
                if float(r[1]) >= vector_sim_floor
            ]
            distinct_docs = {doc_id for doc_id, _ in floor_rows}
            # Provably complete when the batch under-filled (every eligible
            # chunk seen) or its smallest returned cosine is already below the
            # floor (rows are in exact descending-cosine order, so nothing
            # further can pass).
            seen_all = len(rows) < k or (
                len(rows) > 0 and float(rows[-1][1]) < vector_sim_floor
            )
            if seen_all or len(distinct_docs) >= candidate_limit:
                return _group_per_doc_max(floor_rows, candidate_limit)
            k *= 2
    return None


def _group_per_doc_max(
    floor_rows: list[tuple[str, float]], candidate_limit: int
) -> dict[str, float]:
    """Collapse floor-passing ``(doc_id, cosine)`` rows to the per-doc max,
    ordered cosine-descending then doc-id, truncated to ``candidate_limit``.

    Mirrors the exhaustive query's ``MAX(...) ... ORDER BY cosine DESC, doc_id
    LIMIT`` in Python. Values are byte-identical to the SQL aggregate: both take
    the max of the same float8 cosines each backend computed.
    """
    by_doc: dict[str, float] = {}
    for doc_id, cosine in floor_rows:
        prev = by_doc.get(doc_id)
        if prev is None or cosine > prev:
            by_doc[doc_id] = cosine
    ordered = sorted(by_doc.items(), key=lambda item: (-item[1], item[0]))
    return {doc_id: cosine for doc_id, cosine in ordered[:candidate_limit]}


def _embedding_affinity_exhaustive(
    conn: psycopg.Connection[Any],
    *,
    src_embedding: Any,
    source_doc_id: str,
    candidate_limit: int,
    vector_sim_floor: float,
) -> dict[str, float]:
    """Pre-4.1 exhaustive ``MAX(...) GROUP BY`` scan — exact but seq-scans every
    chunk (the range predicate on the cosine defeats the HNSW index).

    Retained verbatim as the correctness fallback for the rare case where the
    adaptive KNN re-query in :func:`_embedding_affinity_knn` cannot converge
    (a cold / sparse corpus where k would have to grow past the cap). Callers
    reach it only after that path returns ``None``.
    """
    rows = conn.execute(
        """
        SELECT d.id::text AS doc_id,
               MAX(1 - (c.embedding <=> %(vec)s::vector)) AS cosine
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.draft = FALSE
          AND d.vault_path IS NOT NULL
          AND d.id <> %(source)s::uuid
          AND c.embedding IS NOT NULL
          AND 1 - (c.embedding <=> %(vec)s::vector) >= %(floor)s
        GROUP BY d.id
        ORDER BY cosine DESC, doc_id
        LIMIT %(limit)s
        """,
        {
            "vec": src_embedding,
            "source": source_doc_id,
            "floor": vector_sim_floor,
            "limit": candidate_limit,
        },
    ).fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


def _existing_link_targets(
    conn: psycopg.Connection[Any], source_doc_id: str
) -> set[str]:
    """Return target ids already linked from ``source_doc_id``.

    Reads both ``links`` (extracted wikilinks) and ``derived_links``
    (fence-emitted edges); both use ``src_document_id`` / ``dst_document_id``
    (verified: migrations 003 + 005). A pair already present in either table is
    dropped from the candidate set so ``connect`` never re-suggests an existing
    edge.

    Both ``links`` and ``derived_links`` are treated as UNDIRECTED here, because
    a suggestion pair is undirected for review purposes (migration 022): if A
    and B are already connected in EITHER orientation, the pair must not be
    re-suggested in either orientation. ``links`` rows are individually directed
    (a wikilink from A's body to B), so we union both the ``src = source`` and
    ``dst = source`` directions. ``derived_links`` are stored as a single
    CANONICAL ``(LEAST, GREATEST)`` pair (already undirected — see
    ``derived_links.pass_runner._canonical_pair``).
    """
    rows = conn.execute(
        """
        SELECT dst_document_id::text FROM links WHERE src_document_id = %(src)s::uuid
        UNION
        SELECT src_document_id::text FROM links WHERE dst_document_id = %(src)s::uuid
        UNION
        SELECT dst_document_id::text FROM derived_links WHERE src_document_id = %(src)s::uuid
        UNION
        SELECT src_document_id::text FROM derived_links WHERE dst_document_id = %(src)s::uuid
        """,
        {"src": source_doc_id},
    ).fetchall()
    return {str(r[0]) for r in rows}


def _retire_linked_pending(
    conn: psycopg.Connection[Any], source_ids: list[str]
) -> int:
    """Delete ``pending`` suggestions whose pair is now in ``links`` / ``derived_links``.

    Closes the window where a suggestion is queued, then the user draws the link
    manually (or a derived edge appears) — the stale pending row must leave the
    queue on the next refresh. Only ``pending`` rows are removed (accepted /
    rejected are frozen). Returns the row count deleted.

    Scoping is by EITHER endpoint (``source_doc_id`` OR ``target_doc_id`` in
    ``source_ids``), not just the stored source: suggestions are undirected
    (migration 022), so the stored orientation is not stable. A partial
    ``refresh --doc B`` must still retire a pending row stored as ``A -> B`` once
    the pair is linked, even though ``B`` is the row's target, not its source.

    Both ``links`` and ``derived_links`` are matched UNDIRECTED: a link drawn in
    EITHER orientation retires the pending row regardless of how the suggestion
    stored its source/target.
    """
    if not source_ids:
        return 0
    cur = conn.execute(
        """
        DELETE FROM link_suggestions ls
        WHERE ls.status = 'pending'
          AND (ls.source_doc_id = ANY(%(srcs)s::uuid[])
               OR ls.target_doc_id = ANY(%(srcs)s::uuid[]))
          AND (
              EXISTS (
                  -- links are matched undirected — match either orientation.
                  SELECT 1 FROM links l
                  WHERE (l.src_document_id = ls.source_doc_id
                         AND l.dst_document_id = ls.target_doc_id)
                     OR (l.src_document_id = ls.target_doc_id
                         AND l.dst_document_id = ls.source_doc_id)
              )
              OR EXISTS (
                  -- derived_links are canonical/undirected — match either way.
                  SELECT 1 FROM derived_links d
                  WHERE (d.src_document_id = ls.source_doc_id
                         AND d.dst_document_id = ls.target_doc_id)
                     OR (d.src_document_id = ls.target_doc_id
                         AND d.dst_document_id = ls.source_doc_id)
              )
          )
        """,
        {"srcs": source_ids},
    )
    return cur.rowcount


# --------------------------------------------------------------------------- #
# Refresh pipeline (DB read + write).
# --------------------------------------------------------------------------- #


def refresh_suggestions(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    doc_prefix: str | None = None,
    dry_run: bool = False,
) -> RefreshResult:
    """Recompute candidate suggestions and upsert the survivors.

    For each eligible source doc (non-draft, vault-backed, ≥1 embedded chunk —
    the same predicate as ``build_related``), blend the graph + embedding legs
    via RRF, drop pairs already linked or scoring below ``cfg.connect_min_score``,
    keep the top ``cfg.connect_max_per_doc``, and upsert into
    ``link_suggestions``. Suggestions are UNDIRECTED (migration 022): exactly one
    row persists per unordered pair, storing the best-scoring orientation (see
    :func:`_upsert_suggestion`). Accepted/rejected rows are frozen and their
    mirror is never re-suggested.

    ``doc_prefix`` limits the refresh to a single source doc (resolved via the
    document-id prefix). ``dry_run`` computes everything but writes nothing.
    The caller owns transaction scope (commit/autocommit).
    """
    if cfg.connect_candidate_limit < 1:
        raise ConnectError(
            f"connect_candidate_limit must be >= 1 (got {cfg.connect_candidate_limit})"
        )
    if cfg.connect_max_per_doc < 1:
        raise ConnectError(
            f"connect_max_per_doc must be >= 1 (got {cfg.connect_max_per_doc})"
        )
    if not (0.0 < cfg.connect_min_score <= 1.0):
        raise ConnectError(
            f"connect_min_score must be in (0.0, 1.0] (got {cfg.connect_min_score})"
        )

    sources = _eligible_source_docs(conn)
    if doc_prefix is not None:
        # Resolve the prefix lazily here (kept out of the import surface) so a
        # bad prefix surfaces the same IdPrefix* errors the rest of the CLI uses.
        from .queries import resolve_document_prefix

        target_id = resolve_document_prefix(conn, doc_prefix)
        sources = [s for s in sources if s.id == target_id]

    if not dry_run:
        # Retire any PENDING suggestion whose pair has since been linked (a
        # manual wikilink or derived edge added after it was queued) so the
        # review queue never re-surfaces an edge that already exists (Codex
        # R2 #1). Scoped to the sources being refreshed; accepted/rejected
        # rows are frozen and untouched.
        _retire_linked_pending(conn, [s.id for s in sources])

    candidates_total = 0
    written_total = 0
    for source in sources:
        scored = _score_source(conn, cfg, source_doc_id=source.id)
        candidates_total += len(scored)
        kept = [pair for pair in scored if pair[1] >= cfg.connect_min_score]
        kept = kept[: cfg.connect_max_per_doc]
        written_total += len(kept)
        if dry_run:
            continue
        for target_id, score, graph_score, embed_score in kept:
            _upsert_suggestion(
                conn,
                source_doc_id=source.id,
                target_doc_id=target_id,
                score=score,
                graph_score=graph_score,
                embed_score=embed_score,
            )

    _logger.info(
        "connect refresh: sources=%d candidates=%d written=%d dry_run=%s",
        len(sources),
        candidates_total,
        written_total,
        dry_run,
    )
    return RefreshResult(
        source_docs=len(sources),
        candidates=candidates_total,
        written=written_total,
        dry_run=dry_run,
    )


def _score_source(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    source_doc_id: str,
) -> list[tuple[str, float, float | None, float | None]]:
    """Blend both legs for one source doc → ranked ``(tgt, score, g, e)`` list.

    The returned list is sorted by RRF score descending (ties broken by target
    id) and has the already-linked pairs removed. ``g`` / ``e`` are the raw
    graph / embedding leg signals (or ``None`` when the pair is absent from
    that leg) — stored on the row for display, distinct from the blended
    ``score``.
    """
    graph = graph_affinity(
        conn,
        source_doc_id=source_doc_id,
        tenant_id=cfg.graph_tenant_id,
        candidate_limit=cfg.connect_candidate_limit,
    )
    embed = embedding_affinity(
        conn,
        source_doc_id=source_doc_id,
        candidate_limit=cfg.connect_candidate_limit,
        vector_sim_floor=cfg.vector_sim_floor,
    )
    already_linked = _existing_link_targets(conn, source_doc_id)

    blended: list[tuple[str, float, float | None, float | None]] = []
    for target_id in set(graph) | set(embed):
        if target_id in already_linked:
            continue
        graph_score = graph.get(target_id)
        embed_score = embed.get(target_id)
        score = score_doc_pair(graph_score, embed_score)
        blended.append((target_id, score, graph_score, embed_score))
    blended.sort(key=lambda item: (-item[1], item[0]))
    return blended


def _upsert_suggestion(
    conn: psycopg.Connection[Any],
    *,
    source_doc_id: str,
    target_doc_id: str,
    score: float,
    graph_score: float | None,
    embed_score: float | None,
) -> None:
    """Upsert one UNDIRECTED suggestion, keeping the best-scoring orientation.

    A suggested pair is undirected for review purposes (migration 022): exactly
    one row may exist per unordered pair, regardless of which doc is stored as
    source vs target. The conflict is inferred against the functional unique
    index ``uq_link_suggestions_unordered_pair`` on
    ``(LEAST(source, target), GREATEST(source, target))``.

    On conflict the row is rewritten — INCLUDING its orientation
    (``source_doc_id`` / ``target_doc_id``) — to the incoming candidate, but
    ONLY when the existing row is ``pending`` AND the new ``score`` is strictly
    greater. This keeps the better-scoring orientation (so the ``accept --write``
    writeback targets the more sensible "source" doc) and never thrashes on
    ties. Because the conflict consumes the insert, an accepted/rejected pair
    can NOT have its mirror re-inserted: refresh never resuggests the reverse of
    a decided pair (the WHERE leaves the frozen row untouched and no new row is
    created).
    """
    conn.execute(
        """
        INSERT INTO link_suggestions
            (source_doc_id, target_doc_id, score, graph_score, embed_score)
        VALUES (%(src)s::uuid, %(dst)s::uuid, %(score)s, %(graph)s, %(embed)s)
        ON CONFLICT (LEAST(source_doc_id, target_doc_id),
                     GREATEST(source_doc_id, target_doc_id)) DO UPDATE
            SET source_doc_id = EXCLUDED.source_doc_id,
                target_doc_id = EXCLUDED.target_doc_id,
                score = EXCLUDED.score,
                graph_score = EXCLUDED.graph_score,
                embed_score = EXCLUDED.embed_score,
                suggested_at = now()
            WHERE link_suggestions.status = 'pending'
              AND EXCLUDED.score > link_suggestions.score
        """,
        {
            "src": source_doc_id,
            "dst": target_doc_id,
            "score": score,
            "graph": graph_score,
            "embed": embed_score,
        },
    )


# --------------------------------------------------------------------------- #
# Queue reads.
# --------------------------------------------------------------------------- #


def iter_suggestions(
    conn: psycopg.Connection[Any],
    *,
    status: str | None = "pending",
    limit: int = 20,
) -> list[SuggestionRow]:
    """Return suggestions joined to their source/target titles.

    ``status=None`` returns every row (the ``--all`` view); otherwise filters to
    one status. Ordered by score descending. ``limit`` caps the result.
    """
    rows = conn.execute(
        """
        SELECT ls.id::text, ls.source_doc_id::text, ls.target_doc_id::text,
               sd.title, td.title, ls.score, ls.graph_score, ls.embed_score,
               ls.status, ls.suggested_at
        FROM link_suggestions ls
        JOIN documents sd ON sd.id = ls.source_doc_id
        JOIN documents td ON td.id = ls.target_doc_id
        WHERE (%(status)s::text IS NULL OR ls.status = %(status)s)
        ORDER BY ls.score DESC, ls.id
        LIMIT %(limit)s
        """,
        {"status": status, "limit": limit},
    ).fetchall()
    return [
        SuggestionRow(
            id=str(r[0]),
            source_doc_id=str(r[1]),
            target_doc_id=str(r[2]),
            source_title=str(r[3] or ""),
            target_title=str(r[4] or ""),
            score=float(r[5]),
            graph_score=None if r[6] is None else float(r[6]),
            embed_score=None if r[7] is None else float(r[7]),
            status=str(r[8]),
            suggested_at=r[9],
        )
        for r in rows
    ]


def suggestion_counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """Return ``{status: count}`` for the three statuses (zero-filled)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM link_suggestions GROUP BY status"
    ).fetchall()
    counts = {"pending": 0, "accepted": 0, "rejected": 0}
    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


def resolve_suggestion_prefix(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve a suggestion-id prefix (min 6 hex chars) to a full id.

    Mirrors :func:`brain.queries.resolve_document_prefix` but against
    ``link_suggestions``. Raises :class:`ConnectError` on a too-short /
    non-hex / not-found / ambiguous prefix.
    """
    if len(prefix) < _MIN_PREFIX_LEN:
        raise ConnectError("suggestion id prefix must be at least 6 characters")
    if not _UUID_PREFIX_RE.fullmatch(prefix):
        raise ConnectError(
            "suggestion id prefix must contain only hex digits and hyphens"
        )
    rows = conn.execute(
        "SELECT id::text FROM link_suggestions WHERE id::text LIKE %s",
        (prefix + "%",),
    ).fetchall()
    if not rows:
        raise ConnectError(f"suggestion not found: {prefix}")
    if len(rows) > 1:
        raise ConnectError(f"suggestion id prefix ambiguous: {prefix}")
    return str(rows[0][0])


# --------------------------------------------------------------------------- #
# Accept / reject (status mutation + writeback metadata).
# --------------------------------------------------------------------------- #


def load_action_context(
    conn: psycopg.Connection[Any], suggestion_id: str
) -> ActionResult:
    """Return a suggestion's current state + writeback metadata WITHOUT mutating.

    Lets an ``accept --write`` caller fetch the source/target vault paths and
    title, perform the vault write FIRST, and only then flip the status — so a
    write failure never leaves the row frozen ``accepted`` with no wikilink
    (Codex R1 #1). ``status`` is the row's current status. Raises
    :class:`ConnectError` for a missing suggestion id.
    """
    row = conn.execute(
        """
        SELECT ls.status, ls.source_doc_id::text, ls.target_doc_id::text,
               sd.vault_path, td.vault_path, td.title
        FROM link_suggestions ls
        JOIN documents sd ON sd.id = ls.source_doc_id
        JOIN documents td ON td.id = ls.target_doc_id
        WHERE ls.id = %s::uuid
        """,
        (suggestion_id,),
    ).fetchone()
    if row is None:
        raise ConnectError(f"suggestion not found: {suggestion_id}")
    return ActionResult(
        suggestion_id=suggestion_id,
        status=str(row[0]),
        source_doc_id=str(row[1]),
        target_doc_id=str(row[2]),
        source_vault_path=row[3],
        target_vault_path=row[4],
        target_title=str(row[5] or ""),
    )


def set_suggestion_status(
    conn: psycopg.Connection[Any], suggestion_id: str, status: str
) -> ActionResult:
    """Flip a suggestion to ``accepted`` / ``rejected`` and stamp ``actioned_at``.

    Returns the writeback metadata (source/target vault paths + target title)
    so an ``accept --write`` caller can build + insert the wikilink without a
    second query. Raises :class:`ConnectError` for an unknown status or a
    missing suggestion id.
    """
    if status not in ("accepted", "rejected"):
        raise ConnectError(
            f"status must be 'accepted' or 'rejected' (got {status!r})"
        )
    ctx = load_action_context(conn, suggestion_id)
    conn.execute(
        "UPDATE link_suggestions SET status = %s, actioned_at = now() WHERE id = %s::uuid",
        (status, suggestion_id),
    )
    return replace(ctx, status=status)


# --------------------------------------------------------------------------- #
# Vault writeback primitives (Typer-free; shared by CLI + MCP).
# --------------------------------------------------------------------------- #


def build_see_also_wikilink(target_vault_path: str, target_title: str) -> str:
    """Build the path-form alias wikilink ``[[<path-no-md>|<title>]]``.

    The alias (path) form — not a bare ``[[Title]]`` — guards against
    misresolution when two docs share a title. The target path is stripped of
    its ``.md`` suffix (matching ``_resolve_by_vault_path``); the title has
    brackets sanitized (Quartz alias rule) and any ``|`` replaced with ``-``
    (the wikilink delimiter).
    """
    link_path = strip_md_extension(target_vault_path)
    safe_title = safe_wikilink_alias(target_title).replace("|", "-")
    return f"[[{link_path}|{safe_title}]]"


def append_see_also_link(vault_file: Path, wikilink: str) -> bool:
    """Insert ``- <wikilink>`` into the file's ``## See Also`` section.

    Idempotent: returns ``False`` (no write) when ``wikilink`` is already in the
    file. Otherwise:

    - If a ``## See Also`` heading exists (anywhere — not only as the trailing
      section, Codex R1 #2), the bullet is inserted at the END of that section
      (just before the next markdown heading, or EOF), so it never lands under
      an unrelated later section.
    - Otherwise a new ``## See Also`` section is appended at the end of the file.

    Writes via :func:`atomic_write_text` and returns ``True``. Never logs the
    file body (privacy rule: ids / paths only).
    """
    text = vault_file.read_text(encoding="utf-8") if vault_file.exists() else ""
    if wikilink in text:
        return False
    bullet = f"- {wikilink}"
    lines = text.splitlines()
    heading_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == _SEE_ALSO_HEADING),
        None,
    )
    if heading_idx is None:
        body = text.rstrip("\n")
        new_text = (
            f"{body}\n\n{_SEE_ALSO_HEADING}\n\n{bullet}\n"
            if body
            else f"{_SEE_ALSO_HEADING}\n\n{bullet}\n"
        )
        atomic_write_text(vault_file, new_text)
        return True
    # Find the end of the See Also section: the next markdown heading after it,
    # or EOF. Insert the bullet just after the section's last non-blank line.
    section_end = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        if lines[j].startswith("#"):
            section_end = j
            break
    insert_at = section_end
    while insert_at - 1 > heading_idx and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    lines.insert(insert_at, bullet)
    atomic_write_text(vault_file, "\n".join(lines) + "\n")
    return True
