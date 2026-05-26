"""Tests for ``brain.graph_rag.retrieve`` themes-with-X retrieval (G2-f, HEADLINE).

The headline feature — "what are the themes in my conversations with X?" — is the
scope-first algorithm of spec §6b: scope to person X, compute the in-scope
normalized lift over ``graph_edge_contributions`` restricted to X's documents,
suppress generic entities, exclude X + the owner, group the scoped subgraph, and
return ranked :class:`ThemeGroup`s with representative X-docs + snippets.

Two layers (mirroring ``tests/test_graphrag_retrieve``):

* **Orchestration units** — real Postgres relational side + a recording
  ``FakeScopeBackend`` injected for the AGE scope step. Exercises the in-scope
  lift, generic suppression, seed-X + owner exclusion, grouping integration,
  doc/snippet population, ranking determinism, the ``synthesize`` DI seam +
  never-raise discipline, ``PersonNotFound`` / ``PersonAmbiguous`` /
  ``person`` validation, and the never-raise-on-empty contract.
* **Live-AGE integration** (``test_db`` against the AGE test instance on port
  5434) — the HEADLINE proof: a synthetic person "Dana Lee" co-occurs with two
  distinct topic clusters across several synthetic documents, built via the real
  ``reconcile_document`` + ``AgeBackend`` (people + a fake concept extractor);
  themes for Dana surfaces exactly the two clusters, excludes Dana + owner, is
  tenant-scoped, and carries representative docs + snippets.

All people / entities / orgs are synthetic (Dana Lee, topics, owner); no PII.
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
from brain.errors import GraphBackendError, PersonAmbiguous, PersonNotFound
from brain.graph_rag import THEMES_MODE, graph_rag_search
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.backends.base import PersonScope
from brain.graph_rag.extract import ExtractedEntity
from brain.graph_rag.reconcile import ReconcileConfig, reconcile_document
from brain.vault.derived_links.directory import DirectoryStore

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# Suppression-disabled ratio (cap = round(N * 1.0) = N, so no entity with df <= N
# is ever generic) — keeps the tiny corpora's entities eligible. Mirrors
# ``tests/test_graphrag_retrieve._NO_SUPPRESS``.
_NO_SUPPRESS = 1.0


# --------------------------------------------------------------------------- #
# Config + fakes
# --------------------------------------------------------------------------- #
def _make_cfg(**overrides: Any) -> Config:
    """A minimal :class:`Config` for the themes caps + tenant + generic ratio."""
    params: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "graph_tenant_id": "default",
        "graph_frontier_cap": 200,
        "graph_min_edge_weight": 0.2,
        "graph_generic_df_ratio": _NO_SUPPRESS,
        "graph_theme_limit": 5,
    }
    params.update(overrides)
    return Config(**params)


class FakeScopeBackend:
    """Records ``scope_person`` calls; returns canned scopes per seed (no AGE).

    Only ``scope_person`` is exercised by the themes path, so it is the only
    Protocol method this fake needs (mirrors ``FakeTraversalBackend`` for local).
    """

    def __init__(
        self,
        scope_by_seed: dict[str, PersonScope] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.scope_by_seed: dict[str, PersonScope] = scope_by_seed or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def scope_person(
        self,
        conn: Any,
        tenant_id: str,
        seed_entity_uuid: str,
        *,
        frontier_cap: int,
    ) -> PersonScope:
        self.calls.append(
            {
                "seed": seed_entity_uuid,
                "tenant_id": tenant_id,
                "frontier_cap": frontier_cap,
            }
        )
        if self.error is not None:
            raise self.error
        return self.scope_by_seed.get(
            seed_entity_uuid,
            PersonScope(
                seed_entity_uuid=seed_entity_uuid,
                entity_uuids=(),
                document_uuids=(),
                tenant_id=tenant_id,
            ),
        )


class _FakeEnricher:
    """Records ``summarize_group`` calls; returns a canned summary (or raises)."""

    def __init__(
        self, *, summary: str | None = "THEME SUMMARY", error: Exception | None = None
    ) -> None:
        self.summary = summary
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def summarize_group(
        self,
        *,
        person: str | None,
        entity_names: list[str],
        doc_titles: list[str],
    ) -> str | None:
        self.calls.append(
            {
                "person": person,
                "entity_names": list(entity_names),
                "doc_titles": list(doc_titles),
            }
        )
        if self.error is not None:
            raise self.error
        return self.summary


class _FakeConceptExtractor:
    """A deterministic concept extractor for the live headline test.

    Returns the concept entities whose ``marker`` substring appears in a
    document's text, at adjacent word positions (0, 1, ...) so they co-occur
    within the default window — i.e. they form a cluster. No Ollama, no PII.
    """

    def __init__(self, by_marker: dict[str, list[tuple[str, str, str]]]) -> None:
        self._by_marker = by_marker

    @property
    def version(self) -> str:
        return "fake-extractor@concepts-v1"

    def extract(self, text: str) -> list[ExtractedEntity]:
        out: list[ExtractedEntity] = []
        for marker, concepts in self._by_marker.items():
            if marker in text:
                for position, (etype, key, name) in enumerate(concepts):
                    out.append(
                        ExtractedEntity(
                            entity_type=etype,
                            canonical_key=key,
                            display_name=name,
                            positions=(position,),
                        )
                    )
        return out


# --------------------------------------------------------------------------- #
# Relational seeding helpers (mirror tests/test_graphrag_retrieve)
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
) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) "
        "VALUES (%s, %s, %s, 1, %s)",
        (tenant, entity_id, document_id, source),
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


def _insert_doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    content: str,
    content_type: str = "note",
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
    vec = embedder.embed([content], input_type="document")[0]
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s, %s, %s, %s)",
        (document_id, chunk_index, content, vec),
    )


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
    tenant_marker: str = "",
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
    salted = f"{content}\n<!-- {tenant_marker}{uuid.uuid4()} -->"
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


def _backend(test_db: psycopg.Connection[Any]) -> AgeBackend:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    return backend


def _theme_keysets(ctx: Any) -> set[frozenset[str]]:
    return {frozenset(e.canonical_key for e in t.entities) for t in ctx.themes}


def _all_theme_keys(ctx: Any) -> set[str]:
    return {e.canonical_key for t in ctx.themes for e in t.entities}


def _digest(ctx: Any) -> list[tuple[int, tuple[str, ...], float, tuple[str, ...]]]:
    """An order-stable digest of the themes for determinism assertions."""
    return [
        (
            t.group_id,
            tuple(e.canonical_key for e in t.entities),
            round(t.score, 9),
            tuple(t.doc_ids),
        )
        for t in ctx.themes
    ]


def _build_two_clusters(
    conn: psycopg.Connection[Any], *, tenant: str = "default"
) -> tuple[str, PersonScope, dict[str, str]]:
    """Seed Dana + two disjoint 2-topic clusters across four of Dana's docs.

    Cluster A {pricing, billing} in d1/d2; cluster B {roadmap, analytics} in
    d3/d4. Returns the Dana entity id, a matching ``PersonScope``, and the id
    map. Co-occurrence contributions are topic-topic only (the seed is excluded
    from theme edges), so the in-scope lift is 1.0 within each cluster.
    """
    _seed_directory(conn, [("dana lee", "dana@x.com")])
    # ``doc_count`` is the maintained CORPUS-WIDE df the generic filter reads
    # (FIX A): Dana is in all 4 docs, each cluster topic in 2.
    dana = _insert_entity(
        conn, tenant=tenant, entity_type="person", name="Dana Lee",
        canonical_key="dana lee", doc_count=4,
    )
    t1 = _insert_entity(
        conn, tenant=tenant, name="Pricing", canonical_key="pricing", doc_count=2
    )
    t2 = _insert_entity(
        conn, tenant=tenant, name="Billing", canonical_key="billing", doc_count=2
    )
    t3 = _insert_entity(
        conn, tenant=tenant, name="Roadmap", canonical_key="roadmap", doc_count=2
    )
    t4 = _insert_entity(
        conn, tenant=tenant, name="Analytics", canonical_key="analytics", doc_count=2
    )
    d1 = _insert_doc(conn, title="P1", content="pricing and billing one")
    d2 = _insert_doc(conn, title="P2", content="pricing and billing two")
    d3 = _insert_doc(conn, title="R1", content="roadmap and analytics one")
    d4 = _insert_doc(conn, title="R2", content="roadmap and analytics two")
    for doc in (d1, d2, d3, d4):
        _insert_mention(conn, entity_id=dana, document_id=doc, tenant=tenant)
    for topic in (t1, t2):
        for doc in (d1, d2):
            _insert_mention(conn, entity_id=topic, document_id=doc, tenant=tenant)
    for topic in (t3, t4):
        for doc in (d3, d4):
            _insert_mention(conn, entity_id=topic, document_id=doc, tenant=tenant)
    for doc in (d1, d2):
        _insert_contribution(conn, document_id=doc, a=t1, b=t2, tenant=tenant)
    for doc in (d3, d4):
        _insert_contribution(conn, document_id=doc, a=t3, b=t4, tenant=tenant)
    scope = PersonScope(
        seed_entity_uuid=dana,
        entity_uuids=tuple(sorted((t1, t2, t3, t4))),
        document_uuids=tuple(sorted((d1, d2, d3, d4))),
        tenant_id=tenant,
    )
    ids = {"t1": t1, "t2": t2, "t3": t3, "t4": t4, "d1": d1, "d2": d2, "d3": d3, "d4": d4}
    return dana, scope, ids


# --------------------------------------------------------------------------- #
# 1. Grouping integration + in-scope lift
# --------------------------------------------------------------------------- #
def test_themes_two_clusters_grouping(test_db: psycopg.Connection[Any]) -> None:
    dana, scope, _ = _build_two_clusters(test_db)
    backend = FakeScopeBackend({dana: scope})

    ctx = graph_rag_search(
        test_db, _make_cfg(), "", backend=backend, mode=THEMES_MODE, person="dana lee"
    )

    assert ctx.mode == THEMES_MODE
    assert ctx.person == "Dana Lee"
    assert len(ctx.themes) == 2
    assert _theme_keysets(ctx) == {
        frozenset({"pricing", "billing"}),
        frozenset({"roadmap", "analytics"}),
    }
    # Each cluster has a single in-scope-lift edge of 1.0 → group score 1.0.
    assert all(t.score == pytest.approx(1.0) for t in ctx.themes)
    # The seed X never appears as a theme entity (spec §17.5).
    assert "dana lee" not in _all_theme_keys(ctx)
    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == [dana]
    assert ctx.explanation.generic_df_cap is not None
    assert backend.calls[0]["seed"] == dana
    assert backend.calls[0]["frontier_cap"] == 200


def test_themes_in_scope_lift_is_fractional(
    test_db: psycopg.Connection[Any],
) -> None:
    """A pair co-occurring in 1 of 2 shared-scope docs has in-scope lift 0.5."""
    _seed_directory(test_db, [("dana lee", "dana@x.com")])
    dana = _insert_entity(
        test_db, entity_type="person", name="Dana Lee", canonical_key="dana lee"
    )
    a = _insert_entity(test_db, name="Topic A", canonical_key="topic-a")
    b = _insert_entity(test_db, name="Topic B", canonical_key="topic-b")
    d1 = _insert_doc(test_db, title="D1", content="alpha")
    d2 = _insert_doc(test_db, title="D2", content="beta")
    d3 = _insert_doc(test_db, title="D3", content="gamma")
    for doc in (d1, d2, d3):
        _insert_mention(test_db, entity_id=dana, document_id=doc)
    # df(a)=2 (d1,d2), df(b)=2 (d1,d3), but they co-occur only in d1 → lift 1/2.
    for doc in (d1, d2):
        _insert_mention(test_db, entity_id=a, document_id=doc)
    for doc in (d1, d3):
        _insert_mention(test_db, entity_id=b, document_id=doc)
    _insert_contribution(test_db, document_id=d1, a=a, b=b)
    scope = PersonScope(
        seed_entity_uuid=dana,
        entity_uuids=tuple(sorted((a, b))),
        document_uuids=tuple(sorted((d1, d2, d3))),
    )

    ctx = graph_rag_search(
        test_db,
        _make_cfg(),
        "",
        backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE,
        person="dana lee",
    )

    assert len(ctx.themes) == 1
    assert _theme_keysets(ctx) == {frozenset({"topic-a", "topic-b"})}
    assert ctx.themes[0].score == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# 2. Exclusions: seed X + owner; non-X people are theme-eligible
# --------------------------------------------------------------------------- #
def test_themes_excludes_seed_and_owner_keeps_other_person(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_directory(test_db, [("dana lee", "dana@x.com")])
    dana = _insert_entity(
        test_db, entity_type="person", name="Dana Lee", canonical_key="dana lee"
    )
    owner = _insert_entity(
        test_db, entity_type="person", name="Owner One", canonical_key="owner one"
    )
    bob = _insert_entity(
        test_db, entity_type="person", name="Bob Ray", canonical_key="bob ray"
    )
    doc = _insert_doc(test_db, title="D", content="a meeting")
    for ent in (dana, owner, bob):
        _insert_mention(test_db, entity_id=ent, document_id=doc)
    # scope returns the co-mentioned owner + bob (the seed Dana is excluded by
    # scope_person itself; the fake mirrors that).
    scope = PersonScope(
        seed_entity_uuid=dana,
        entity_uuids=tuple(sorted((owner, bob))),
        document_uuids=(doc,),
    )

    ctx = graph_rag_search(
        test_db,
        _make_cfg(owner_participants=frozenset({"owner one"})),
        "",
        backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE,
        person="dana lee",
    )

    # Owner + seed excluded; the non-X, non-owner person Bob is a theme entity.
    assert _all_theme_keys(ctx) == {"bob ray"}
    assert "dana lee" not in _all_theme_keys(ctx)
    assert "owner one" not in _all_theme_keys(ctx)


# --------------------------------------------------------------------------- #
# 3. Generic suppression (absolute corpus cap)
# --------------------------------------------------------------------------- #
def test_themes_generic_suppression(test_db: psycopg.Connection[Any]) -> None:
    """A topic generic across the corpus (df above the corpus cap) is dropped."""
    dana, scope, ids = _build_two_clusters(test_db)
    # A generic topic mentioned in ALL FOUR of Dana's docs → corpus-wide df 4
    # (``doc_count`` is the maintained corpus-wide df the generic filter reads).
    generic = _insert_entity(
        test_db, name="Standup", canonical_key="standup", doc_count=4
    )
    for doc in (ids["d1"], ids["d2"], ids["d3"], ids["d4"]):
        _insert_mention(test_db, entity_id=generic, document_id=doc)
    scope = PersonScope(
        seed_entity_uuid=dana,
        entity_uuids=tuple(sorted((*scope.entity_uuids, generic))),
        document_uuids=scope.document_uuids,
    )
    # corpus_N = 4 docs with mentions; ratio 0.75 → cap round(3.0) = 3.
    # generic df 4 > 3 → suppressed; the cluster topics df 2 <= 3 → kept.
    ctx = graph_rag_search(
        test_db,
        _make_cfg(graph_generic_df_ratio=0.75),
        "",
        backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE,
        person="dana lee",
    )

    assert ctx.explanation is not None
    assert ctx.explanation.generic_df_cap == 3
    assert "standup" not in _all_theme_keys(ctx)
    assert _theme_keysets(ctx) == {
        frozenset({"pricing", "billing"}),
        frozenset({"roadmap", "analytics"}),
    }


def test_themes_suppresses_corpus_generic_with_small_in_scope_df(
    test_db: psycopg.Connection[Any],
) -> None:
    """REGRESSION (G2 review FIX A): a CORPUS-generic entity with a small in-scope
    df is suppressed from X's themes.

    Before the fix the generic filter compared each candidate's IN-SCOPE df
    (max = |X's docs|, usually small) against the corpus-sized cap, so a
    tenant-ubiquitous entity that co-occurred with X in only a few of X's docs
    slipped through and polluted X's theme groups. The filter now reads the
    corpus-wide ``graph_entities.doc_count`` and compares it to the corpus cap
    (matching derive-time suppression in ``_recompute_aggregates``).

    Here the generic topic's in-scope df is 2 (<= cap 3) — so the OLD filter
    would have KEPT it and let it join the theme via its co-occurrence edges to
    the two real topics — while its corpus-wide df is 6 (> cap 3), so the FIXED
    filter suppresses it. The two genuinely in-scope topics (corpus df 2) stay.
    """
    _seed_directory(test_db, [("dana lee", "dana@x.com")])
    dana = _insert_entity(
        test_db, entity_type="person", name="Dana Lee",
        canonical_key="dana lee", doc_count=2,
    )
    # Two legitimately in-scope topics (corpus df 2 <= cap → retained).
    t1 = _insert_entity(
        test_db, name="Negotiation", canonical_key="negotiation", doc_count=2
    )
    t2 = _insert_entity(
        test_db, name="Proposal", canonical_key="proposal", doc_count=2
    )
    # A tenant-GENERIC topic: corpus-wide doc_count 6, but it co-occurs with Dana
    # in only 2 of her docs (in-scope df 2).
    generic = _insert_entity(
        test_db, name="Weekly Sync", canonical_key="weekly-sync", doc_count=6
    )
    d1 = _insert_doc(test_db, title="X1", content="negotiation proposal sync one")
    d2 = _insert_doc(test_db, title="X2", content="negotiation proposal sync two")
    for ent in (dana, t1, t2, generic):
        for doc in (d1, d2):
            _insert_mention(test_db, entity_id=ent, document_id=doc)
    # Four MORE corpus docs that mention the generic but NOT Dana → its corpus df
    # is 6 while its in-scope (Dana's) df stays 2. corpus_N = 6 distinct docs.
    for i in range(4):
        extra = _insert_doc(test_db, title=f"G{i}", content="weekly sync recap")
        _insert_mention(test_db, entity_id=generic, document_id=extra)
    # All three eligible pairs co-occur in Dana's docs, so WITHOUT the fix the
    # generic would join the theme via its edges to the two real topics.
    for doc in (d1, d2):
        _insert_contribution(test_db, document_id=doc, a=t1, b=t2)
        _insert_contribution(test_db, document_id=doc, a=t1, b=generic)
        _insert_contribution(test_db, document_id=doc, a=t2, b=generic)
    scope = PersonScope(
        seed_entity_uuid=dana,
        entity_uuids=tuple(sorted((t1, t2, generic))),
        document_uuids=tuple(sorted((d1, d2))),
    )

    # corpus_N = 6 (d1, d2 + 4 extra), ratio 0.5 → cap = round(3.0) = 3.
    ctx = graph_rag_search(
        test_db,
        _make_cfg(graph_generic_df_ratio=0.5),
        "",
        backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE,
        person="dana lee",
    )

    assert ctx.explanation is not None
    assert ctx.explanation.generic_df_cap == 3
    # Corpus-generic topic (corpus df 6 > 3) suppressed even though its in-scope
    # df (2) is <= the cap — exactly the case the old in-scope-df filter missed.
    assert "weekly-sync" not in _all_theme_keys(ctx)
    # The two legitimately in-scope topics (corpus df 2 <= 3) are retained.
    assert _theme_keysets(ctx) == {frozenset({"negotiation", "proposal"})}


# --------------------------------------------------------------------------- #
# 4. Doc + snippet population, ranking determinism
# --------------------------------------------------------------------------- #
def test_themes_populates_docs_and_snippets(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    dana, scope, ids = _build_two_clusters(test_db)
    for key in ("d1", "d2", "d3", "d4"):
        _add_chunk(test_db, fake_embedder, ids[key], f"body for {key}")

    ctx = graph_rag_search(
        test_db, _make_cfg(), "", backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE, person="dana lee",
    )

    by_keys = {frozenset(e.canonical_key for e in t.entities): t for t in ctx.themes}
    pricing_group = by_keys[frozenset({"pricing", "billing"})]
    roadmap_group = by_keys[frozenset({"roadmap", "analytics"})]
    # Each group's representative docs are exactly its cluster's docs.
    assert set(pricing_group.doc_ids) == {ids["d1"], ids["d2"]}
    assert set(roadmap_group.doc_ids) == {ids["d3"], ids["d4"]}
    # Context-level docs cover all four and carry snippets (leading-chunk path).
    assert {d.document_id for d in ctx.docs} == {ids[k] for k in ("d1", "d2", "d3", "d4")}
    assert all(d.snippet for d in ctx.docs)


def test_themes_ranking_is_deterministic(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    dana, scope, ids = _build_two_clusters(test_db)
    for key in ("d1", "d2", "d3", "d4"):
        _add_chunk(test_db, fake_embedder, ids[key], f"body for {key}")
    backend = FakeScopeBackend({dana: scope})

    ctx1 = graph_rag_search(
        test_db, _make_cfg(), "", backend=backend, mode=THEMES_MODE, person="dana lee"
    )
    ctx2 = graph_rag_search(
        test_db, _make_cfg(), "", backend=backend, mode=THEMES_MODE, person="dana lee"
    )

    assert _digest(ctx1) == _digest(ctx2)
    # group_id is the 0-based rank; ids are dense and ascending.
    assert [t.group_id for t in ctx1.themes] == list(range(len(ctx1.themes)))


def test_themes_theme_limit_truncates(test_db: psycopg.Connection[Any]) -> None:
    dana, scope, _ = _build_two_clusters(test_db)

    ctx = graph_rag_search(
        test_db,
        _make_cfg(graph_theme_limit=1),
        "",
        backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE,
        person="dana lee",
    )

    assert len(ctx.themes) == 1


# --------------------------------------------------------------------------- #
# 5. --synthesize DI seam + never-raise discipline
# --------------------------------------------------------------------------- #
def test_themes_synthesize_populates_summaries(
    test_db: psycopg.Connection[Any],
) -> None:
    dana, scope, _ = _build_two_clusters(test_db)
    enricher = _FakeEnricher(summary="A synthesized theme.")

    ctx = graph_rag_search(
        test_db, _make_cfg(), "", backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE, person="dana lee", synthesize=True, enricher=enricher,
    )

    assert ctx.themes
    assert all(t.summary == "A synthesized theme." for t in ctx.themes)
    # The enricher was called per group with the person + entity names.
    assert len(enricher.calls) == len(ctx.themes)
    assert all(call["person"] == "Dana Lee" for call in enricher.calls)
    assert all(call["entity_names"] for call in enricher.calls)


def test_themes_synthesize_off_by_default(test_db: psycopg.Connection[Any]) -> None:
    dana, scope, _ = _build_two_clusters(test_db)
    enricher = _FakeEnricher()

    ctx = graph_rag_search(
        test_db, _make_cfg(), "", backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE, person="dana lee", enricher=enricher,
    )

    assert all(t.summary is None for t in ctx.themes)
    assert enricher.calls == []  # default off → never consulted


def test_themes_synthesize_enricher_returns_none(
    test_db: psycopg.Connection[Any],
) -> None:
    """Ollama down (enricher returns None) → summary=None, retrieval succeeds."""
    dana, scope, _ = _build_two_clusters(test_db)

    ctx = graph_rag_search(
        test_db, _make_cfg(), "", backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE, person="dana lee",
        synthesize=True, enricher=_FakeEnricher(summary=None),
    )

    assert ctx.themes  # retrieval still returns the themes
    assert all(t.summary is None for t in ctx.themes)


def test_themes_synthesize_enricher_raises_never_raises(
    test_db: psycopg.Connection[Any],
) -> None:
    """A misbehaving enricher that raises is absorbed → summary=None, no crash."""
    dana, scope, _ = _build_two_clusters(test_db)

    ctx = graph_rag_search(
        test_db, _make_cfg(), "", backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE, person="dana lee",
        synthesize=True, enricher=_FakeEnricher(error=RuntimeError("ollama timeout")),
    )

    assert ctx.themes
    assert all(t.summary is None for t in ctx.themes)


def test_themes_synthesize_without_enricher_warns(
    test_db: psycopg.Connection[Any],
) -> None:
    """synthesize=True with no injected enricher → summaries None, still returns."""
    dana, scope, _ = _build_two_clusters(test_db)

    ctx = graph_rag_search(
        test_db, _make_cfg(), "", backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE, person="dana lee", synthesize=True, enricher=None,
    )

    assert ctx.themes
    assert all(t.summary is None for t in ctx.themes)


# --------------------------------------------------------------------------- #
# 6. Person resolution + validation + never-raise-on-empty
# --------------------------------------------------------------------------- #
def test_themes_requires_person(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(ValueError):
        graph_rag_search(
            test_db, _make_cfg(), "", backend=FakeScopeBackend(), mode=THEMES_MODE
        )
    with pytest.raises(ValueError):
        graph_rag_search(
            test_db, _make_cfg(), "", backend=FakeScopeBackend(),
            mode=THEMES_MODE, person="   ",
        )


def test_themes_person_not_found_raises(test_db: psycopg.Connection[Any]) -> None:
    # Empty directory → resolve_person_to_keys raises PersonNotFound.
    with pytest.raises(PersonNotFound):
        graph_rag_search(
            test_db, _make_cfg(), "", backend=FakeScopeBackend(),
            mode=THEMES_MODE, person="ghost person",
        )


def test_themes_person_ambiguous_raises(test_db: psycopg.Connection[Any]) -> None:
    _seed_directory(
        test_db, [("dana lee", "dana@x.com"), ("sam lee", "sam@x.com")]
    )
    with pytest.raises(PersonAmbiguous):
        graph_rag_search(
            test_db, _make_cfg(), "", backend=FakeScopeBackend(),
            mode=THEMES_MODE, person="lee",
        )


def test_themes_person_resolves_but_no_graph_entity_is_empty(
    test_db: psycopg.Connection[Any],
) -> None:
    """Person in the directory but absent from the graph → empty, never-raise."""
    _seed_directory(test_db, [("dana lee", "dana@x.com")])
    backend = FakeScopeBackend()

    ctx = graph_rag_search(
        test_db, _make_cfg(), "", backend=backend, mode=THEMES_MODE, person="dana lee"
    )

    assert ctx.themes == []
    assert ctx.docs == []
    assert ctx.entities == []
    assert backend.calls == []  # no seed entity → scope_person never called
    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == []


def test_themes_empty_scope_is_empty(test_db: psycopg.Connection[Any]) -> None:
    """A resolvable person with an empty scope returns empty-but-valid context."""
    _seed_directory(test_db, [("dana lee", "dana@x.com")])
    dana = _insert_entity(
        test_db, entity_type="person", name="Dana Lee", canonical_key="dana lee"
    )
    backend = FakeScopeBackend(
        {dana: PersonScope(seed_entity_uuid=dana, entity_uuids=(), document_uuids=())}
    )

    ctx = graph_rag_search(
        test_db, _make_cfg(), "", backend=backend, mode=THEMES_MODE, person="dana lee"
    )

    assert ctx.themes == []
    assert ctx.docs == []
    assert backend.calls[0]["seed"] == dana  # scope was attempted


def test_themes_scope_backend_error_propagates(
    test_db: psycopg.Connection[Any],
) -> None:
    """A scope cap-exceed is a loud failure, not silently swallowed (spec §6a)."""
    _seed_directory(test_db, [("dana lee", "dana@x.com")])
    dana = _insert_entity(
        test_db, entity_type="person", name="Dana Lee", canonical_key="dana lee"
    )
    backend = FakeScopeBackend(error=GraphBackendError("scope cap exceeded"))

    with pytest.raises(GraphBackendError):
        graph_rag_search(
            test_db, _make_cfg(), "", backend=backend,
            mode=THEMES_MODE, person="dana lee",
        )
    assert dana  # entity existed so scope_person was reached


# --------------------------------------------------------------------------- #
# 7. Live-AGE integration — THE HEADLINE PROOF
# --------------------------------------------------------------------------- #
def _reconcile_themes_corpus(
    conn: psycopg.Connection[Any],
    backend: AgeBackend,
    fake_embedder: Any,
    *,
    tenant: str,
) -> dict[str, str]:
    """Build a live person+concept graph: Dana + owner over two topic clusters.

    Four documents — two PRICING (topics pricing+billing), two ROADMAP (topics
    roadmap+analytics) — with Dana + an owner as participants. Concepts come
    from a deterministic fake extractor. ``owner_keys`` is empty so the owner
    becomes a real graph person co-mentioned with Dana, so the themes-path owner
    exclusion is genuinely exercised. Returns external_id → doc_id.
    """
    _seed_directory(
        conn, [("dana lee", "dana@x.com"), ("owner one", "owner@x.com")]
    )
    extractor = _FakeConceptExtractor(
        {
            "PRICING": [
                ("topic", "pricing", "Pricing"),
                ("topic", "billing", "Billing"),
            ],
            "ROADMAP": [
                ("topic", "roadmap", "Roadmap"),
                ("topic", "analytics", "Analytics"),
            ],
        }
    )
    rcfg = ReconcileConfig(
        tenant_id=tenant,
        generic_df_ratio=_NO_SUPPRESS,
        concepts_enabled=True,
        owner_keys=frozenset(),
    )
    docs: dict[str, str] = {}
    rows = [
        ("p1", "PRICING enterprise tiers and billing cycles"),
        ("p2", "PRICING strategy and billing renewals"),
        ("r1", "ROADMAP planning and analytics dashboards"),
        ("r2", "ROADMAP milestones and analytics review"),
    ]
    for external_id, body in rows:
        doc = _seed_gmail_doc(
            conn,
            external_id=f"{tenant}-{external_id}",
            participants=[("dana lee", "dana@x.com"), ("owner one", "owner@x.com")],
            content=body,
            tenant_marker=tenant,
        )
        _add_chunk(conn, fake_embedder, doc, body)
        reconcile_document(conn, doc, backend=backend, config=rcfg, extractor=extractor)
        docs[external_id] = doc
    return docs


def test_live_themes_headline_two_topic_clusters(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    backend = _backend(test_db)
    docs = _reconcile_themes_corpus(
        test_db, backend, fake_embedder, tenant="default"
    )

    ctx = graph_rag_search(
        test_db,
        _make_cfg(owner_participants=frozenset({"owner one"})),
        "",
        backend=backend,
        mode=THEMES_MODE,
        person="dana lee",
    )

    assert ctx.mode == THEMES_MODE
    assert ctx.person == "Dana Lee"
    # The headline: exactly the two topic clusters surface as themes.
    assert len(ctx.themes) == 2
    assert _theme_keysets(ctx) == {
        frozenset({"pricing", "billing"}),
        frozenset({"roadmap", "analytics"}),
    }
    # Dana (seed) and the owner are excluded from every theme.
    assert "dana lee" not in _all_theme_keys(ctx)
    assert "owner one" not in _all_theme_keys(ctx)
    # Each theme carries representative X-docs; the context docs carry snippets.
    for theme in ctx.themes:
        assert theme.doc_ids
    assert ctx.docs and all(d.snippet for d in ctx.docs)
    # Representative docs are drawn only from Dana's documents.
    assert {d.document_id for d in ctx.docs} <= set(docs.values())
    assert ctx.explanation is not None
    assert ctx.explanation.seed_entity_ids == [
        _entity_id(test_db, "default", "dana lee")
    ]


def test_live_themes_synthesize_attaches_summary(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    backend = _backend(test_db)
    _reconcile_themes_corpus(test_db, backend, fake_embedder, tenant="default")
    enricher = _FakeEnricher(summary="Themes Dana discussed.")

    ctx = graph_rag_search(
        test_db,
        _make_cfg(owner_participants=frozenset({"owner one"})),
        "",
        backend=backend,
        mode=THEMES_MODE,
        person="dana lee",
        synthesize=True,
        enricher=enricher,
    )

    assert ctx.themes
    assert all(t.summary == "Themes Dana discussed." for t in ctx.themes)
    # The synthesis prompt carried the representative doc titles.
    assert any(call["doc_titles"] for call in enricher.calls)


def test_live_themes_is_tenant_scoped(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A second tenant's identical corpus never leaks into a default-tenant query."""
    backend = _backend(test_db)
    default_docs = _reconcile_themes_corpus(
        test_db, backend, fake_embedder, tenant="default"
    )
    other_docs = _reconcile_themes_corpus(
        test_db, backend, fake_embedder, tenant="other"
    )

    default_ctx = graph_rag_search(
        test_db,
        _make_cfg(owner_participants=frozenset({"owner one"})),
        "",
        backend=backend,
        mode=THEMES_MODE,
        person="dana lee",
    )
    other_ctx = graph_rag_search(
        test_db,
        _make_cfg(owner_participants=frozenset({"owner one"})),
        "",
        backend=backend,
        mode=THEMES_MODE,
        person="dana lee",
        tenant="other",
    )

    assert default_ctx.tenant_id == "default"
    assert other_ctx.tenant_id == "other"
    # Each tenant's representative docs come only from its own corpus.
    assert {d.document_id for d in default_ctx.docs} <= set(default_docs.values())
    assert {d.document_id for d in other_ctx.docs} <= set(other_docs.values())
    assert {d.document_id for d in default_ctx.docs}.isdisjoint(
        set(other_docs.values())
    )
    # Both still recover the two topic clusters within their own tenant.
    assert _theme_keysets(default_ctx) == {
        frozenset({"pricing", "billing"}),
        frozenset({"roadmap", "analytics"}),
    }
    assert _theme_keysets(other_ctx) == _theme_keysets(default_ctx)


# --------------------------------------------------------------------------- #
# A2: person-scoped doc count threaded onto theme entities
# --------------------------------------------------------------------------- #
def test_themes_entities_carry_person_scoped_doc_count(
    test_db: psycopg.Connection[Any],
) -> None:
    """Theme entities carry ``scoped_doc_count`` = docs co-occurring with the person.

    Entity "e" (synthetic topic) co-occurs with person "dana lee" in 2 of the
    person's scoped docs (d1, d2); its corpus-wide ``doc_count`` is 3 (d1, d2, d3).
    Entity "f" appears only in d1 and d2 (doc_count=2). After themes retrieval,
    ``e.scoped_doc_count`` must equal 2 (not 3) and ``e.doc_count`` must remain 3.

    Corpus has 3 total docs with mentions → cap = round(3 × 1.0) = 3.
    entity e: corpus doc_count=3, is_generic(3, cap=3)=False (3 not > 3) → eligible.
    entity f: corpus doc_count=2, is_generic(2, cap=3)=False → eligible.
    """
    _seed_directory(test_db, [("dana lee", "dana@x.com")])
    dana = _insert_entity(
        test_db, entity_type="person", name="Dana Lee", canonical_key="dana lee"
    )
    # Entity "e" appears in 3 corpus docs (doc_count=3 stored):
    #   d1, d2 are in Dana's scope; d3 is outside (no Dana mention).
    e = _insert_entity(
        test_db, name="Synthetic Topic E", canonical_key="e", doc_count=3
    )
    # Entity "f" appears only in d1 and d2 (doc_count=2 stored).
    f = _insert_entity(
        test_db, name="Synthetic Topic F", canonical_key="f", doc_count=2
    )
    d1 = _insert_doc(test_db, title="Scoped1", content="scoped doc one")
    d2 = _insert_doc(test_db, title="Scoped2", content="scoped doc two")
    d3 = _insert_doc(test_db, title="Extra", content="extra doc out of scope")
    # Dana's scope covers d1 + d2; both e and f appear in d1 and d2.
    for doc in (d1, d2):
        _insert_mention(test_db, entity_id=dana, document_id=doc)
        _insert_mention(test_db, entity_id=e, document_id=doc)
        _insert_mention(test_db, entity_id=f, document_id=doc)
        _insert_contribution(test_db, document_id=doc, a=e, b=f)
    # e also appears in d3 (out of scope — no dana mention there).
    _insert_mention(test_db, entity_id=e, document_id=d3)
    scope = PersonScope(
        seed_entity_uuid=dana,
        entity_uuids=tuple(sorted((e, f))),
        document_uuids=tuple(sorted((d1, d2))),
    )

    ctx = graph_rag_search(
        test_db,
        _make_cfg(),
        "",
        backend=FakeScopeBackend({dana: scope}),
        mode=THEMES_MODE,
        person="dana lee",
    )

    assert ctx.themes, "expected at least one theme group"
    all_entities = {en.canonical_key: en for t in ctx.themes for en in t.entities}
    assert "e" in all_entities, f"entity 'e' not in theme entities: {list(all_entities)}"
    entity_e = all_entities["e"]
    # scoped_doc_count = distinct docs in Dana's scope that mention e = 2.
    assert entity_e.scoped_doc_count == 2
    # doc_count = corpus-wide maintained count as stored in graph_entities = 3.
    assert entity_e.doc_count == 3
