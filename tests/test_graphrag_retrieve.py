"""Tests for ``brain.graph_rag.retrieve`` — local (entity-centric) retrieval (G2-d).

Two layers:

* **Orchestration units** (real Postgres relational side + a recording
  ``FakeTraversalBackend`` injected for the AGE traversal): proves the local
  path depends only on the ``GraphBackend.traverse`` seam (dependency
  inversion). Exercises seed resolution (exact / name / substring / miss /
  tie-break), cap injection, document ranking, ``GraphContext`` /
  ``GraphExplanation`` assembly, the never-raise-on-empty contract, the
  non-local ``mode`` guard, and ``GraphBackendError`` propagation. These need a
  real DB for the seed / mention / snippet SQL but no live AGE.
* **Live-AGE integration** (``test_db`` against the AGE test instance on port
  5434): builds a small synthetic person graph via ``reconcile_document`` + the
  real ``AgeBackend``, then runs ``graph_rag_search`` and asserts the reached
  entities, ranked docs, snippets, and tenant scoping (a second tenant's data
  never leaks).

All people / entities are synthetic (alice / bob / carol); no PII.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.errors import GraphBackendError
from brain.graph_rag import graph_rag_search
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.backends.base import PersonScope, TraversalHit
from brain.graph_rag.reconcile import ReconcileConfig, reconcile_document
from brain.vault.derived_links.directory import DirectoryStore
from tests.conftest import FakeEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# Suppression-disabled ratio (cap = round(N * 1.0) = N, so no entity with df <= N
# is ever generic) — keeps the tiny live corpora's edges materialized. Mirrors
# ``tests/test_graphrag_reconcile._NO_SUPPRESS``.
_NO_SUPPRESS = 1.0

# ``graph_communities.summary_embedding`` ships as vector(1024) (migration 013);
# the auto→global dispatch test seeds an embedding directly at that dim (no
# dim-reconcile), so its query embedder must match. Mirrors
# ``tests/test_graphrag_global._SUMMARY_DIM``.
_SUMMARY_DIM = 1024


# --------------------------------------------------------------------------- #
# Config + backend fakes
# --------------------------------------------------------------------------- #
def _make_cfg(**overrides: Any) -> Config:
    """A minimal :class:`Config` for the local-retrieval caps + tenant."""
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

    Only ``traverse`` is exercised by the local retrieval path, so that is the
    only Protocol method this fake needs.
    """

    def __init__(
        self,
        hits_by_seed: dict[str, list[TraversalHit]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.hits_by_seed: dict[str, list[TraversalHit]] = hits_by_seed or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "seed": seed_entity_uuid,
                "tenant_id": tenant_id,
                "depth": depth,
                "frontier_cap": frontier_cap,
                "min_edge_weight": min_edge_weight,
            }
        )
        if self.error is not None:
            raise self.error
        return list(self.hits_by_seed.get(seed_entity_uuid, []))


def _hit(entity_uuid: str, affinity: float, *, hops: int = 1) -> TraversalHit:
    return TraversalHit(
        entity_uuid=entity_uuid, affinity=affinity, hops=hops, tenant_id="default"
    )


class FakeComboBackend:
    """Records both ``traverse`` and ``scope_person`` — for the auto-router tests.

    The auto path may dispatch to the local (``traverse``) or themes
    (``scope_person``) retrieval depending on the routing decision, so a single
    backend exercising both seams keeps the router integration tests honest about
    which path actually ran.
    """

    def __init__(
        self,
        *,
        hits_by_seed: dict[str, list[TraversalHit]] | None = None,
        scope_by_seed: dict[str, PersonScope] | None = None,
    ) -> None:
        self.hits_by_seed: dict[str, list[TraversalHit]] = hits_by_seed or {}
        self.scope_by_seed: dict[str, PersonScope] = scope_by_seed or {}
        self.traverse_calls: list[str] = []
        self.scope_calls: list[str] = []

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
        self.traverse_calls.append(seed_entity_uuid)
        return list(self.hits_by_seed.get(seed_entity_uuid, []))

    def scope_person(
        self,
        conn: Any,
        tenant_id: str,
        seed_entity_uuid: str,
        *,
        frontier_cap: int,
    ) -> PersonScope:
        self.scope_calls.append(seed_entity_uuid)
        return self.scope_by_seed.get(
            seed_entity_uuid,
            PersonScope(
                seed_entity_uuid=seed_entity_uuid,
                entity_uuids=(),
                document_uuids=(),
                tenant_id=tenant_id,
            ),
        )


def _insert_contribution(
    conn: psycopg.Connection[Any],
    *,
    document_id: str,
    a: str,
    b: str,
    tenant: str = "default",
    count: int = 1,
) -> None:
    """Insert a canonical (``src < dst``) per-doc co-occurrence contribution."""
    src, dst = (a, b) if a < b else (b, a)
    conn.execute(
        "INSERT INTO graph_edge_contributions "
        "(tenant_id, document_id, src_id, dst_id, cooccur_count) "
        "VALUES (%s, %s, %s, %s, %s)",
        (tenant, document_id, src, dst, count),
    )


# --------------------------------------------------------------------------- #
# Relational seeding helpers
# --------------------------------------------------------------------------- #
def _insert_entity(
    conn: psycopg.Connection[Any],
    *,
    tenant: str = "default",
    entity_type: str = "topic",
    name: str,
    canonical_key: str,
    doc_count: int = 0,
) -> str:
    """Insert a ``graph_entities`` row; return its id."""
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
    source: str = "people",
    mention_count: int = 1,
) -> None:
    """Link an entity to a document via ``graph_entity_mentions``."""
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) "
        "VALUES (%s, %s, %s, %s, %s)",
        (tenant, entity_id, document_id, mention_count, source),
    )


def _insert_doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    content: str,
    content_type: str = "note",
) -> str:
    """Insert a manual ``documents`` row; return id."""
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
        "VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
        (src_row[0], title, content, content_hash, content_type),
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
    """Insert one chunk (its ``tsv`` is generated from ``content``)."""
    vec = embedder.embed([content], input_type="document")[0]
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s, %s, %s, %s)",
        (document_id, chunk_index, content, vec),
    )


def _insert_community(
    conn: psycopg.Connection[Any],
    *,
    summary: str | None,
    embedding: list[float] | None = None,
    tenant: str = "default",
    member_count: int = 0,
) -> str:
    """Insert a ``graph_communities`` row; return its ``community_key``.

    Mirrors ``tests/test_graphrag_global._insert_community`` — used only by the
    auto→global dispatch test (the global path ranks communities, not seeds).
    """
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


# --------------------------------------------------------------------------- #
# Live-AGE seeding helpers (mirror tests/test_graphrag_reconcile)
# --------------------------------------------------------------------------- #
def _seed_directory(
    conn: psycopg.Connection[Any], pairs: Sequence[tuple[str, str]]
) -> None:
    store = DirectoryStore(conn)
    for name, email in pairs:
        store.upsert_pair(display_name=name, email=email, source="gmail")


def _seed_gmail_doc(
    conn: psycopg.Connection[Any],
    *,
    external_id: str,
    participants: Sequence[tuple[str, str]],
    content: str,
) -> str:
    """Insert a sources+documents gmail pair; return the doc id."""
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('gmail', %s, '{}'::jsonb) RETURNING id",
        (external_id,),
    ).fetchone()
    assert src_row is not None
    from_hdr = f"{participants[0][0]} <{participants[0][1]}>"
    to_hdr = ", ".join(f"{n} <{e}>" for n, e in participants[1:])
    metadata = {"from": from_hdr, "to": to_hdr, "thread_id": external_id}
    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        "INSERT INTO documents "
        "(source_id, title, content, content_hash, content_type, metadata) "
        "VALUES (%s, %s, %s, %s, 'email', %s::jsonb) RETURNING id::text",
        (src_row[0], external_id, salted, content_hash, json.dumps(metadata)),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _entity_id(conn: psycopg.Connection[Any], tenant: str, canonical_key: str) -> str:
    row = conn.execute(
        "SELECT id::text FROM graph_entities "
        "WHERE tenant_id = %s AND canonical_key = %s",
        (tenant, canonical_key),
    ).fetchone()
    assert row is not None, f"no entity for {canonical_key!r}"
    return str(row[0])


def _rcfg(tenant: str = "default") -> ReconcileConfig:
    return ReconcileConfig(tenant_id=tenant, generic_df_ratio=_NO_SUPPRESS)


def _backend(test_db: psycopg.Connection[Any]) -> AgeBackend:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    return backend


# --------------------------------------------------------------------------- #
# 1. Seed resolution
# --------------------------------------------------------------------------- #
def test_exact_canonical_key_seed(test_db: psycopg.Connection[Any]) -> None:
    eid = _insert_entity(
        test_db, name="Acme Corp", canonical_key="acme", entity_type="org"
    )
    backend = FakeTraversalBackend()

    ctx = graph_rag_search(test_db, _make_cfg(), "acme", backend=backend)

    assert ctx.mode == "local"
    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == [eid]
    # Seed is included in the entities neighbourhood.
    assert [e.id for e in ctx.entities] == [eid]
    assert backend.calls[0]["seed"] == eid


def test_exact_name_seed(test_db: psycopg.Connection[Any]) -> None:
    """A query that matches ``name`` (not ``canonical_key``) still resolves."""
    eid = _insert_entity(
        test_db, name="Acme Corp", canonical_key="acme", entity_type="org"
    )
    ctx = graph_rag_search(
        test_db, _make_cfg(), "Acme Corp", backend=FakeTraversalBackend()
    )
    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == [eid]


def test_no_seed_returns_empty_never_raises(
    test_db: psycopg.Connection[Any],
) -> None:
    _insert_entity(test_db, name="Acme Corp", canonical_key="acme", entity_type="org")
    backend = FakeTraversalBackend()

    ctx = graph_rag_search(test_db, _make_cfg(), "nonexistent-xyz", backend=backend)

    assert ctx.mode == "local"
    assert ctx.entities == []
    assert ctx.docs == []
    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == []
    assert ctx.explanation.nodes_visited == 0
    # No seed → traverse is never called.
    assert backend.calls == []


def test_blank_query_returns_empty(test_db: psycopg.Connection[Any]) -> None:
    _insert_entity(test_db, name="Acme Corp", canonical_key="acme", entity_type="org")
    ctx = graph_rag_search(test_db, _make_cfg(), "   ", backend=FakeTraversalBackend())
    assert ctx.entities == []
    assert ctx.docs == []


def test_exact_match_precedes_substring(test_db: psycopg.Connection[Any]) -> None:
    """An exact match wins outright; the substring tier is not consulted."""
    exact = _insert_entity(
        test_db, name="alpha", canonical_key="alpha", entity_type="topic"
    )
    _insert_entity(
        test_db, name="alphabet", canonical_key="alphabet", entity_type="topic"
    )

    ctx = graph_rag_search(
        test_db, _make_cfg(), "alpha", backend=FakeTraversalBackend()
    )

    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == [exact]


def test_substring_fallback_with_doc_count_tiebreak(
    test_db: psycopg.Connection[Any],
) -> None:
    """No exact match → substring tier, ordered by doc_count DESC (tie-break)."""
    low = _insert_entity(
        test_db, name="alphabet", canonical_key="alphabet", doc_count=2
    )
    high = _insert_entity(
        test_db, name="alpha centauri", canonical_key="alpha centauri", doc_count=9
    )

    ctx = graph_rag_search(test_db, _make_cfg(), "alph", backend=FakeTraversalBackend())

    assert ctx.explanation is not None
    # Higher doc_count first; both resolve in the substring tier.
    assert ctx.explanation.seed_entity_ids == [high, low]


def test_exact_tier_orders_by_doc_count(test_db: psycopg.Connection[Any]) -> None:
    """Two entity types share a canonical_key → ordered by doc_count DESC."""
    person = _insert_entity(
        test_db, name="Bob", canonical_key="bob", entity_type="person", doc_count=10
    )
    topic = _insert_entity(
        test_db, name="bob", canonical_key="bob", entity_type="topic", doc_count=3
    )

    ctx = graph_rag_search(test_db, _make_cfg(), "bob", backend=FakeTraversalBackend())

    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == [person, topic]


# --------------------------------------------------------------------------- #
# 2. Cap injection
# --------------------------------------------------------------------------- #
def test_caps_default_from_config(test_db: psycopg.Connection[Any]) -> None:
    eid = _insert_entity(test_db, name="alpha", canonical_key="alpha")
    backend = FakeTraversalBackend()

    graph_rag_search(
        test_db,
        _make_cfg(graph_depth=3, graph_frontier_cap=42, graph_min_edge_weight=0.25),
        "alpha",
        backend=backend,
    )

    call = backend.calls[0]
    assert call["seed"] == eid
    assert call["depth"] == 3
    assert call["frontier_cap"] == 42
    assert call["min_edge_weight"] == 0.25
    assert call["tenant_id"] == "default"


def test_caps_overridable_by_args(test_db: psycopg.Connection[Any]) -> None:
    _insert_entity(test_db, name="alpha", canonical_key="alpha")
    backend = FakeTraversalBackend()

    graph_rag_search(
        test_db,
        _make_cfg(graph_depth=2, graph_frontier_cap=200, graph_min_edge_weight=0.2),
        "alpha",
        backend=backend,
        depth=5,
        frontier_cap=7,
        min_edge_weight=0.5,
    )

    call = backend.calls[0]
    assert (call["depth"], call["frontier_cap"], call["min_edge_weight"]) == (5, 7, 0.5)


# --------------------------------------------------------------------------- #
# 3. Document ranking + GraphContext assembly
# --------------------------------------------------------------------------- #
def test_document_ranking_and_assembly(test_db: psycopg.Connection[Any]) -> None:
    seed = _insert_entity(test_db, name="alpha", canonical_key="alpha", doc_count=5)
    r1 = _insert_entity(test_db, name="beta", canonical_key="beta")
    r2 = _insert_entity(test_db, name="gamma", canonical_key="gamma")

    doc_a = _insert_doc(test_db, title="A", content="alpha and beta together")
    doc_b = _insert_doc(test_db, title="B", content="beta only here")
    doc_c = _insert_doc(test_db, title="C", content="alpha only here")
    doc_d = _insert_doc(test_db, title="D", content="gamma only here")

    _insert_mention(test_db, entity_id=seed, document_id=doc_a)
    _insert_mention(test_db, entity_id=r1, document_id=doc_a)
    _insert_mention(test_db, entity_id=r1, document_id=doc_b)
    _insert_mention(test_db, entity_id=seed, document_id=doc_c)
    _insert_mention(test_db, entity_id=r2, document_id=doc_d)

    backend = FakeTraversalBackend(hits_by_seed={seed: [_hit(r1, 0.8), _hit(r2, 0.3)]})

    ctx = graph_rag_search(test_db, _make_cfg(), "alpha", backend=backend)

    # entities = seed first, then reached by affinity DESC.
    assert [e.id for e in ctx.entities] == [seed, r1, r2]
    # docs ranked: A(1.0+0.8) > C(1.0) > B(0.8) > D(0.3).
    assert [d.document_id for d in ctx.docs] == [doc_a, doc_c, doc_b, doc_d]
    scores = {d.document_id: d.score for d in ctx.docs}
    assert scores[doc_a] == pytest.approx(1.8)
    assert scores[doc_c] == pytest.approx(1.0)
    assert scores[doc_b] == pytest.approx(0.8)
    assert scores[doc_d] == pytest.approx(0.3)

    assert ctx.explanation is not None
    exp = ctx.explanation
    assert exp.seed_entity_ids == [seed]
    assert exp.nodes_visited == 3  # 1 seed + 2 reached
    assert exp.matched_filters["seed_count"] == 1
    assert exp.matched_filters["reached_count"] == 2


def test_limit_caps_returned_docs(test_db: psycopg.Connection[Any]) -> None:
    seed = _insert_entity(test_db, name="alpha", canonical_key="alpha")
    doc1 = _insert_doc(test_db, title="1", content="alpha one")
    doc2 = _insert_doc(test_db, title="2", content="alpha two")
    doc3 = _insert_doc(test_db, title="3", content="alpha three")
    for doc in (doc1, doc2, doc3):
        _insert_mention(test_db, entity_id=seed, document_id=doc)

    ctx = graph_rag_search(
        test_db, _make_cfg(), "alpha", backend=FakeTraversalBackend(), limit=2
    )

    assert len(ctx.docs) == 2


def test_snippet_attached_from_chunk(test_db: psycopg.Connection[Any]) -> None:
    seed = _insert_entity(test_db, name="alpha", canonical_key="alpha")

    class _Emb:
        dim = 4096

        def embed(
            self, texts: list[str], input_type: str = "document"
        ) -> list[list[float]]:
            return [[0.01] * self.dim for _ in texts]

    doc = _insert_doc(
        test_db, title="Doc", content="The quarterly pricing review covered tiers."
    )
    _add_chunk(test_db, _Emb(), doc, "The quarterly pricing review covered tiers.")
    _insert_mention(test_db, entity_id=seed, document_id=doc)

    ctx = graph_rag_search(test_db, _make_cfg(), "alpha", backend=FakeTraversalBackend())

    assert len(ctx.docs) == 1
    assert "pricing review" in ctx.docs[0].snippet


def test_doc_without_chunks_has_empty_snippet(
    test_db: psycopg.Connection[Any],
) -> None:
    seed = _insert_entity(test_db, name="alpha", canonical_key="alpha")
    doc = _insert_doc(test_db, title="Doc", content="body never chunked")
    _insert_mention(test_db, entity_id=seed, document_id=doc)

    ctx = graph_rag_search(test_db, _make_cfg(), "alpha", backend=FakeTraversalBackend())

    assert len(ctx.docs) == 1
    assert ctx.docs[0].snippet == ""


def test_graph_hit_carries_document_summary(
    test_db: psycopg.Connection[Any],
) -> None:
    """A graph-retrieved hit populates ``SearchResult.summary``, like hybrid does.

    Without this the field's value would depend on WHICH LEG retrieved the
    document: ``fuse`` picks one carrier per id (hybrid first, else graph), so a
    graph-only hit would silently report "no summary" and the brief projection
    would be forced onto the chunk snippet for a document that has one.
    """
    # Arrange
    seed = _insert_entity(test_db, name="alpha", canonical_key="alpha")
    with_summary = _insert_doc(test_db, title="Has summary", content="body one")
    without_summary = _insert_doc(test_db, title="No summary", content="body two")
    summary_text = "Synthetic one-line abstract of the first document."
    test_db.execute(
        "UPDATE documents SET summary = %s WHERE id = %s",
        (summary_text, with_summary),
    )
    _insert_mention(test_db, entity_id=seed, document_id=with_summary)
    _insert_mention(test_db, entity_id=seed, document_id=without_summary)

    # Act
    ctx = graph_rag_search(test_db, _make_cfg(), "alpha", backend=FakeTraversalBackend())

    # Assert
    by_title = {doc.title: doc for doc in ctx.docs}
    assert by_title["Has summary"].summary == summary_text
    assert by_title["No summary"].summary is None


# --------------------------------------------------------------------------- #
# 4. Mode guard + error propagation
# --------------------------------------------------------------------------- #
def test_explicit_global_mode_dispatches_to_global(
    test_db: psycopg.Connection[Any],
) -> None:
    # G3-d: an EXPLICIT ``mode='global'`` now DISPATCHES to the community-based
    # global retrieval path (it was rejected with GraphModeUnavailable in G2; the
    # router-level rejection is removed in G3-e). With no communities seeded it
    # returns an empty-but-valid global context (never-raise). The backend is
    # unused on the global path. Full global coverage lives in
    # ``tests/test_graphrag_global.py``.
    ctx = graph_rag_search(
        test_db,
        _make_cfg(),
        "alpha",
        backend=FakeTraversalBackend(),
        mode="global",
    )
    assert ctx.mode == "global"
    assert ctx.communities == []
    assert ctx.docs == []


def test_backend_error_propagates(test_db: psycopg.Connection[Any]) -> None:
    """A traversal cap-exceed is a loud failure, not a silent truncation."""
    _insert_entity(test_db, name="alpha", canonical_key="alpha")
    backend = FakeTraversalBackend(error=GraphBackendError("cap exceeded"))

    with pytest.raises(GraphBackendError):
        graph_rag_search(test_db, _make_cfg(), "alpha", backend=backend)


# --------------------------------------------------------------------------- #
# 5. Live-AGE integration
# --------------------------------------------------------------------------- #
def test_live_local_reaches_neighbors_and_docs(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    backend = _backend(test_db)
    _seed_directory(
        test_db,
        [("alice", "alice@x.com"), ("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    doc1 = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
        content="Pricing strategy sync about enterprise tiers.",
    )
    doc2 = _seed_gmail_doc(
        test_db,
        external_id="m2",
        participants=[("bob", "bob@x.com"), ("carol", "carol@x.com")],
        content="Roadmap planning for the analytics module.",
    )
    _add_chunk(
        test_db, fake_embedder, doc1, "Pricing strategy sync about enterprise tiers."
    )
    _add_chunk(
        test_db, fake_embedder, doc2, "Roadmap planning for the analytics module."
    )
    reconcile_document(test_db, doc1, backend=backend, config=_rcfg())
    reconcile_document(test_db, doc2, backend=backend, config=_rcfg())

    ctx = graph_rag_search(
        test_db, _make_cfg(graph_min_edge_weight=0.0), "bob", backend=backend
    )

    bob_id = _entity_id(test_db, "default", "bob")
    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == [bob_id]
    # bob (seed) reaches alice + carol (its only co-mention neighbours).
    assert {e.canonical_key for e in ctx.entities} == {"bob", "alice", "carol"}
    assert ctx.entities[0].canonical_key == "bob"  # seed first
    # Both docs surface (each co-mentions bob with a neighbour).
    assert {d.document_id for d in ctx.docs} == {doc1, doc2}
    assert all(d.snippet for d in ctx.docs)


def test_live_local_seed_with_no_neighbors(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A resolvable seed with no CO_OCCURS edges still returns its own docs."""
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="solo",
        participants=[("alice", "alice@x.com"), ("ghost", "ghost@x.com")],
        content="Solo note mentioning only alice in the directory.",
    )
    _add_chunk(test_db, fake_embedder, doc, "Solo note mentioning only alice.")
    reconcile_document(test_db, doc, backend=backend, config=_rcfg())

    ctx = graph_rag_search(
        test_db, _make_cfg(graph_min_edge_weight=0.0), "alice", backend=backend
    )

    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == [_entity_id(test_db, "default", "alice")]
    # No neighbours, but the seed's own document is returned.
    assert {e.canonical_key for e in ctx.entities} == {"alice"}
    assert {d.document_id for d in ctx.docs} == {doc}


def test_live_local_is_tenant_scoped(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A second tenant's identical graph never leaks into a default-tenant query."""
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])

    default_doc = _seed_gmail_doc(
        test_db,
        external_id="d1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
        content="Default tenant conversation.",
    )
    other_doc = _seed_gmail_doc(
        test_db,
        external_id="o1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
        content="Other tenant conversation.",
    )
    _add_chunk(test_db, fake_embedder, default_doc, "Default tenant conversation.")
    _add_chunk(test_db, fake_embedder, other_doc, "Other tenant conversation.")
    reconcile_document(test_db, default_doc, backend=backend, config=_rcfg("default"))
    reconcile_document(test_db, other_doc, backend=backend, config=_rcfg("other"))

    default_ctx = graph_rag_search(
        test_db, _make_cfg(graph_min_edge_weight=0.0), "bob", backend=backend
    )
    other_ctx = graph_rag_search(
        test_db,
        _make_cfg(graph_min_edge_weight=0.0),
        "bob",
        backend=backend,
        tenant="other",
    )

    assert {d.document_id for d in default_ctx.docs} == {default_doc}
    assert {d.document_id for d in other_ctx.docs} == {other_doc}
    assert default_ctx.tenant_id == "default"
    assert other_ctx.tenant_id == "other"
    assert default_ctx.explanation is not None and other_ctx.explanation is not None
    # Distinct seed entities per tenant (no shared id).
    assert (
        default_ctx.explanation.seed_entity_ids
        != other_ctx.explanation.seed_entity_ids
    )


# --------------------------------------------------------------------------- #
# 6. Auto-router integration (G2-g) — router wired into graph_rag_search
# --------------------------------------------------------------------------- #
def _seed_dana_themes(
    conn: psycopg.Connection[Any],
) -> tuple[str, PersonScope]:
    """Seed Dana + one 2-topic cluster (topic-a/topic-b) over one of Dana's docs.

    Returns the Dana person-entity id and a matching ``PersonScope`` (keyed by
    that id) so a :class:`FakeComboBackend` can serve the themes scope step.
    """
    _seed_directory(conn, [("dana lee", "dana@x.com")])
    dana = _insert_entity(
        conn, entity_type="person", name="Dana Lee", canonical_key="dana lee"
    )
    a = _insert_entity(conn, name="Topic A", canonical_key="topic-a")
    b = _insert_entity(conn, name="Topic B", canonical_key="topic-b")
    d1 = _insert_doc(conn, title="D1", content="topic-a and topic-b together")
    for ent in (dana, a, b):
        _insert_mention(conn, entity_id=ent, document_id=d1)
    _insert_contribution(conn, document_id=d1, a=a, b=b)
    scope = PersonScope(
        seed_entity_uuid=dana,
        entity_uuids=tuple(sorted((a, b))),
        document_uuids=(d1,),
    )
    return dana, scope


def test_auto_non_thematic_dispatches_local(test_db: psycopg.Connection[Any]) -> None:
    """A non-thematic auto query runs local retrieval; no degradation signals."""
    seed = _insert_entity(
        test_db, name="Acme Corp", canonical_key="acme", entity_type="org"
    )
    backend = FakeComboBackend()

    ctx = graph_rag_search(test_db, _make_cfg(), "acme", backend=backend, mode="auto")

    assert ctx.mode == "local"
    assert ctx.requested_mode is None
    assert ctx.degraded_from is None
    assert ctx.degradation_reason is None
    assert [e.id for e in ctx.entities] == [seed]
    assert backend.traverse_calls == [seed]
    assert backend.scope_calls == []  # local path, never scopes


def test_auto_thematic_no_person_dispatches_global(
    test_db: psycopg.Connection[Any],
) -> None:
    """G3-e flip: thematic + no resolvable person dispatches to the real global
    community path (was the G2 degraded-local), invoking ``_retrieve_global`` with
    the injected pre-warmed ``embedder`` and stamping NO degradation signals (Q6)."""
    # 'recurring'/'themes' make the query thematic; no person entity is seeded, so
    # the auto router resolves no person → global (not themes, not degraded local).
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    query = "what are the recurring themes lately"
    query_vec = emb.embed([query], input_type="query")[0]
    # Seed one community whose embedding matches the query so the vector leg of
    # ``_retrieve_global`` ranks it (proving the global path actually ran).
    community_key = _insert_community(
        test_db, summary="roadmap themes", embedding=query_vec
    )
    backend = FakeComboBackend()

    ctx = graph_rag_search(
        test_db,
        _make_cfg(),
        query,
        backend=backend,
        mode="auto",
        embedder=emb,
    )

    assert ctx.mode == "global"
    assert [c.community_key for c in ctx.communities] == [community_key]
    # No degradation signals — the flip routes to the real global path; the G2
    # degradation fields stay dormant (spec §17c Q6).
    assert ctx.requested_mode is None
    assert ctx.degraded_from is None
    assert ctx.degradation_reason is None
    # The global path never touches the traversal/scope backend.
    assert backend.traverse_calls == []
    assert backend.scope_calls == []


def test_auto_thematic_scanned_person_dispatches_themes(
    test_db: psycopg.Connection[Any],
) -> None:
    """Thematic query naming a known person → themes; no degradation signals."""
    dana, scope = _seed_dana_themes(test_db)
    backend = FakeComboBackend(scope_by_seed={dana: scope})

    ctx = graph_rag_search(
        test_db,
        _make_cfg(graph_generic_df_ratio=_NO_SUPPRESS, graph_theme_limit=5),
        "what are the themes in my conversations with dana lee",
        backend=backend,
        mode="auto",
    )

    assert ctx.mode == "themes"
    assert ctx.person == "Dana Lee"
    assert {frozenset(e.canonical_key for e in t.entities) for t in ctx.themes} == {
        frozenset({"topic-a", "topic-b"})
    }
    # Themes is an honored route — never degraded.
    assert ctx.requested_mode is None
    assert ctx.degraded_from is None
    assert ctx.degradation_reason is None
    assert backend.scope_calls == [dana]
    assert backend.traverse_calls == []  # themes path, never traverses


def test_auto_thematic_explicit_person_dispatches_themes(
    test_db: psycopg.Connection[Any],
) -> None:
    """An explicit person + thematic auto query routes to themes (precedence)."""
    dana, scope = _seed_dana_themes(test_db)
    backend = FakeComboBackend(scope_by_seed={dana: scope})

    ctx = graph_rag_search(
        test_db,
        _make_cfg(graph_generic_df_ratio=_NO_SUPPRESS, graph_theme_limit=5),
        "recurring themes please",
        backend=backend,
        mode="auto",
        person="dana lee",
    )

    assert ctx.mode == "themes"
    assert ctx.person == "Dana Lee"
    assert backend.scope_calls == [dana]
    assert ctx.degraded_from is None


def test_explicit_local_mode_has_no_degradation_fields(
    test_db: psycopg.Connection[Any],
) -> None:
    """Explicit local is honored unchanged; degradation fields stay None."""
    seed = _insert_entity(test_db, name="alpha", canonical_key="alpha")

    ctx = graph_rag_search(
        test_db, _make_cfg(), "alpha", backend=FakeTraversalBackend(), mode="local"
    )

    assert ctx.mode == "local"
    assert [e.id for e in ctx.entities] == [seed]
    assert ctx.requested_mode is None
    assert ctx.degraded_from is None
    assert ctx.degradation_reason is None
