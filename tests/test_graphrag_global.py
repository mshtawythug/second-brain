"""Tests for the GraphRAG global (community-based) retrieval path (wave G3-d).

Covers :func:`brain.graph_rag.global_._retrieve_global`, the pure
:func:`brain.graph_rag.global_._fuse_rrf` RRF helper, and the
``graph_rag_search(mode='global')`` dispatch:

* pure RRF fusion: both legs combine; ties broken by ``community_key``.
* community ranking: a community in BOTH legs outranks a single-leg one; the
  closest vector-leg community ranks first.
* FTS-only degradation (never-raise): a ``None`` ``embedder`` instance, a
  failing embed call, or a failing vector cosine SQL leg (e.g. a dim mismatch
  after a backend swap) → a WARN + the vector leg skipped.
* ``CommunityGroup`` population: representative entities (by ``member_rank``),
  representative ``doc_ids``, ``member_count`` / ``summary`` / ``score``, and
  context-level docs/snippets via the shared ``_build_doc_results``.
* empty (no communities / no summaries) → an empty-but-valid context.
* tenant isolation + deterministic tie-break ordering + the ``limit`` cap.
* a full build → summarize → retrieve integration over the test DB.

All entities/docs are synthetic (Alice/Bob/Carol + P-/Q- keys); no PII; no live
Ollama (a fake enricher) and no live embedder (the deterministic
:class:`tests.conftest.FakeEmbedder`).
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.graph_rag import graph_rag_search
from brain.graph_rag.communities import build_communities
from brain.graph_rag.communities_summary import summarize_communities
from brain.graph_rag.global_ import _fuse_rrf, _retrieve_global
from brain.graph_rag.schema import GraphContext
from tests.conftest import FakeEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# ``graph_communities.summary_embedding`` ships as vector(1024) (migration 013).
# The unit tests seed embeddings directly at that dim (no dim-reconcile), so the
# query embedder must match. The integration test instead lets
# ``summarize_communities`` resize the column to the FakeEmbedder default (4096).
_SUMMARY_DIM = 1024


# --------------------------------------------------------------------------- #
# Test doubles (DI seam — no monkeypatching, no live Ollama / embedder)
# --------------------------------------------------------------------------- #
class _UnusedBackend:
    """GraphBackend placeholder — the global path never calls a backend method."""


class _RaisingEmbedder:
    """Fake embedder whose ``embed`` raises (query-embedding failure case)."""

    dim = _SUMMARY_DIM

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        raise RuntimeError("synthetic embed failure")

    def count_tokens(self, text: str) -> int:
        return 1


class _WrongDimEmbedder:
    """Fake embedder returning a vector whose dim != ``summary_embedding``'s.

    Embedding the query SUCCEEDS but yields an 8-dim vector; the cosine ``<=>``
    SQL then fails with a dimension mismatch against the 1024-dim
    ``summary_embedding`` column. Exercises the FIX-1 guard that the cosine SQL
    *execution* (not just the embed call) degrades to FTS-only.
    """

    dim = 8

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        return [[0.1] * self.dim for _ in texts]

    def count_tokens(self, text: str) -> int:
        return 1


class _EntityNameSummarizer:
    """Fake enricher whose summary is derived from the community's member names.

    Yields a DISTINCT summary per community (each cluster has different members),
    so :func:`summarize_communities` writes distinct summaries + embeddings for
    the integration test.
    """

    model = "fake-model:1b"

    def summarize_group(
        self,
        *,
        person: str | None,
        entity_names: list[str],
        doc_titles: list[str],
    ) -> str | None:
        return "Community covering " + ", ".join(entity_names)


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
def _cfg(**overrides: Any) -> Config:
    params: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "graph_tenant_id": "default",
    }
    params.update(overrides)
    return Config(**params)


def _insert_community(
    conn: psycopg.Connection[Any],
    tenant: str,
    *,
    summary: str | None,
    embedding: list[float] | None = None,
    member_count: int = 0,
) -> str:
    """Insert a ``graph_communities`` row; return its ``community_key``."""
    row = conn.execute(
        "INSERT INTO graph_communities "
        "(tenant_id, source_graph_hash, members_hash, member_count, summary, "
        " summary_embedding) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING community_key::text",
        (tenant, "synthetic-graph-hash", uuid.uuid4().hex, member_count, summary,
         embedding),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_entity(
    conn: psycopg.Connection[Any],
    tenant: str,
    name: str,
    canonical_key: str,
    *,
    entity_type: str = "person",
) -> str:
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        (tenant, entity_type, name, canonical_key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _add_member(
    conn: psycopg.Connection[Any],
    tenant: str,
    community_key: str,
    entity_id: str,
    *,
    member_rank: int = 0,
    member_weight: float = 0.0,
) -> None:
    conn.execute(
        "INSERT INTO graph_community_members "
        "(tenant_id, community_key, entity_id, member_rank, member_weight) "
        "VALUES (%s, %s, %s, %s, %s)",
        (tenant, community_key, entity_id, member_rank, member_weight),
    )


def _insert_document(
    conn: psycopg.Connection[Any], title: str, content: str = "synthetic body"
) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, 'note') RETURNING id::text",
        (title, content, uuid.uuid4().hex),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _add_mention(
    conn: psycopg.Connection[Any], tenant: str, entity_id: str, document_id: str
) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, source) VALUES (%s, %s, %s, 'people')",
        (tenant, entity_id, document_id),
    )


def _add_chunk(
    conn: psycopg.Connection[Any],
    embedder: Any,
    document_id: str,
    content: str,
    *,
    chunk_index: int = 0,
) -> None:
    """Insert one chunk (its ``tsv`` is generated from ``content``)."""
    vec = embedder.embed([content], input_type="document")[0]
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s, %s, %s, %s)",
        (document_id, chunk_index, content, vec),
    )


def _seed_two_clusters(
    conn: psycopg.Connection[Any], tenant: str = "default"
) -> tuple[list[str], list[str]]:
    """Two dense triangles + a weak bridge (mirrors the communities tests)."""
    def _rel(a: str, b: str, weight: float) -> None:
        src, dst = sorted((a, b))
        conn.execute(
            "INSERT INTO graph_relationships "
            "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
            "VALUES (%s, %s, %s, 'co_occurs', %s, 1, 1)",
            (tenant, src, dst, weight),
        )

    c1 = [_insert_entity(conn, tenant, f"P-{i}", f"p-{tenant}-{i}") for i in range(3)]
    c2 = [_insert_entity(conn, tenant, f"Q-{i}", f"q-{tenant}-{i}") for i in range(3)]
    for a, b in [(0, 1), (0, 2), (1, 2)]:
        _rel(c1[a], c1[b], 0.8)
        _rel(c2[a], c2[b], 0.8)
    _rel(c1[2], c2[0], 0.05)  # weak bridge
    return c1, c2


def _community_keys(conn: psycopg.Connection[Any], tenant: str) -> list[str]:
    rows = conn.execute(
        "SELECT community_key::text FROM graph_communities WHERE tenant_id = %s "
        "ORDER BY community_key",
        (tenant,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _community_for_entity(
    conn: psycopg.Connection[Any], tenant: str, entity_id: str
) -> str:
    row = conn.execute(
        "SELECT community_key::text FROM graph_community_members "
        "WHERE tenant_id = %s AND entity_id = %s",
        (tenant, entity_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


# --------------------------------------------------------------------------- #
# Pure RRF fusion (no DB)
# --------------------------------------------------------------------------- #
def test_fuse_rrf_combines_both_legs() -> None:
    # "c-a" appears in both legs → its contributions sum and it ranks first.
    fts = ["c-b", "c-a"]  # b rank0, a rank1
    vec = ["c-a", "c-c"]  # a rank0, c rank1
    fused = _fuse_rrf(fts, vec)
    keys = [key for key, _score in fused]
    assert keys == ["c-a", "c-b", "c-c"]
    score_by_key = dict(fused)
    # c-a = 1/(60+1+1) + 1/(60+0+1); single-leg keys get one contribution.
    assert score_by_key["c-a"] == pytest.approx(1 / 62 + 1 / 61)
    assert score_by_key["c-b"] == pytest.approx(1 / 61)
    assert score_by_key["c-c"] == pytest.approx(1 / 62)


def test_fuse_rrf_tiebreak_by_community_key() -> None:
    # Both keys land at rank 0 of a single leg → equal score → key-asc tie-break.
    fused = _fuse_rrf(["c-z"], ["c-a"])
    assert [key for key, _score in fused] == ["c-a", "c-z"]
    score_by_key = dict(fused)
    assert score_by_key["c-a"] == pytest.approx(score_by_key["c-z"])


def test_fuse_rrf_empty() -> None:
    assert _fuse_rrf([], []) == []


# --------------------------------------------------------------------------- #
# Community ranking (FTS + vector RRF)
# --------------------------------------------------------------------------- #
def test_global_rrf_fuses_both_legs(test_db: psycopg.Connection[Any]) -> None:
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    query = "roadmap"
    query_vec = emb.embed([query], input_type="query")[0]
    far_vec = emb.embed(["unrelated"], input_type="document")[0]

    # A: FTS match ('roadmap') + closest embedding → in BOTH legs at the top.
    a = _insert_community(
        test_db, "default", summary="roadmap planning", embedding=query_vec
    )
    # B: no FTS match + a farther embedding → vector leg only.
    b = _insert_community(
        test_db, "default", summary="budget review", embedding=far_vec
    )

    ctx = _retrieve_global(
        test_db, _cfg(), query, tenant="default", embedder=emb
    )
    assert [c.community_key for c in ctx.communities] == [a, b]
    assert ctx.explanation is not None
    assert ctx.explanation.matched_filters["vector_arm_used"] is True


def test_global_vector_leg_ranks_closest_first(
    test_db: psycopg.Connection[Any],
) -> None:
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    query = "alpha"  # matches neither summary → vector leg decides
    query_vec = emb.embed([query], input_type="query")[0]
    other_vec = emb.embed(["different content"], input_type="document")[0]

    far = _insert_community(
        test_db, "default", summary="topic one", embedding=other_vec
    )
    near = _insert_community(
        test_db, "default", summary="topic two", embedding=query_vec
    )

    ctx = _retrieve_global(
        test_db, _cfg(), query, tenant="default", embedder=emb
    )
    keys = [c.community_key for c in ctx.communities]
    assert keys[0] == near  # exact-match embedding is the closest
    assert set(keys) == {near, far}


# --------------------------------------------------------------------------- #
# FTS-only degradation (never-raise)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("embedder_kind", ["none", "embed_raises"])
def test_global_degrades_to_fts_only_on_embedder_problem(
    test_db: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
    embedder_kind: str,
) -> None:
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    # Both communities carry an embedding; only A matches the FTS query.
    a = _insert_community(
        test_db,
        "default",
        summary="roadmap planning",
        embedding=emb.embed(["roadmap planning"], input_type="document")[0],
    )
    _insert_community(
        test_db,
        "default",
        summary="budget review",
        embedding=emb.embed(["budget review"], input_type="document")[0],
    )

    # perf-T4 G5: the caller now passes a pre-warmed Embedder instance (not a
    # factory). A factory that raises during construction is the caller's
    # problem — the parametrize accordingly drops the former "factory_raises"
    # case; "none" + "embed_raises" remain the only kinds of failure
    # _retrieve_global itself must degrade through.
    embedder = None if embedder_kind == "none" else _RaisingEmbedder()

    with caplog.at_level(logging.WARNING, logger="brain.graph_rag.global_"):
        ctx = _retrieve_global(
            test_db, _cfg(), "roadmap", tenant="default", embedder=embedder
        )

    # Vector leg skipped → only the FTS-matching community surfaces; never raises.
    assert [c.community_key for c in ctx.communities] == [a]
    assert ctx.explanation is not None
    assert ctx.explanation.matched_filters["vector_arm_used"] is False
    assert ctx.explanation.matched_filters["vector_candidate_count"] == 0
    assert "FTS-only" in caplog.text


def test_global_vector_sql_dim_mismatch_degrades_to_fts_only(
    test_db: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FIX 1: a cosine-SQL failure (dim mismatch) degrades to FTS-only, never raises.

    The communities carry 1024-dim ``summary_embedding`` vectors (the migration
    default); the injected embedder embeds the query fine but returns an 8-dim
    vector, so the ``<=>`` cosine SQL fails with a dimension mismatch. Regression
    guard: the cosine SQL used to run OUTSIDE the never-raise try/except, so a
    backend swapped without a community rebuild would RAISE and break global
    retrieval. With FIX 1 the SQL is inside the guard → global degrades to
    FTS-only (``vector_arm_used`` False) instead of raising.
    """
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    a = _insert_community(
        test_db,
        "default",
        summary="roadmap planning",
        embedding=emb.embed(["roadmap planning"], input_type="document")[0],
    )
    _insert_community(
        test_db,
        "default",
        summary="budget review",
        embedding=emb.embed(["budget review"], input_type="document")[0],
    )

    with caplog.at_level(logging.WARNING, logger="brain.graph_rag.global_"):
        ctx = _retrieve_global(
            test_db,
            _cfg(),
            "roadmap",
            tenant="default",
            embedder=_WrongDimEmbedder(),
        )

    # The cosine SQL raised a dim mismatch → vector leg skipped; only the
    # FTS-matching community surfaces; the call never raises.
    assert [c.community_key for c in ctx.communities] == [a]
    assert ctx.explanation is not None
    assert ctx.explanation.matched_filters["vector_arm_used"] is False
    assert ctx.explanation.matched_filters["vector_candidate_count"] == 0
    assert "FTS-only" in caplog.text


# --------------------------------------------------------------------------- #
# CommunityGroup population
# --------------------------------------------------------------------------- #
def test_global_populates_community_group(
    test_db: psycopg.Connection[Any],
) -> None:
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    query = "roadmap"
    query_vec = emb.embed([query], input_type="query")[0]
    key = _insert_community(
        test_db, "default", summary="roadmap topic", embedding=query_vec,
        member_count=3,
    )
    e0 = _insert_entity(test_db, "default", "Alice", "alice")
    e1 = _insert_entity(test_db, "default", "Bob", "bob")
    e2 = _insert_entity(test_db, "default", "Carol", "carol")
    _add_member(test_db, "default", key, e0, member_rank=0)
    _add_member(test_db, "default", key, e1, member_rank=1)
    _add_member(test_db, "default", key, e2, member_rank=2)
    d1 = _insert_document(test_db, "Doc One")
    d2 = _insert_document(test_db, "Doc Two")
    for entity in (e0, e1, e2):  # d1 mentions all three (count 3)
        _add_mention(test_db, "default", entity, d1)
    _add_mention(test_db, "default", e0, d2)  # d2 mentions one (count 1)

    ctx = _retrieve_global(
        test_db, _cfg(), query, tenant="default", embedder=emb
    )
    assert len(ctx.communities) == 1
    group = ctx.communities[0]
    assert group.community_key == key
    assert group.level == 0
    assert group.member_count == 3
    assert group.summary == "roadmap topic"
    assert group.score > 0.0
    # Entities ordered by member_rank.
    assert [e.id for e in group.entities] == [e0, e1, e2]
    # Representative docs ranked by mention count (d1 > d2).
    assert group.doc_ids == [d1, d2]
    # Context-level entities deduped across communities.
    assert {e.id for e in ctx.entities} == {e0, e1, e2}
    # Context-level docs reuse SearchResult.
    assert {r.document_id for r in ctx.docs} == {d1, d2}


def test_global_docs_have_snippets(test_db: psycopg.Connection[Any]) -> None:
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    chunk_emb = FakeEmbedder()  # dim 4096 — matches chunks.embedding
    query = "roadmap"
    query_vec = emb.embed([query], input_type="query")[0]
    key = _insert_community(
        test_db, "default", summary="roadmap topic", embedding=query_vec,
        member_count=1,
    )
    e0 = _insert_entity(test_db, "default", "Alice", "alice")
    _add_member(test_db, "default", key, e0, member_rank=0)
    d1 = _insert_document(test_db, "Doc One")
    _add_mention(test_db, "default", e0, d1)
    _add_chunk(test_db, chunk_emb, d1, "Detailed roadmap planning discussion.")

    ctx = _retrieve_global(
        test_db, _cfg(), query, tenant="default", embedder=emb
    )
    doc = next(r for r in ctx.docs if r.document_id == d1)
    assert doc.title == "Doc One"
    assert doc.snippet  # non-empty — reuses the _build_doc_results snippet path
    assert "roadmap" in doc.snippet.lower()


# --------------------------------------------------------------------------- #
# Empty (never-raise) cases
# --------------------------------------------------------------------------- #
def test_global_empty_when_no_summaries(
    test_db: psycopg.Connection[Any],
) -> None:
    # A community with no summary + no embedding is in neither leg.
    _insert_community(test_db, "default", summary=None, embedding=None)
    ctx = _retrieve_global(
        test_db, _cfg(), "roadmap", tenant="default",
        embedder=FakeEmbedder(dim=_SUMMARY_DIM),
    )
    assert isinstance(ctx, GraphContext)
    assert ctx.mode == "global"
    assert ctx.communities == []
    assert ctx.docs == []
    assert ctx.entities == []
    assert ctx.explanation is not None


def test_global_empty_when_no_communities(
    test_db: psycopg.Connection[Any],
) -> None:
    ctx = _retrieve_global(
        test_db, _cfg(), "roadmap", tenant="default",
        embedder=FakeEmbedder(dim=_SUMMARY_DIM),
    )
    assert ctx.mode == "global"
    assert ctx.communities == []
    assert ctx.docs == []


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #
def test_global_is_tenant_isolated(test_db: psycopg.Connection[Any]) -> None:
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    query_vec = emb.embed(["roadmap"], input_type="query")[0]
    mine = _insert_community(
        test_db, "default", summary="roadmap default", embedding=query_vec
    )
    theirs = _insert_community(
        test_db, "other", summary="roadmap other", embedding=query_vec
    )

    ctx = _retrieve_global(
        test_db, _cfg(), "roadmap", tenant="default", embedder=emb
    )
    keys = {c.community_key for c in ctx.communities}
    assert mine in keys
    assert theirs not in keys


# --------------------------------------------------------------------------- #
# Deterministic tie-break + limit
# --------------------------------------------------------------------------- #
def test_global_deterministic_tiebreak_by_key(
    test_db: psycopg.Connection[Any],
) -> None:
    # Identical summaries + FTS-only → equal ts_rank → ordered by community_key.
    summary = "roadmap planning identical"
    a = _insert_community(test_db, "default", summary=summary, embedding=None)
    b = _insert_community(test_db, "default", summary=summary, embedding=None)

    first = _retrieve_global(
        test_db, _cfg(), "roadmap", tenant="default", embedder=None
    )
    keys = [c.community_key for c in first.communities]
    assert keys == sorted([a, b])

    # Re-running yields byte-identical ordering (determinism).
    second = _retrieve_global(
        test_db, _cfg(), "roadmap", tenant="default", embedder=None
    )
    assert [c.community_key for c in second.communities] == keys


def test_global_respects_community_limit(
    test_db: psycopg.Connection[Any],
) -> None:
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    query_vec = emb.embed(["roadmap"], input_type="query")[0]
    for i in range(3):
        _insert_community(
            test_db, "default", summary=f"roadmap topic {i}", embedding=query_vec
        )

    capped = _retrieve_global(
        test_db, _cfg(graph_community_limit=1), "roadmap", tenant="default",
        embedder=emb,
    )
    assert len(capped.communities) == 1

    # An explicit ``limit`` overrides the config default.
    override = _retrieve_global(
        test_db, _cfg(graph_community_limit=1), "roadmap", tenant="default",
        limit=2, embedder=emb,
    )
    assert len(override.communities) == 2


# --------------------------------------------------------------------------- #
# graph_rag_search dispatch
# --------------------------------------------------------------------------- #
def test_graph_rag_search_dispatches_to_global(
    test_db: psycopg.Connection[Any],
) -> None:
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    query_vec = emb.embed(["roadmap"], input_type="query")[0]
    a = _insert_community(
        test_db, "default", summary="roadmap topic", embedding=query_vec
    )

    ctx = graph_rag_search(
        test_db, _cfg(), "roadmap", backend=_UnusedBackend(), mode="global",
        tenant="default", embedder=emb,
    )
    assert ctx.mode == "global"
    assert [c.community_key for c in ctx.communities] == [a]
    assert ctx.session_id  # generated when omitted


def test_graph_rag_search_global_no_longer_raises_without_embedder(
    test_db: psycopg.Connection[Any],
) -> None:
    # Explicit global with NO embedder now WORKS (FTS-only) instead of
    # raising GraphModeUnavailable (the G3-d behavior change).
    a = _insert_community(
        test_db, "default", summary="roadmap topic", embedding=None
    )
    ctx = graph_rag_search(
        test_db, _cfg(), "roadmap", backend=_UnusedBackend(), mode="global",
        tenant="default",
    )
    assert ctx.mode == "global"
    assert [c.community_key for c in ctx.communities] == [a]


def test_graph_rag_search_global_honors_session_id(
    test_db: psycopg.Connection[Any],
) -> None:
    ctx = graph_rag_search(
        test_db, _cfg(), "roadmap", backend=_UnusedBackend(), mode="global",
        tenant="default", session_id="sess-123",
    )
    assert ctx.session_id == "sess-123"


# --------------------------------------------------------------------------- #
# Integration: build → summarize → retrieve
# --------------------------------------------------------------------------- #
def test_global_integration_build_summarize_retrieve(
    test_db: psycopg.Connection[Any],
) -> None:
    c1, c2 = _seed_two_clusters(test_db)
    d1 = _insert_document(test_db, "Cluster One Doc")
    d2 = _insert_document(test_db, "Cluster Two Doc")
    for entity in c1:
        _add_mention(test_db, "default", entity, d1)
    for entity in c2:
        _add_mention(test_db, "default", entity, d2)
    _add_chunk(test_db, FakeEmbedder(), d1, "Cluster one discussion body.")
    _add_chunk(test_db, FakeEmbedder(), d2, "Cluster two discussion body.")

    build_communities(test_db, _cfg(), tenant="default")
    summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=_EntityNameSummarizer(), embedder=FakeEmbedder(),
    )

    # The summarize pass resized summary_embedding to the FakeEmbedder dim (4096),
    # so the query embedder must match — use the default FakeEmbedder.
    ctx = graph_rag_search(
        test_db, _cfg(), "Cluster", backend=_UnusedBackend(), mode="global",
        tenant="default", embedder=FakeEmbedder(),
    )
    assert ctx.mode == "global"
    # Both communities carry an embedding → both surface via the vector leg.
    assert {c.community_key for c in ctx.communities} == set(
        _community_keys(test_db, "default")
    )

    key1 = _community_for_entity(test_db, "default", c1[0])
    key2 = _community_for_entity(test_db, "default", c2[0])
    group1 = next(c for c in ctx.communities if c.community_key == key1)
    group2 = next(c for c in ctx.communities if c.community_key == key2)
    assert d1 in group1.doc_ids  # cluster-one community → cluster-one doc
    assert d2 in group2.doc_ids
    assert {r.document_id for r in ctx.docs} >= {d1, d2}
