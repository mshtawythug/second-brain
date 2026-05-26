"""Global (community-based) graph retrieval (spec §6c — wave G3-d).

The third retrieval path alongside **local** (:mod:`brain.graph_rag.retrieve`)
and **themes-with-X** (:mod:`brain.graph_rag.themes`). Where local resolves
entity seeds and themes scopes a person, **global** ranks the tenant's
pre-detected **communities** (``graph_communities``) against a free-text query
and returns the top ones with their representative entities + documents. The
public dispatch entry :func:`brain.graph_rag.retrieve.graph_rag_search` calls
:func:`_retrieve_global` here when ``mode='global'`` (the router flip to route
*auto* there is wave G3-e; G3-d only adds the path + the explicit-mode dispatch).

The ranking (spec §17c Q4): the ranked unit is the **community**, not docs or
chunks. Two signals are fused with Reciprocal Rank Fusion
(:func:`brain.rank_fusion.rrf_contribution`, ``k=60`` matching
:mod:`brain.search`):

* **FTS leg** — ``ts_rank`` over ``graph_communities.summary_tsv`` for
  communities with a non-NULL ``summary`` (reusing
  :func:`brain.search._build_tsquery` so the tsquery shape matches hybrid
  search rather than reinventing it).
* **Vector leg** — cosine distance over ``summary_embedding`` for communities
  with a non-NULL embedding. The query is embedded ``input_type="query"`` via
  the injected ``embedder`` instance (spec §17c Q9). The vector leg is the
  **only reason global needs an embedder**; local/themes stay embedder-free.

``score = Σ 1/(60 + rank + 1)`` per ``community_key``; ties broken by
``community_key`` (deterministic). The top ``limit`` communities
(``cfg.graph_community_limit`` default) become :class:`CommunityGroup`s.

Design invariants (mirroring the sibling paths):

* **Never-raise (spec §17c Q9/Q10).** No communities / no summaries → an
  empty-but-valid :class:`GraphContext` (never raises). A **missing**
  ``embedder`` OR a **failing** query-embedding call → a WARN + the vector
  leg is skipped (FTS-only), never a hard failure — exactly the §6c "guarded
  on non-null embedding" degradation.
* **Tenant-scoped (spec §4 D9).** Every read filters by ``tenant_id``.
* **Deterministic ordering.** Both legs order before truncation, the fusion
  tie-breaks on ``community_key``, and the per-community entity/doc reads break
  ties on stable keys, so repeated runs return byte-identical results.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

import psycopg

from ..rank_fusion import rrf_contribution
from ._retrieval_common import _build_doc_results, _row_to_entity
from .router import GLOBAL_MODE
from .schema import CommunityGroup, GraphContext, GraphEntity, GraphExplanation

if TYPE_CHECKING:
    from ..config import Config
    from ..ingest import Embedder
    from ..search import SearchResult

_logger = logging.getLogger(__name__)

# RRF k constant — matches :data:`brain.search.RRF_K` (spec §17c Q4 keeps the
# global path and hybrid search on the same fusion constant via the shared
# :func:`brain.rank_fusion.rrf_contribution` helper).
_RRF_K = 60

# Per-leg candidate cap. The community universe is already bounded by the §17c Q8
# ``graph_community_max`` detection cap, but capping each ranking leg keeps the
# fusion work bounded under multi-tenant scale and the truncation deterministic
# (each leg orders before slicing).
_CANDIDATE_LIMIT = 200

# Representative member entities carried on each ``CommunityGroup`` (top by
# ``member_rank``). ``member_count`` carries the authoritative full size; this
# only caps the surfaced subset (mirrors
# :data:`brain.graph_rag.communities_summary._SUMMARY_ENTITY_LIMIT`).
_MEMBER_ENTITY_LIMIT = 20


def _retrieve_global(
    conn: psycopg.Connection[Any],
    cfg: Config,
    query: str,
    *,
    tenant: str,
    limit: int | None = None,
    embedder: Embedder | None = None,
    session_id: str | None = None,
) -> GraphContext:
    """Run global (community-based) retrieval + assemble its ``GraphContext``.

    Ranks the tenant's communities by fusing an FTS leg (over ``summary_tsv``)
    with a vector leg (over ``summary_embedding``, only when an embedder is
    available) via RRF, takes the top ``limit`` (``cfg.graph_community_limit``
    when ``None``), and populates each :class:`CommunityGroup` with its
    representative member entities + documents (+ snippets via the shared
    :func:`brain.graph_rag._retrieval_common._build_doc_results`).

    ``embedder`` is the **pre-warmed** :class:`brain.ingest.Embedder` instance
    used to embed the *query* for the vector leg (spec §17c Q9 — local/themes
    never need one). Pre-warmed because the caller (CLI/MCP/fuse) constructs
    or reuses ONE long-lived instance and passes it in, so global mode never
    re-builds the embedder per call (perf-T4 G5). A ``None`` embedder or a
    failing ``embed()`` call logs a WARN and runs **FTS-only** (never-raise).
    No communities / no summaries → an empty-but-valid context. ``session_id``
    is generated when omitted (a fresh ``uuid4`` hex), mirroring the MCP
    ``{session_id, results}`` envelope.
    """
    resolved_limit = cfg.graph_community_limit if limit is None else limit
    resolved_session = uuid.uuid4().hex if session_id is None else session_id

    fts_keys = _fts_ranked_keys(conn, tenant=tenant, query=query)
    vector_keys, vector_arm_used = _vector_ranked_keys(
        conn, tenant=tenant, query=query, embedder=embedder
    )

    fused = _fuse_rrf(fts_keys, vector_keys)
    top = fused[:resolved_limit] if resolved_limit > 0 else []

    communities, docs = _build_communities(conn, tenant, query, top, resolved_limit)
    entities = _dedupe_entities(communities)

    explanation = GraphExplanation(
        mode=GLOBAL_MODE,
        tenant_id=tenant,
        seed_entity_ids=[],
        person_keys=[],
        depth=0,
        frontier_cap=0,
        min_edge_weight=0.0,
        nodes_visited=len(communities),
        edges_considered=0,
        generic_df_cap=None,
        matched_filters={
            "query": query,
            "fts_candidate_count": len(fts_keys),
            "vector_candidate_count": len(vector_keys),
            "vector_arm_used": vector_arm_used,
            "community_count": len(communities),
            "limit": resolved_limit,
        },
    )
    return GraphContext(
        session_id=resolved_session,
        mode=GLOBAL_MODE,
        query=query,
        tenant_id=tenant,
        person=None,
        themes=[],
        communities=communities,
        entities=entities,
        docs=docs,
        explanation=explanation,
    )


# --------------------------------------------------------------------------- #
# Ranking legs (FTS + vector) → RRF fusion
# --------------------------------------------------------------------------- #
def _fts_ranked_keys(
    conn: psycopg.Connection[Any], *, tenant: str, query: str
) -> list[str]:
    """Communities ranked by ``ts_rank`` over ``summary_tsv`` (FTS leg, §17c Q4).

    Tenant-scoped, restricted to communities with a non-NULL ``summary``. Reuses
    :func:`brain.search._build_tsquery` (late import — keeps :mod:`brain.graph_rag`
    import-cheap, mirroring :func:`._retrieval_common._build_doc_results`) so the
    compact-form tsquery expansion matches hybrid search. Orders by ``ts_rank``
    DESC then ``community_key`` so the 0-indexed rank used for RRF is
    deterministic; capped at :data:`_CANDIDATE_LIMIT`. An empty / pure-punctuation
    query (empty tsquery) ranks nothing.
    """
    from ..search import _build_tsquery

    tsquery = _build_tsquery(conn, query)
    if not tsquery:
        return []
    rows = conn.execute(
        "SELECT community_key::text FROM graph_communities "
        "WHERE tenant_id = %s AND summary IS NOT NULL "
        "AND summary_tsv @@ to_tsquery('english', %s) "
        "ORDER BY ts_rank(summary_tsv, to_tsquery('english', %s)) DESC, "
        "community_key "
        "LIMIT %s",
        (tenant, tsquery, tsquery, _CANDIDATE_LIMIT),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _vector_ranked_keys(
    conn: psycopg.Connection[Any],
    *,
    tenant: str,
    query: str,
    embedder: Embedder | None,
) -> tuple[list[str], bool]:
    """Communities ranked by ``summary_embedding`` cosine (vector leg, §17c Q4/Q9).

    Returns ``(keys, attempted)``. The vector leg requires an embedder (spec
    §17c Q9 — global REQUIRES one; local/themes stay embedder-free): the
    injected ``embedder`` is the caller's PRE-WARMED instance (perf-T4 G5 —
    the caller constructs ONCE and reuses across calls / legs), used to embed
    the *query* (``input_type="query"``). A ``None`` embedder OR **any**
    failure in the vector arm — embedding the query OR executing the cosine
    ``<=>`` SQL (e.g. a dim mismatch when the backend was swapped without a
    community rebuild, or a transient DB error) → a WARN and ``([], False)``
    (FTS-only degradation; never-raise, matching §6c "guarded on non-null
    embedding"). The cosine SQL is inside the guard precisely so a dim-mismatch
    / DB error in the ``<=>`` execution degrades to FTS-only rather than
    breaking global retrieval. ``attempted=True`` means the full vector arm ran
    (embed + cosine SQL succeeded) and the ranking is usable (even if zero
    communities carry a ``summary_embedding`` — the leg simply contributes
    nothing).

    Tenant-scoped, restricted to ``summary_embedding IS NOT NULL``; ordered by
    cosine distance ascending then ``community_key`` (deterministic), capped at
    :data:`_CANDIDATE_LIMIT`. Runs under the caller's autocommit connection (the
    CLI/MCP search paths set ``autocommit=True``), so a failed ``<=>`` statement
    leaves the connection usable for the subsequent FTS-only assembly.
    """
    if embedder is None:
        _logger.warning(
            "global retrieval: no embedder injected; vector leg skipped "
            "(FTS-only)"
        )
        return [], False
    try:
        query_vec = embedder.embed([query], input_type="query")[0]
        rows = conn.execute(
            "SELECT community_key::text FROM graph_communities "
            "WHERE tenant_id = %s AND summary_embedding IS NOT NULL "
            "ORDER BY summary_embedding <=> %s::vector, community_key "
            "LIMIT %s",
            (tenant, query_vec, _CANDIDATE_LIMIT),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — never-raise: degrade to FTS-only
        _logger.warning(
            "global retrieval: vector leg failed (%s); vector leg skipped "
            "(FTS-only)",
            exc,
        )
        return [], False
    return [str(row[0]) for row in rows], True


def _fuse_rrf(
    fts_keys: list[str], vector_keys: list[str]
) -> list[tuple[str, float]]:
    """Fuse the two ranked legs with RRF (spec §17c Q4).

    Each leg contributes ``1 / (60 + rank + 1)`` per ``community_key`` (0-indexed
    rank, via :func:`brain.rank_fusion.rrf_contribution`); contributions
    accumulate across legs. Returns ``(community_key, score)`` sorted by score
    DESC then ``community_key`` ASC (the deterministic tie-break).
    """
    scores: dict[str, float] = {}
    for rank, key in enumerate(fts_keys):
        scores[key] = scores.get(key, 0.0) + rrf_contribution(rank, k=_RRF_K)
    for rank, key in enumerate(vector_keys):
        scores[key] = scores.get(key, 0.0) + rrf_contribution(rank, k=_RRF_K)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


# --------------------------------------------------------------------------- #
# Community population (entities + representative docs + snippets)
# --------------------------------------------------------------------------- #
def _build_communities(
    conn: psycopg.Connection[Any],
    tenant: str,
    query: str,
    ranked: list[tuple[str, float]],
    limit: int,
) -> tuple[list[CommunityGroup], list[SearchResult]]:
    """Assemble ranked :class:`CommunityGroup`s + the context-level docs.

    For the top communities (``ranked``, preserving fusion order), batch-load
    each community's metadata (``level`` / ``member_count`` / ``summary``),
    representative member entities (top by ``member_rank``), and representative
    documents (those most mentioning the community's entities). The
    context-level ``docs`` aggregate every contributing document's per-community
    mention scores and reuse :func:`._retrieval_common._build_doc_results` for
    the snippet path (so global docs match the local/themes/hybrid snippet
    shape). An empty ``ranked`` returns ``([], [])`` (the never-raise empty case).
    """
    if not ranked:
        return [], []
    keys = [key for key, _score in ranked]
    score_by_key = dict(ranked)

    meta_by_key = _community_metadata(conn, tenant, keys)
    entities_by_key = _community_entities(conn, tenant, keys)
    docs_by_key = _community_doc_scores(conn, tenant, keys)

    communities: list[CommunityGroup] = []
    context_doc_score: dict[str, float] = {}
    for key in keys:  # preserve fusion ranking order
        level, member_count, summary = meta_by_key.get(key, (0, 0, None))
        doc_scores = docs_by_key.get(key, [])
        doc_ids = [doc_id for doc_id, _count in doc_scores[:limit]]
        communities.append(
            CommunityGroup(
                community_key=key,
                level=level,
                member_count=member_count,
                score=score_by_key[key],
                summary=summary,
                entities=entities_by_key.get(key, []),
                doc_ids=doc_ids,
            )
        )
        for doc_id, count in doc_scores:
            context_doc_score[doc_id] = context_doc_score.get(doc_id, 0.0) + count

    context_ranked = sorted(
        context_doc_score.items(), key=lambda kv: (-kv[1], kv[0])
    )[:limit]
    return communities, _build_doc_results(conn, query, context_ranked)


def _community_metadata(
    conn: psycopg.Connection[Any], tenant: str, keys: list[str]
) -> dict[str, tuple[int, int, str | None]]:
    """Batch-load ``(level, member_count, summary)`` per community key."""
    if not keys:
        return {}
    rows = conn.execute(
        "SELECT community_key::text, level, member_count, summary "
        "FROM graph_communities "
        "WHERE tenant_id = %s AND community_key::text = ANY(%s)",
        (tenant, keys),
    ).fetchall()
    return {str(row[0]): (int(row[1]), int(row[2]), row[3]) for row in rows}


def _community_entities(
    conn: psycopg.Connection[Any], tenant: str, keys: list[str]
) -> dict[str, list[GraphEntity]]:
    """Batch-load each community's representative member entities (by member_rank).

    Joins ``graph_community_members`` → ``graph_entities`` (tenant-scoped),
    orders by ``member_rank`` ASC then ``canonical_key`` / ``id`` (deterministic),
    groups by community key in Python, and caps each list at
    :data:`_MEMBER_ENTITY_LIMIT`. Maps each row to a :class:`GraphEntity` via the
    shared :func:`._retrieval_common._row_to_entity`.
    """
    if not keys:
        return {}
    rows = conn.execute(
        "SELECT cm.community_key::text, ge.id::text, ge.entity_type, ge.name, "
        "ge.canonical_key, ge.description, ge.doc_count "
        "FROM graph_community_members cm "
        "JOIN graph_entities ge "
        "  ON ge.tenant_id = cm.tenant_id AND ge.id = cm.entity_id "
        "WHERE cm.tenant_id = %s AND cm.community_key::text = ANY(%s) "
        "ORDER BY cm.community_key, cm.member_rank ASC, ge.canonical_key ASC, "
        "ge.id ASC",
        (tenant, keys),
    ).fetchall()
    out: dict[str, list[GraphEntity]] = {}
    for row in rows:
        key = str(row[0])
        bucket = out.setdefault(key, [])
        if len(bucket) >= _MEMBER_ENTITY_LIMIT:
            continue
        bucket.append(_row_to_entity(row[1:7], tenant))
    return out


def _community_doc_scores(
    conn: psycopg.Connection[Any], tenant: str, keys: list[str]
) -> dict[str, list[tuple[str, float]]]:
    """Batch-load each community's representative documents + mention scores.

    Joins ``graph_community_members`` → ``graph_entity_mentions`` (tenant-scoped)
    and counts, per community, how many of the community's entity-mention rows
    hit each document. Returns ``community_key → [(document_id, count), …]``
    ordered by count DESC then ``document_id`` ASC (deterministic). Mirrors the
    document ranking in :func:`brain.graph_rag.communities_summary.
    _representative_doc_titles` (count of community entities present), but keyed
    by document id for the per-community ``doc_ids`` + the context-level fusion.
    """
    if not keys:
        return {}
    rows = conn.execute(
        "SELECT cm.community_key::text, m.document_id::text, COUNT(*) AS n "
        "FROM graph_community_members cm "
        "JOIN graph_entity_mentions m "
        "  ON m.tenant_id = cm.tenant_id AND m.entity_id = cm.entity_id "
        "WHERE cm.tenant_id = %s AND cm.community_key::text = ANY(%s) "
        "GROUP BY cm.community_key, m.document_id "
        "ORDER BY cm.community_key, n DESC, m.document_id ASC",
        (tenant, keys),
    ).fetchall()
    out: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        out.setdefault(str(row[0]), []).append((str(row[1]), float(row[2])))
    return out


def _dedupe_entities(communities: list[CommunityGroup]) -> list[GraphEntity]:
    """Flat, deduped, deterministically-sorted entity list across communities.

    Mirrors :func:`brain.graph_rag.themes._theme_member_entities` so the
    context-level ``entities`` shape matches the themes path.
    """
    by_id: dict[str, GraphEntity] = {}
    for community in communities:
        for entity in community.entities:
            by_id.setdefault(entity.id, entity)
    return sorted(
        by_id.values(), key=lambda e: (e.canonical_key, e.entity_type, e.id)
    )
