"""Fuse retrieval: graph (local) ⊕ vector/FTS (hybrid) via RRF (spec §17d Q1).

The fourth retrieval path alongside **local** (:mod:`brain.graph_rag.retrieve`),
**themes-with-X** (:mod:`brain.graph_rag.themes`), and **global**
(:mod:`brain.graph_rag.global_`). Where those resolve a single ranked unit
(entity-centric docs / theme groups / communities), **fuse** combines two
independent **document** rankings into one (wave G4-c):

* **Graph leg** — the ``local`` (entity-centric) document list
  (:func:`brain.graph_rag.retrieve._retrieve_local`). ``themes`` / ``global`` are
  deliberately NOT fused: their ranked units (theme groups / communities) and
  scoping semantics differ, so they don't fuse cleanly into a doc list (spec
  §17d Q1).
* **Hybrid leg** — the existing vector + FTS document ranking
  (:func:`brain.search.hybrid_search`). Its ranking is **consumed unchanged**
  (the spec §4 D7 invariant — fuse never modifies hybrid search).

The fusion (spec §17d Q1): RRF over each leg's **document-id rank** via
:func:`brain.rank_fusion.rrf_contribution` (``k=60``, the same constant as
:mod:`brain.search` / global / ``build_related``) — **not** a score-blend.
``score = Σ 1/(60 + rank + 1)`` accumulated per ``document_id`` across both legs;
ties broken by ``document_id`` (deterministic). The fused
:class:`~brain.search.SearchResult`s land in ``GraphContext.docs`` (wire-stable —
no new field, no change to ``SearchResult``); ``GraphContext.mode`` and
``GraphExplanation.mode`` are both stamped ``"fuse"``; and per-doc leg provenance
(which leg(s) ranked each returned doc + their 0-indexed ranks) is recorded in
``GraphExplanation.matched_filters["fuse_doc_provenance"]``.

Fallbacks (never-raise; spec §17d Q1, mirroring the §17b dec 7 / §17c Q9
best-effort-optional-leg discipline):

* The **graph leg** is the primary leg: a ``GraphBackendError`` from the
  traversal is **not** swallowed (the backend's complete-or-loud-failure
  contract, identical to ``mode=local``); an empty graph result simply yields no
  graph docs → **hybrid-only**.
* The **hybrid leg** is additive/best-effort: a missing ``embedder_factory`` /
  a failing embedder construction / a failing query embedding → a WARN + the
  hybrid leg runs **FTS-only**; the hybrid leg failing even FTS-only (a DB /
  tsquery error) → a WARN + fuse degrades to **graph-only**.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import psycopg

from ..rank_fusion import rrf_contribution
from .router import FUSE_MODE
from .schema import GraphContext, GraphExplanation

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..config import Config
    from ..ingest import Embedder
    from ..search import SearchResult
    from .backends.base import GraphBackend

_logger = logging.getLogger(__name__)

# RRF k constant — matches :data:`brain.search.RRF_K` and the global path's
# ``_RRF_K`` so every fused ranker shares one fusion constant via the shared
# :func:`brain.rank_fusion.rrf_contribution` helper (spec §17d Q1).
_RRF_K = 60

# Fuse is gated to the default tenant until documents/chunks are tenantized
# (spec §17d decision 6 / G4-review finding P1-1). ``documents`` / ``chunks``
# carry NO ``tenant_id`` column, so fuse's hybrid leg (corpus-wide
# :func:`brain.search.hybrid_search`) would surface another tenant's documents —
# a cross-tenant leak. ``local`` / ``global`` are inherently tenant-scoped (their
# doc-id sets come solely from tenant-predicated ``graph_entity_mentions``) and
# need no gate. Tracked follow-up: tenantize documents/chunks, then remove this.
_DEFAULT_FUSE_TENANT = "default"


class _NullEmbedder:
    """Null Embedder for fuse's FTS-only hybrid fallback (spec §17d Q1).

    When no real embedder can be constructed (``embedder_factory`` absent or
    failing), fuse still runs the hybrid leg **FTS-only**.
    :func:`brain.search.hybrid_search` requires an ``embedder`` argument even in
    ``fts_only`` mode, but with ``fts_only=True`` + ``snippet_context_tokens=0``
    it never calls ``embed`` / ``count_tokens``. This null object satisfies the
    :class:`brain.ingest.Embedder` Protocol structurally so the FTS-only call is
    runnable; its methods are unreachable on that path and raise so any future
    accidental use is loud rather than silently wrong.
    """

    dim = 0

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        raise NotImplementedError("fuse FTS-only hybrid leg must not embed")

    def count_tokens(self, text: str) -> int:
        raise NotImplementedError("fuse FTS-only hybrid leg must not count tokens")


_NULL_EMBEDDER = _NullEmbedder()


def _retrieve_fuse(
    conn: psycopg.Connection[Any],
    cfg: Config,
    query: str,
    *,
    backend: GraphBackend,
    tenant: str,
    depth: int,
    frontier_cap: int,
    min_edge_weight: float,
    limit: int,
    embedder_factory: Callable[[], Embedder] | None = None,
    session_id: str | None = None,
) -> GraphContext:
    """Run fuse (graph ⊕ hybrid) retrieval + assemble its ``GraphContext``.

    Runs the graph leg (``_retrieve_local``) and the hybrid leg
    (:func:`brain.search.hybrid_search`), RRF-merges the two document-id rankings
    (:func:`_fuse_doc_rankings`), takes the top ``limit``, and shapes the fused
    :class:`~brain.search.SearchResult`s into ``GraphContext.docs`` (carrying each
    doc's per-leg provenance in ``GraphExplanation.matched_filters``). The graph
    leg's entities ride along on ``GraphContext.entities`` for context. Caps
    (``depth`` / ``frontier_cap`` / ``min_edge_weight``) feed the graph leg; the
    hybrid leg reads its tuning (``vector_sim_floor`` / ``recency_halflife_days``
    / ``snippet_context_tokens``) from ``cfg``. ``session_id`` is generated when
    omitted (a fresh ``uuid4`` hex).

    Never-raise: an empty graph leg → hybrid-only; a missing/failed embedder →
    FTS-only hybrid; a fully-dead hybrid leg → graph-only (spec §17d Q1). A
    ``GraphBackendError`` from the graph traversal is propagated (the backend's
    complete-or-loud-failure contract, identical to ``mode=local``).

    Tenant gate (spec §17d decision 6 / G4-review P1-1): a non-default ``tenant``
    raises :class:`ValueError` BEFORE either leg runs — the hybrid leg is
    corpus-wide (documents/chunks are not tenantized) so a non-default fuse would
    leak cross-tenant documents. The ``ValueError`` maps cleanly through the
    existing graphrag surfaces (CLI → ``typer.BadParameter`` exit 2; MCP →
    ``INVALID_PARAMS``). ``local`` / ``global`` stay available (inherently scoped).
    """
    # Tenant gate (spec §17d decision 6 / G4-review P1-1): refuse fuse for any
    # non-default tenant BEFORE either leg runs (not empty, not graph-only, not
    # hybrid-filtered) — the hybrid leg queries documents/chunks corpus-wide
    # (they carry no tenant_id), so a non-default fuse would surface another
    # tenant's documents. local/global remain available (inherently scoped).
    if tenant != _DEFAULT_FUSE_TENANT:
        raise ValueError(
            "mode='fuse' is only available for tenant 'default' until "
            "documents/chunks are tenantized; use mode='local' or mode='global' "
            "for graph-scoped retrieval"
        )

    # Lazy import to avoid a retrieve ↔ fuse module-load cycle (retrieve.py
    # imports ``_retrieve_fuse`` at top; this resolves once retrieve is loaded).
    from .retrieve import _retrieve_local

    resolved_session = uuid.uuid4().hex if session_id is None else session_id

    # GRAPH leg (primary): local entity-centric docs.
    graph_ctx = _retrieve_local(
        conn,
        query,
        backend=backend,
        tenant_id=tenant,
        depth=depth,
        frontier_cap=frontier_cap,
        min_edge_weight=min_edge_weight,
        limit=limit,
        session_id=resolved_session,
    )
    graph_docs = graph_ctx.docs

    # HYBRID leg (additive, best-effort): FTS + vector via hybrid_search.
    hybrid_docs, hybrid_vector_arm = _run_hybrid_leg(
        conn, cfg, query, limit=limit, embedder_factory=embedder_factory
    )

    # RRF-merge the two document-id rankings.
    graph_ids = [doc.document_id for doc in graph_docs]
    hybrid_ids = [doc.document_id for doc in hybrid_docs]
    fused, provenance = _fuse_doc_rankings(graph_ids, hybrid_ids)
    top = fused[:limit] if limit > 0 else []
    docs = _build_fused_docs(top, graph_docs, hybrid_docs)
    fuse_doc_provenance = {doc.document_id: provenance[doc.document_id] for doc in docs}

    seed_entity_ids = (
        list(graph_ctx.explanation.seed_entity_ids)
        if graph_ctx.explanation is not None
        else []
    )
    explanation = GraphExplanation(
        mode=FUSE_MODE,
        tenant_id=tenant,
        seed_entity_ids=seed_entity_ids,
        person_keys=[],
        depth=depth,
        frontier_cap=frontier_cap,
        min_edge_weight=min_edge_weight,
        nodes_visited=len(graph_ctx.entities),
        edges_considered=0,
        generic_df_cap=None,
        matched_filters={
            "query": query,
            "graph_doc_count": len(graph_docs),
            "hybrid_doc_count": len(hybrid_docs),
            "hybrid_vector_arm_used": hybrid_vector_arm,
            "fused_doc_count": len(docs),
            "limit": limit,
            "fuse_doc_provenance": fuse_doc_provenance,
        },
    )
    return GraphContext(
        session_id=resolved_session,
        mode=FUSE_MODE,
        query=query,
        tenant_id=tenant,
        person=None,
        themes=[],
        communities=[],
        entities=graph_ctx.entities,
        docs=docs,
        explanation=explanation,
    )


# --------------------------------------------------------------------------- #
# RRF merge (pure) + fused-doc shaping
# --------------------------------------------------------------------------- #
def _fuse_doc_rankings(
    graph_ids: list[str], hybrid_ids: list[str]
) -> tuple[list[tuple[str, float]], dict[str, dict[str, Any]]]:
    """Pure RRF merge of the two document-id rankings (spec §17d Q1).

    Each leg contributes ``rrf_contribution(rank, k=60)`` per ``document_id``
    (0-indexed rank in that leg's order); contributions accumulate across legs.
    Returns ``(fused, provenance)``:

    * ``fused`` — ``[(document_id, score), …]`` sorted by score DESC then
      ``document_id`` ASC (the deterministic tie-break).
    * ``provenance`` — ``document_id → {"graph_rank", "hybrid_rank",
      "fused_score"}`` with 0-indexed ranks (``None`` when the doc is absent from
      that leg).

    Pure (no DB / no ``SearchResult``) so the fusion is unit-testable in
    isolation. ``_retrieve_fuse`` passes the per-leg ``document_id`` orders.
    """
    graph_rank = {doc_id: rank for rank, doc_id in enumerate(graph_ids)}
    hybrid_rank = {doc_id: rank for rank, doc_id in enumerate(hybrid_ids)}

    scores: dict[str, float] = {}
    for rank, doc_id in enumerate(graph_ids):
        scores[doc_id] = scores.get(doc_id, 0.0) + rrf_contribution(rank, k=_RRF_K)
    for rank, doc_id in enumerate(hybrid_ids):
        scores[doc_id] = scores.get(doc_id, 0.0) + rrf_contribution(rank, k=_RRF_K)

    fused = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    provenance = {
        doc_id: {
            "graph_rank": graph_rank.get(doc_id),
            "hybrid_rank": hybrid_rank.get(doc_id),
            "fused_score": score,
        }
        for doc_id, score in fused
    }
    return fused, provenance


def _build_fused_docs(
    ranked: list[tuple[str, float]],
    graph_docs: list[SearchResult],
    hybrid_docs: list[SearchResult],
) -> list[SearchResult]:
    """Shape the fused ranking into ``SearchResult``s (no dataclass change).

    Reuses each leg's :class:`~brain.search.SearchResult` as the carrier —
    preferring the hybrid leg's (its snippet is query-relevant), falling back to
    the graph leg's — and overrides only ``score`` with the fused RRF score
    (immutably, via :func:`dataclasses.replace`). ``explain`` is cleared (the
    fuse provenance lives in ``GraphExplanation.matched_filters`` instead). Order
    preserves the fused ranking.
    """
    hybrid_by_id = {doc.document_id: doc for doc in hybrid_docs}
    graph_by_id = {doc.document_id: doc for doc in graph_docs}
    out: list[SearchResult] = []
    for doc_id, score in ranked:
        carrier = hybrid_by_id.get(doc_id) or graph_by_id.get(doc_id)
        if carrier is None:  # defensive: every fused id came from a leg
            continue
        out.append(replace(carrier, score=score, explain=None))
    return out


# --------------------------------------------------------------------------- #
# Hybrid leg (best-effort; FTS-only / graph-only fallbacks)
# --------------------------------------------------------------------------- #
def _run_hybrid_leg(
    conn: psycopg.Connection[Any],
    cfg: Config,
    query: str,
    *,
    limit: int,
    embedder_factory: Callable[[], Embedder] | None,
) -> tuple[list[SearchResult], bool]:
    """Run the FTS + vector hybrid leg for fuse (spec §17d Q1; never-raise).

    Returns ``(docs, vector_arm_used)``:

    * embedder constructs + the query embeds + the cosine SQL runs → full hybrid
      (``vector_arm_used=True``).
    * embedder absent / construction fails / the vector arm fails → **FTS-only**
      hybrid (``vector_arm_used=False``); the hybrid leg's ranking is consumed
      unchanged (spec §4 D7 — hybrid search is never modified).
    * the FTS-only ``hybrid_search`` itself failing (a DB / tsquery error) →
      ``([], False)`` + a WARN (fuse degrades to graph-only).
    """
    # Late import keeps :mod:`brain.graph_rag` import-cheap + free of the ingest
    # import cycle :mod:`brain.search` pulls in (mirrors ``_build_doc_results``).
    from ..search import hybrid_search

    embedder = _build_leg_embedder(embedder_factory)

    # Vector arm — only when a real embedder constructed.
    if embedder is not None:
        try:
            docs = hybrid_search(
                conn,
                embedder=embedder,
                query=query,
                limit=limit,
                vector_sim_floor=cfg.vector_sim_floor,
                recency_halflife_days=cfg.recency_halflife_days,
                snippet_context_tokens=cfg.snippet_context_tokens,
            )
            return docs, True
        except Exception as exc:  # noqa: BLE001 — never-raise: degrade to FTS-only
            _logger.warning(
                "fuse: hybrid vector leg failed (%s); retrying FTS-only", exc
            )

    # FTS-only arm — no usable embedder OR the vector arm failed above. Use the
    # real embedder when present (its methods are unreachable with fts_only=True +
    # snippet_context_tokens=0), else the null placeholder so the leg still runs.
    fts_embedder = embedder if embedder is not None else _NULL_EMBEDDER
    try:
        docs = hybrid_search(
            conn,
            embedder=fts_embedder,
            query=query,
            limit=limit,
            fts_only=True,
            snippet_context_tokens=0,
        )
    except Exception as exc:  # noqa: BLE001 — hybrid fully dead → graph-only
        _logger.warning(
            "fuse: hybrid FTS leg failed (%s); fuse degrades to graph-only", exc
        )
        return [], False
    return docs, False


def _build_leg_embedder(
    embedder_factory: Callable[[], Embedder] | None,
) -> Embedder | None:
    """Construct the hybrid-leg embedder; ``None`` on absent/failed factory (WARN).

    A ``None`` factory or a factory that raises both degrade the hybrid leg to
    FTS-only (spec §17d Q1 — embedder construction failing still runs FTS-only),
    so this returns ``None`` rather than raising.
    """
    if embedder_factory is None:
        _logger.warning(
            "fuse: no embedder_factory injected; hybrid leg runs FTS-only"
        )
        return None
    try:
        return embedder_factory()
    except Exception as exc:  # noqa: BLE001 — never-raise: degrade to FTS-only
        _logger.warning(
            "fuse: embedder construction failed (%s); hybrid leg runs FTS-only", exc
        )
        return None
