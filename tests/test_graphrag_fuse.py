"""Tests for the GraphRAG fuse retrieval path (wave G4-c; spec §17d Q1).

Covers :mod:`brain.graph_rag.fuse` — the RRF merge of the graph (``local``) doc
leg with the vector/FTS (``hybrid_search``) doc leg — and the
``graph_rag_search(mode='fuse')`` dispatch:

* **pure RRF merge** (no DB): :func:`brain.graph_rag.fuse._fuse_doc_rankings`
  combines two doc-id rankings via ``rrf_contribution(rank, k=60)``; ties broken
  by ``document_id``; provenance map records each leg's 0-indexed rank.
* **fused-doc shaping** (:func:`brain.graph_rag.fuse._build_fused_docs`):
  hybrid-leg carrier preferred (query-relevant snippet), score overridden with
  the fused RRF score, order preserved.
* **integration** (real Postgres relational side + a recording
  ``FakeTraversalBackend`` for the graph leg + the deterministic
  :class:`tests.conftest.FakeEmbedder` for the hybrid leg): fuse returns a merged
  ranked doc list with per-doc provenance.
* **fallbacks (never-raise)**: no ``embedder_factory`` → graph + FTS-only hybrid;
  an empty graph leg → hybrid-only; a fully-dead hybrid leg → graph-only; an
  all-empty query → an empty-but-valid context.

All entities/docs are synthetic (alpha / beta / roadmap); no PII; no live
Ollama; no live embedder.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any
from unittest import mock

import psycopg
import pytest

from brain.config import Config
from brain.graph_rag import graph_rag_search
from brain.graph_rag.backends.base import TraversalHit
from brain.graph_rag.fuse import (
    FUSE_MODE,
    _build_fused_docs,
    _fuse_doc_rankings,
    _retrieve_fuse,
)
from brain.search import SearchResult

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


# --------------------------------------------------------------------------- #
# Config + backend fakes
# --------------------------------------------------------------------------- #
def _make_cfg(**overrides: Any) -> Config:
    """A minimal :class:`Config` for the fuse caps + tenant (mirrors retrieve)."""
    params: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "graph_tenant_id": "default",
        "graph_depth": 2,
        "graph_frontier_cap": 200,
        "graph_min_edge_weight": 0.2,
    }
    params.update(overrides)
    return Config(**params)


class FakeTraversalBackend:
    """Records ``traverse`` calls; returns canned hits per seed (no AGE).

    Only ``traverse`` is exercised by the graph (local) leg, so that is the only
    Protocol method this fake needs (mirrors ``test_graphrag_retrieve``).
    """

    def __init__(
        self, hits_by_seed: dict[str, list[TraversalHit]] | None = None
    ) -> None:
        self.hits_by_seed: dict[str, list[TraversalHit]] = hits_by_seed or {}
        self.calls: list[str] = []

    def traverse(
        self,
        conn: Any,
        tenant_id: str,
        seed_entity_uuid: str,
        *,
        depth: int,
        frontier_cap: int,
        min_edge_weight: float = 0.0,
    ) -> list[TraversalHit]:
        self.calls.append(seed_entity_uuid)
        return list(self.hits_by_seed.get(seed_entity_uuid, []))


def _hit(entity_uuid: str, affinity: float, *, hops: int = 1) -> TraversalHit:
    return TraversalHit(
        entity_uuid=entity_uuid, affinity=affinity, hops=hops, tenant_id="default"
    )


# --------------------------------------------------------------------------- #
# Relational seeding helpers (mirror test_graphrag_retrieve)
# --------------------------------------------------------------------------- #
def _insert_entity(
    conn: psycopg.Connection[Any],
    *,
    name: str,
    canonical_key: str,
    tenant: str = "default",
    entity_type: str = "topic",
    doc_count: int = 0,
) -> str:
    row = conn.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, doc_count) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
        (tenant, entity_type, name, canonical_key, doc_count),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_mention(
    conn: psycopg.Connection[Any],
    *,
    entity_id: str,
    document_id: str,
    tenant: str = "default",
) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) "
        "VALUES (%s, %s, %s, 1, 'people')",
        (tenant, entity_id, document_id),
    )


def _insert_doc(
    conn: psycopg.Connection[Any], *, title: str, content: str
) -> str:
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('manual', %s, '{}'::jsonb) RETURNING id",
        (uuid.uuid4().hex,),
    ).fetchone()
    assert src_row is not None
    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        "INSERT INTO documents (source_id, title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s, 'note') RETURNING id::text",
        (src_row[0], title, content, content_hash),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _add_chunk(
    conn: psycopg.Connection[Any],
    embedder: Any,
    document_id: str,
    content: str,
    *,
    chunk_index: int = 0,
) -> None:
    vec = embedder.embed([content], input_type="document")[0]
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s, %s, %s, %s)",
        (document_id, chunk_index, content, vec),
    )


def _doc(doc_id: str, *, snippet: str = "", score: float = 0.0) -> SearchResult:
    """A minimal :class:`SearchResult` carrier for the pure shaping unit tests."""
    return SearchResult(
        document_id=doc_id,
        title=f"title-{doc_id}",
        source_kind="manual",
        snippet=snippet,
        score=score,
        content_type="note",
        tags=[],
    )


# --------------------------------------------------------------------------- #
# 1. Pure RRF merge (no DB)
# --------------------------------------------------------------------------- #
def test_fuse_doc_rankings_combines_both_legs() -> None:
    # "b" appears in both legs → its contributions sum and it ranks first.
    graph_ids = ["a", "b"]  # a rank0, b rank1
    hybrid_ids = ["b", "c"]  # b rank0, c rank1
    fused, provenance = _fuse_doc_rankings(graph_ids, hybrid_ids)

    assert [doc_id for doc_id, _ in fused] == ["b", "a", "c"]
    score_by_id = dict(fused)
    assert score_by_id["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert score_by_id["a"] == pytest.approx(1 / 61)
    assert score_by_id["c"] == pytest.approx(1 / 62)
    # Provenance records each leg's 0-indexed rank (None when absent).
    assert provenance["b"] == {
        "graph_rank": 1,
        "hybrid_rank": 0,
        "fused_score": pytest.approx(1 / 62 + 1 / 61),
    }
    assert provenance["a"]["graph_rank"] == 0
    assert provenance["a"]["hybrid_rank"] is None
    assert provenance["c"]["graph_rank"] is None
    assert provenance["c"]["hybrid_rank"] == 1


def test_fuse_doc_rankings_tiebreak_by_doc_id() -> None:
    # Both at rank 0 of a single leg → equal score → doc-id-asc tie-break.
    fused, _ = _fuse_doc_rankings(["z"], ["a"])
    assert [doc_id for doc_id, _ in fused] == ["a", "z"]
    score_by_id = dict(fused)
    assert score_by_id["a"] == pytest.approx(score_by_id["z"])


def test_fuse_doc_rankings_empty() -> None:
    fused, provenance = _fuse_doc_rankings([], [])
    assert fused == []
    assert provenance == {}


def test_fuse_doc_rankings_single_leg() -> None:
    """A doc only in the graph leg keeps a None hybrid_rank (and vice versa)."""
    fused, provenance = _fuse_doc_rankings(["a", "b"], [])
    assert [doc_id for doc_id, _ in fused] == ["a", "b"]
    assert provenance["a"] == {
        "graph_rank": 0,
        "hybrid_rank": None,
        "fused_score": pytest.approx(1 / 61),
    }


# --------------------------------------------------------------------------- #
# 2. Fused-doc shaping
# --------------------------------------------------------------------------- #
def test_build_fused_docs_prefers_hybrid_carrier_and_overrides_score() -> None:
    graph_docs = [_doc("a", snippet="graph snippet", score=1.8)]
    hybrid_docs = [_doc("a", snippet="hybrid snippet", score=0.5)]
    ranked = [("a", 0.0328)]

    out = _build_fused_docs(ranked, graph_docs, hybrid_docs)

    assert len(out) == 1
    # Hybrid carrier preferred (query-relevant snippet); score is the fused score.
    assert out[0].snippet == "hybrid snippet"
    assert out[0].score == pytest.approx(0.0328)
    assert out[0].explain is None


def test_build_fused_docs_falls_back_to_graph_carrier() -> None:
    graph_docs = [_doc("g", snippet="graph only")]
    hybrid_docs: list[SearchResult] = []
    out = _build_fused_docs([("g", 0.0164)], graph_docs, hybrid_docs)
    assert out[0].snippet == "graph only"
    assert out[0].score == pytest.approx(0.0164)


def test_build_fused_docs_preserves_ranked_order() -> None:
    graph_docs = [_doc("a"), _doc("b")]
    hybrid_docs = [_doc("c")]
    ranked = [("c", 0.9), ("a", 0.5), ("b", 0.1)]
    out = _build_fused_docs(ranked, graph_docs, hybrid_docs)
    assert [d.document_id for d in out] == ["c", "a", "b"]


# --------------------------------------------------------------------------- #
# 3. Integration — both legs merge (real DB; fake backend + fake embedder)
# --------------------------------------------------------------------------- #
def test_fuse_merges_graph_and_hybrid(test_db: psycopg.Connection[Any]) -> None:
    from tests.conftest import FakeEmbedder

    emb = FakeEmbedder()
    alpha = _insert_entity(test_db, name="alpha", canonical_key="alpha", doc_count=2)
    beta = _insert_entity(test_db, name="beta", canonical_key="beta")

    doc_a = _insert_doc(test_db, title="A", content="alpha and beta roadmap")
    doc_b = _insert_doc(test_db, title="B", content="alpha standalone note")
    doc_c = _insert_doc(test_db, title="C", content="alpha appears but no graph link")
    # Graph mentions: doc_a (alpha+beta), doc_b (alpha). doc_c has NO mention.
    _insert_mention(test_db, entity_id=alpha, document_id=doc_a)
    _insert_mention(test_db, entity_id=beta, document_id=doc_a)
    _insert_mention(test_db, entity_id=alpha, document_id=doc_b)
    # Chunks back the hybrid (FTS+vector) leg — every doc mentions "alpha".
    for doc_id, content in (
        (doc_a, "alpha and beta roadmap"),
        (doc_b, "alpha standalone note"),
        (doc_c, "alpha appears but no graph link"),
    ):
        _add_chunk(test_db, emb, doc_id, content)

    backend = FakeTraversalBackend(hits_by_seed={alpha: [_hit(beta, 0.8)]})
    ctx = _retrieve_fuse(
        test_db,
        _make_cfg(),
        "alpha",
        backend=backend,
        tenant="default",
        depth=2,
        frontier_cap=200,
        min_edge_weight=0.2,
        limit=10,
        embedder_factory=lambda: emb,
    )

    assert ctx.mode == FUSE_MODE
    assert ctx.explanation is not None
    assert ctx.explanation.mode == FUSE_MODE
    doc_ids = [d.document_id for d in ctx.docs]
    # doc_a + doc_b are in BOTH legs (graph contribution + hybrid contribution) →
    # they always outrank the hybrid-only doc_c (a single-leg doc). The exact
    # order of doc_a vs doc_b depends on the hybrid ranking, so assert the set.
    assert set(doc_ids[:2]) == {doc_a, doc_b}
    # doc_c is hybrid-only but still merges into the fused list (ranked last here).
    assert doc_c in doc_ids
    assert doc_ids[-1] == doc_c

    prov = ctx.explanation.matched_filters["fuse_doc_provenance"]
    assert prov[doc_a]["graph_rank"] is not None
    assert prov[doc_a]["hybrid_rank"] is not None
    assert prov[doc_c]["graph_rank"] is None
    assert prov[doc_c]["hybrid_rank"] is not None
    assert ctx.explanation.matched_filters["hybrid_vector_arm_used"] is True
    # Graph leg's entities ride along for context (seed alpha + reached beta).
    assert {e.id for e in ctx.entities} == {alpha, beta}


def test_fuse_dispatch_via_graph_rag_search(
    test_db: psycopg.Connection[Any],
) -> None:
    from tests.conftest import FakeEmbedder

    emb = FakeEmbedder()
    alpha = _insert_entity(test_db, name="alpha", canonical_key="alpha")
    doc_a = _insert_doc(test_db, title="A", content="alpha roadmap")
    _insert_mention(test_db, entity_id=alpha, document_id=doc_a)
    _add_chunk(test_db, emb, doc_a, "alpha roadmap")

    backend = FakeTraversalBackend()
    ctx = graph_rag_search(
        test_db,
        _make_cfg(),
        "alpha",
        backend=backend,
        mode="fuse",
        tenant="default",
        embedder_factory=lambda: emb,
    )
    assert ctx.mode == FUSE_MODE
    assert doc_a in [d.document_id for d in ctx.docs]


# --------------------------------------------------------------------------- #
# 4. Fallbacks (never-raise)
# --------------------------------------------------------------------------- #
def test_fuse_no_embedder_degrades_to_fts_only(
    test_db: psycopg.Connection[Any],
) -> None:
    """No embedder_factory → graph + FTS-only hybrid (vector arm skipped)."""
    from tests.conftest import FakeEmbedder

    emb = FakeEmbedder()
    alpha = _insert_entity(test_db, name="alpha", canonical_key="alpha")
    doc_a = _insert_doc(test_db, title="A", content="alpha roadmap")
    doc_c = _insert_doc(test_db, title="C", content="alpha only in hybrid")
    _insert_mention(test_db, entity_id=alpha, document_id=doc_a)
    _add_chunk(test_db, emb, doc_a, "alpha roadmap")
    _add_chunk(test_db, emb, doc_c, "alpha only in hybrid")

    ctx = _retrieve_fuse(
        test_db,
        _make_cfg(),
        "alpha",
        backend=FakeTraversalBackend(),
        tenant="default",
        depth=2,
        frontier_cap=200,
        min_edge_weight=0.2,
        limit=10,
        embedder_factory=None,
    )

    assert ctx.mode == FUSE_MODE
    assert ctx.explanation is not None
    assert ctx.explanation.matched_filters["hybrid_vector_arm_used"] is False
    doc_ids = [d.document_id for d in ctx.docs]
    # FTS-only hybrid still ranked + merged: both the graph doc and the
    # hybrid-only doc appear (never-raise; spec §17d Q1).
    assert doc_a in doc_ids
    assert doc_c in doc_ids


def test_fuse_empty_graph_leg_is_hybrid_only(
    test_db: psycopg.Connection[Any],
) -> None:
    """A query that resolves no seed → graph leg empty → hybrid-only."""
    from tests.conftest import FakeEmbedder

    emb = FakeEmbedder()
    # An entity exists, but the query 'roadmap' does not match it → no seed.
    _insert_entity(test_db, name="alpha", canonical_key="alpha")
    doc_r = _insert_doc(test_db, title="R", content="the roadmap planning session")
    _add_chunk(test_db, emb, doc_r, "the roadmap planning session")

    ctx = _retrieve_fuse(
        test_db,
        _make_cfg(),
        "roadmap",
        backend=FakeTraversalBackend(),
        tenant="default",
        depth=2,
        frontier_cap=200,
        min_edge_weight=0.2,
        limit=10,
        embedder_factory=lambda: emb,
    )

    assert ctx.mode == FUSE_MODE
    assert ctx.explanation is not None
    assert ctx.explanation.matched_filters["graph_doc_count"] == 0
    doc_ids = [d.document_id for d in ctx.docs]
    assert doc_ids == [doc_r]
    prov = ctx.explanation.matched_filters["fuse_doc_provenance"]
    assert prov[doc_r]["graph_rank"] is None
    assert prov[doc_r]["hybrid_rank"] is not None


def test_fuse_hybrid_dead_is_graph_only(
    test_db: psycopg.Connection[Any],
) -> None:
    """A fully-dead hybrid leg (hybrid_search raises) → graph-only + never-raise.

    Uses ``unittest.mock.patch`` (a standard test double with automatic cleanup —
    not monkeypatching per CLAUDE.md rule 13) to force ``hybrid_search`` to raise
    in BOTH the vector and FTS-only arms, the only way to exercise the
    "hybrid leg cannot run at all" branch.
    """
    from tests.conftest import FakeEmbedder

    emb = FakeEmbedder()
    alpha = _insert_entity(test_db, name="alpha", canonical_key="alpha")
    doc_a = _insert_doc(test_db, title="A", content="alpha roadmap")
    _insert_mention(test_db, entity_id=alpha, document_id=doc_a)
    _add_chunk(test_db, emb, doc_a, "alpha roadmap")

    with mock.patch(
        "brain.search.hybrid_search", side_effect=RuntimeError("synthetic FTS failure")
    ):
        ctx = _retrieve_fuse(
            test_db,
            _make_cfg(),
            "alpha",
            backend=FakeTraversalBackend(),
            tenant="default",
            depth=2,
            frontier_cap=200,
            min_edge_weight=0.2,
            limit=10,
            embedder_factory=lambda: emb,
        )

    assert ctx.mode == FUSE_MODE
    assert ctx.explanation is not None
    assert ctx.explanation.matched_filters["hybrid_doc_count"] == 0
    assert ctx.explanation.matched_filters["hybrid_vector_arm_used"] is False
    # Graph-only: only the graph-mentioned doc survives.
    assert [d.document_id for d in ctx.docs] == [doc_a]
    prov = ctx.explanation.matched_filters["fuse_doc_provenance"]
    assert prov[doc_a]["graph_rank"] == 0
    assert prov[doc_a]["hybrid_rank"] is None


def test_fuse_all_empty_returns_empty_context(
    test_db: psycopg.Connection[Any],
) -> None:
    """No seed + no docs → an empty-but-valid fuse context (never-raise)."""
    from tests.conftest import FakeEmbedder

    ctx = _retrieve_fuse(
        test_db,
        _make_cfg(),
        "nothing-here-xyz",
        backend=FakeTraversalBackend(),
        tenant="default",
        depth=2,
        frontier_cap=200,
        min_edge_weight=0.2,
        limit=10,
        embedder_factory=lambda: FakeEmbedder(),
    )

    assert ctx.mode == FUSE_MODE
    assert ctx.docs == []
    assert ctx.entities == []
    assert ctx.themes == []
    assert ctx.communities == []
    assert ctx.explanation is not None
    assert ctx.explanation.matched_filters["fused_doc_count"] == 0
    assert ctx.explanation.matched_filters["fuse_doc_provenance"] == {}


def test_fuse_limit_caps_returned_docs(
    test_db: psycopg.Connection[Any],
) -> None:
    from tests.conftest import FakeEmbedder

    emb = FakeEmbedder()
    alpha = _insert_entity(test_db, name="alpha", canonical_key="alpha")
    docs = [
        _insert_doc(test_db, title=str(i), content=f"alpha doc {i}") for i in range(4)
    ]
    for doc_id in docs:
        _insert_mention(test_db, entity_id=alpha, document_id=doc_id)
        _add_chunk(test_db, emb, doc_id, "alpha content")

    ctx = _retrieve_fuse(
        test_db,
        _make_cfg(),
        "alpha",
        backend=FakeTraversalBackend(),
        tenant="default",
        depth=2,
        frontier_cap=200,
        min_edge_weight=0.2,
        limit=2,
        embedder_factory=lambda: emb,
    )
    assert len(ctx.docs) == 2


def test_fuse_session_id_honored(test_db: psycopg.Connection[Any]) -> None:
    from tests.conftest import FakeEmbedder

    ctx = _retrieve_fuse(
        test_db,
        _make_cfg(),
        "alpha",
        backend=FakeTraversalBackend(),
        tenant="default",
        depth=2,
        frontier_cap=200,
        min_edge_weight=0.2,
        limit=10,
        embedder_factory=lambda: FakeEmbedder(),
        session_id="fixed-session-123",
    )
    assert ctx.session_id == "fixed-session-123"


# --------------------------------------------------------------------------- #
# 5. Tenant gate (spec §17d decision 6 / G4-review finding P1-1)
# --------------------------------------------------------------------------- #
def test_fuse_non_default_tenant_rejects_before_either_leg(
    test_db: psycopg.Connection[Any],
) -> None:
    """A non-default tenant raises BEFORE either leg runs (no cross-tenant leak).

    Regression for G4-review P1-1: ``documents``/``chunks`` are NOT tenantized, so
    fuse's hybrid leg (``hybrid_search``) is corpus-wide. ``_retrieve_fuse`` must
    refuse a non-default tenant BEFORE the graph leg traverses OR the hybrid leg
    queries the corpus — proven here by a recording backend that stays un-called
    and a ``hybrid_search`` mock that is never invoked.
    """
    from tests.conftest import FakeEmbedder

    backend = FakeTraversalBackend(hits_by_seed={"seed": [_hit("other", 0.5)]})
    with (
        mock.patch("brain.search.hybrid_search") as hybrid,
        pytest.raises(ValueError, match="only available for tenant 'default'"),
    ):
        _retrieve_fuse(
            test_db,
            _make_cfg(),
            "alpha",
            backend=backend,
            tenant="other",
            depth=2,
            frontier_cap=200,
            min_edge_weight=0.2,
            limit=10,
            embedder_factory=lambda: FakeEmbedder(),
        )
    # Neither leg ran: the graph backend was never traversed and the hybrid leg
    # never queried the corpus-wide documents/chunks.
    assert backend.calls == []
    assert hybrid.call_count == 0


def test_fuse_default_tenant_still_runs(test_db: psycopg.Connection[Any]) -> None:
    """The gate is exact: ``tenant='default'`` still runs both legs (no false reject)."""
    from tests.conftest import FakeEmbedder

    emb = FakeEmbedder()
    alpha = _insert_entity(test_db, name="alpha", canonical_key="alpha")
    doc_a = _insert_doc(test_db, title="A", content="alpha roadmap")
    _insert_mention(test_db, entity_id=alpha, document_id=doc_a)
    _add_chunk(test_db, emb, doc_a, "alpha roadmap")

    ctx = _retrieve_fuse(
        test_db,
        _make_cfg(),
        "alpha",
        backend=FakeTraversalBackend(),
        tenant="default",
        depth=2,
        frontier_cap=200,
        min_edge_weight=0.2,
        limit=10,
        embedder_factory=lambda: emb,
    )
    assert ctx.mode == FUSE_MODE
    assert doc_a in [d.document_id for d in ctx.docs]
