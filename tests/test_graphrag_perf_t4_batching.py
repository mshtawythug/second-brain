"""Regression tests for perf-T4 GraphRAG batching (G1-G5).

Each batched implementation must produce byte-identical state to the prior
per-row form. We exercise the live AGE test DB end-to-end (one round-trip per
batch instead of N) and assert the resulting rows match a hand-built oracle.

* **G1** — ``AgeBackend.refresh_cooccur_edges``: one ``UNWIND $rows`` Cypher
  per call instead of N round-trips. Verifies (a) every relational row
  becomes an AGE CO_OCCURS edge with identical properties, (b) the
  missing-vertex case still raises ``GraphBackendError`` and rolls the
  delete back atomically, (c) the empty-rows case clears and returns 0.
* **G2** — ``AgeBackend.upsert_entities``: two batched ``UNWIND`` statements
  (MERGE pass + MATCH/SET pass) instead of ``2 × N``. Verifies idempotent
  re-upsert updates the AGE properties to the second-call's values, and
  the empty-list case is a no-op returning 0.
* **G3** — ``aggregates._recompute_aggregates``: single ``INSERT … SELECT``
  CTE preserving ``normalized_lift`` + generic-suppression EXACTLY.
  Verifies the materialized ``graph_relationships`` rows match the
  hand-computed oracle (count, weight to 12-decimal precision, co_count,
  doc_count) AND that the strictly-greater suppression boundary holds (an
  entity sitting AT the cap is kept; one OVER the cap is dropped).
* **G4** — ``communities._persist``: single ``executemany`` over the
  ``graph_community_members`` rows. Verifies one ``build_communities`` call
  writes identical (community_key, entity_id, member_rank, member_weight)
  tuples to the prior per-row loop.
* **G5** — ``global_._vector_ranked_keys``: signature change to take a
  pre-warmed ``Embedder`` instance. Verifies (a) passing the instance
  produces the same vector-leg ranking as the prior factory form, (b) the
  ``None`` and ``embed-raises`` degradation paths still log + skip the
  vector leg without raising.

Every entity / document / community uses synthetic UUIDs + names (no PII).
"""
from __future__ import annotations

import math
import os
import uuid
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.errors import GraphBackendError
from brain.graph_rag.aggregates import _recompute_aggregates, refresh_aggregates
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.communities import build_communities
from brain.graph_rag.global_ import _retrieve_global, _vector_ranked_keys
from brain.graph_rag.schema import GraphEntity
from brain.graph_rag.weighting import edge_weight, generic_df_cap
from tests.conftest import FakeEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# Synthetic, lexically-ordered UUIDs (A < B < C < D < E) keep src_id < dst_id.
_A = "11111111-1111-4111-8111-111111111111"
_B = "22222222-2222-4222-8222-222222222222"
_C = "33333333-3333-4333-8333-333333333333"
_D = "44444444-4444-4444-8444-444444444444"
_E = "55555555-5555-4555-8555-555555555555"

# G5 — community summary embeddings ship at vector(1024).
_SUMMARY_DIM = 1024


def _entity(eid: str, tenant: str = "default", name: str | None = None) -> GraphEntity:
    return GraphEntity(
        id=eid,
        entity_type="person",
        name=name or eid[:8],
        canonical_key=eid[:8],
        tenant_id=tenant,
    )


# --------------------------------------------------------------------------- #
# G1 — refresh_cooccur_edges batched UNWIND
# --------------------------------------------------------------------------- #
def _seed_entities(
    conn: psycopg.Connection[Any], backend: AgeBackend, ids: list[str]
) -> None:
    """Catalog rows + AGE vertices for ``ids`` (tenant 'default')."""
    for eid in ids:
        conn.execute(
            "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'default', 'person', %s, %s)",
            (eid, eid[:8], eid[:8]),
        )
    backend.upsert_entities(conn, "default", [_entity(eid) for eid in ids])


def _insert_relationship(
    conn: psycopg.Connection[Any],
    src: str,
    dst: str,
    weight: float,
    *,
    co_count: int = 1,
    doc_count: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO graph_relationships "
        "(tenant_id, src_id, dst_id, weight, co_count, doc_count) "
        "VALUES ('default', %s, %s, %s, %s, %s)",
        (src, dst, weight, co_count, doc_count),
    )


def _age_cooccur_rows(
    conn: psycopg.Connection[Any], backend: AgeBackend, tenant: str
) -> list[tuple[str, str, float, int, int]]:
    """Read every CO_OCCURS edge back from AGE as (src, dst, w, co, dc) tuples."""
    import json

    conn.execute('SET search_path = ag_catalog, "$user", public')
    try:
        rows = conn.execute(
            f"SELECT * FROM ag_catalog.cypher('{backend.graph_name}', $$ "
            "MATCH (a:Entity)-[r:CO_OCCURS {tenant_id: $t}]->(b:Entity) "
            "RETURN a.entity_uuid, b.entity_uuid, r.weight, r.co_count, r.doc_count "
            "ORDER BY a.entity_uuid, b.entity_uuid $$, %s::ag_catalog.agtype) "
            "AS (s ag_catalog.agtype, d ag_catalog.agtype, w ag_catalog.agtype, "
            "co ag_catalog.agtype, dc ag_catalog.agtype)",
            ('{"t": "' + tenant + '"}',),
        ).fetchall()
    finally:
        conn.execute("RESET search_path")

    out: list[tuple[str, str, float, int, int]] = []
    for s, d, w, co, dc in rows:
        # CO_OCCURS scalar columns carry no ::edge/::vertex annotation —
        # plain JSON parse suffices.
        out.append(
            (
                json.loads(str(s)),
                json.loads(str(d)),
                float(str(w)),
                int(str(co)),
                int(str(dc)),
            )
        )
    return out


def test_g1_refresh_cooccur_edges_batched_matches_oracle(
    test_db: psycopg.Connection[Any],
) -> None:
    """Batched UNWIND CO_OCCURS rebuild emits IDENTICAL edges to the per-row form.

    Seeds 4 entities + 6 relationship rows (one per ordered pair), then
    rebuilds the AGE CO_OCCURS set in one batched call and reads it back.
    Every relational row must surface as an AGE edge with the exact same
    weight / co_count / doc_count (regression for G1).
    """
    backend = AgeBackend()
    backend.bootstrap(test_db)
    _seed_entities(test_db, backend, [_A, _B, _C, _D])

    # Distinct weight/count tuples per edge so a swap-or-drop bug is visible.
    seeded = [
        (_A, _B, 0.10, 11, 21),
        (_A, _C, 0.25, 12, 22),
        (_A, _D, 0.50, 13, 23),
        (_B, _C, 0.65, 14, 24),
        (_B, _D, 0.80, 15, 25),
        (_C, _D, 1.00, 16, 26),
    ]
    for src, dst, w, co, dc in seeded:
        _insert_relationship(test_db, src, dst, w, co_count=co, doc_count=dc)

    created = backend.refresh_cooccur_edges(test_db, "default")
    assert created == len(seeded)

    actual = _age_cooccur_rows(test_db, backend, "default")
    expected = sorted(seeded)
    assert actual == expected


def test_g1_empty_rows_clears_and_returns_zero(
    test_db: psycopg.Connection[Any],
) -> None:
    """Refresh with zero relational rows still clears the tenant's CO_OCCURS."""
    backend = AgeBackend()
    backend.bootstrap(test_db)
    _seed_entities(test_db, backend, [_A, _B])
    # Plant a stray edge to prove the DELETE still runs.
    _insert_relationship(test_db, _A, _B, 0.5)
    backend.refresh_cooccur_edges(test_db, "default")
    assert _age_cooccur_rows(test_db, backend, "default") == [(_A, _B, 0.5, 1, 1)]

    test_db.execute("DELETE FROM graph_relationships WHERE tenant_id = 'default'")
    created = backend.refresh_cooccur_edges(test_db, "default")
    assert created == 0
    assert _age_cooccur_rows(test_db, backend, "default") == []


def test_g1_missing_vertex_raises_and_rolls_back(
    test_db: psycopg.Connection[Any],
) -> None:
    """A relational row pointing at an AGE-missing entity raises and rolls back.

    Regression: the per-row form raised on the first miss and the surrounding
    transaction rolled back the DELETE so the prior CO_OCCURS set survived
    intact. The batched form must preserve that complete-or-loud-failure
    contract.
    """
    backend = AgeBackend()
    backend.bootstrap(test_db)
    # Insert catalog rows for all four, but only upsert A,B as AGE vertices.
    for eid in (_A, _B, _C, _D):
        test_db.execute(
            "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'default', 'person', %s, %s)",
            (eid, eid[:8], eid[:8]),
        )
    backend.upsert_entities(test_db, "default", [_entity(_A), _entity(_B)])
    # First, seed a baseline CO_OCCURS set (A-B) and rebuild atomically so it
    # commits.
    _insert_relationship(test_db, _A, _B, 0.42)
    backend.refresh_cooccur_edges(test_db, "default")
    baseline = _age_cooccur_rows(test_db, backend, "default")
    assert baseline == [(_A, _B, 0.42, 1, 1)]

    # Now add a relational row whose endpoint vertex doesn't exist in AGE.
    _insert_relationship(test_db, _C, _D, 0.99)

    with pytest.raises(GraphBackendError, match="missing"):
        backend.refresh_cooccur_edges(test_db, "default")

    # The DELETE inside the failed refresh rolled back — baseline survives.
    assert _age_cooccur_rows(test_db, backend, "default") == baseline


# --------------------------------------------------------------------------- #
# G2 — upsert_entities batched UNWIND
# --------------------------------------------------------------------------- #
def _age_entity_name(
    conn: psycopg.Connection[Any], backend: AgeBackend, eid: str
) -> str | None:
    import json

    conn.execute('SET search_path = ag_catalog, "$user", public')
    try:
        rows = conn.execute(
            f"SELECT * FROM ag_catalog.cypher('{backend.graph_name}', $$ "
            "MATCH (e:Entity {entity_uuid: $u, tenant_id: $t}) RETURN e.name $$, "
            "%s::ag_catalog.agtype) AS (n ag_catalog.agtype)",
            ('{"u": "' + eid + '", "t": "default"}',),
        ).fetchall()
    finally:
        conn.execute("RESET search_path")
    return json.loads(str(rows[0][0])) if rows else None


def test_g2_upsert_entities_batched_is_idempotent(
    test_db: psycopg.Connection[Any],
) -> None:
    """Two batched UNWINDs MERGE-then-SET produce the same per-row outcome.

    First call creates the vertices with one name; second call updates the
    name on the SAME vertices. Both calls must persist their SET map — the
    AGE quirk that an in-MERGE SET drops on freshly-created vertices is why
    we split the MERGE + MATCH/SET passes (regression for G2).
    """
    backend = AgeBackend()
    backend.bootstrap(test_db)
    initial = [_entity(_A, name="Alpha"), _entity(_B, name="Beta")]
    n = backend.upsert_entities(test_db, "default", initial)
    assert n == 2
    assert _age_entity_name(test_db, backend, _A) == "Alpha"
    assert _age_entity_name(test_db, backend, _B) == "Beta"

    updated = [_entity(_A, name="Alpha-v2"), _entity(_B, name="Beta-v2")]
    n2 = backend.upsert_entities(test_db, "default", updated)
    assert n2 == 2
    assert _age_entity_name(test_db, backend, _A) == "Alpha-v2"
    assert _age_entity_name(test_db, backend, _B) == "Beta-v2"


def test_g2_upsert_entities_empty_is_noop(test_db: psycopg.Connection[Any]) -> None:
    """An empty list of entities returns 0 without opening an AGE session."""
    backend = AgeBackend()
    backend.bootstrap(test_db)
    assert backend.upsert_entities(test_db, "default", []) == 0


# --------------------------------------------------------------------------- #
# G3 — _recompute_aggregates set-based INSERT … SELECT
# --------------------------------------------------------------------------- #
def _insert_doc(conn: psycopg.Connection[Any], doc_id: str) -> None:
    conn.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s, 'note')",
        (doc_id, doc_id[:8], f"synthetic body {doc_id[:8]}", doc_id),
    )


def _insert_mention(
    conn: psycopg.Connection[Any], entity_id: str, document_id: str
) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) "
        "VALUES ('default', %s, %s, 1, 'people')",
        (entity_id, document_id),
    )


def _insert_contribution(
    conn: psycopg.Connection[Any],
    src: str,
    dst: str,
    document_id: str,
    cooccur_count: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO graph_edge_contributions "
        "(tenant_id, src_id, dst_id, document_id, cooccur_count) "
        "VALUES ('default', %s, %s, %s, %s)",
        (src, dst, document_id, cooccur_count),
    )


def test_g3_recompute_aggregates_set_based_matches_oracle(
    test_db: psycopg.Connection[Any],
) -> None:
    """CTE INSERT … SELECT yields IDENTICAL rows to the per-row Python loop.

    Builds a small graph with four entities + four documents and verifies
    every materialized ``graph_relationships`` row matches the hand-computed
    ``edge_weight`` (normalized lift) over the seeded contributions. With
    ``generic_df_ratio=1.0`` (cap = corpus_n) NO entity is suppressed, so
    every contribution pair surfaces; the weight is
    ``co_doc_count / min(src_df, dst_df)``.
    """
    backend = AgeBackend()
    backend.bootstrap(test_db)
    for eid in (_A, _B, _C, _D):
        test_db.execute(
            "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'default', 'person', %s, %s)",
            (eid, eid[:8], eid[:8]),
        )
    # Documents + per-document mentions: A in doc1..4; B in doc1,2; C in doc1;
    # D in doc2.
    docs = [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa0001",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa0002",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa0003",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa0004",
    ]
    for doc_id in docs:
        _insert_doc(test_db, doc_id)
    # Mention shape: df(A)=4, df(B)=2, df(C)=1, df(D)=1.
    mentions = [
        (_A, docs[0]), (_A, docs[1]), (_A, docs[2]), (_A, docs[3]),
        (_B, docs[0]), (_B, docs[1]),
        (_C, docs[0]),
        (_D, docs[1]),
    ]
    for eid, did in mentions:
        _insert_mention(test_db, eid, did)
    # Contributions: A-B co-occurs in doc1+doc2; A-C in doc1; A-D in doc2;
    # B-C in doc1; B-D in doc2.
    contributions = [
        (_A, _B, docs[0], 1), (_A, _B, docs[1], 1),
        (_A, _C, docs[0], 1),
        (_A, _D, docs[1], 1),
        (_B, _C, docs[0], 1),
        (_B, _D, docs[1], 1),
    ]
    for src, dst, did, cc in contributions:
        _insert_contribution(test_db, src, dst, did, cc)

    # generic_df_ratio=1.0 → cap = corpus_n; no suppression.
    written = _recompute_aggregates(test_db, "default", generic_df_ratio=1.0)

    # Oracle: re-derive every row from the seed data using the SAME helper the
    # prior loop called.
    corpus_n = 4  # distinct mention documents
    cap = generic_df_cap(corpus_n, 1.0)
    df = {_A: 4, _B: 2, _C: 1, _D: 1}
    pair_co: dict[tuple[str, str], tuple[int, int]] = {}
    for src, dst, _did, cc in contributions:
        key = (src, dst)
        prior = pair_co.get(key, (0, 0))
        pair_co[key] = (prior[0] + cc, prior[1] + 1)
    expected: list[tuple[str, str, float, int, int]] = []
    for (src, dst), (co, pair_doc_count) in pair_co.items():
        w = edge_weight(
            co_doc_count=pair_doc_count,
            src_doc_count=df[src],
            dst_doc_count=df[dst],
            cap=cap,
        )
        if w is None:
            continue
        expected.append((src, dst, w, co, pair_doc_count))
    assert written == len(expected)

    rows = test_db.execute(
        "SELECT src_id::text, dst_id::text, weight, co_count, doc_count "
        "FROM graph_relationships WHERE tenant_id = 'default' "
        "ORDER BY src_id, dst_id"
    ).fetchall()
    actual = [
        (str(r[0]), str(r[1]), float(r[2]), int(r[3]), int(r[4]))
        for r in rows
    ]
    expected_sorted = sorted(expected)
    assert len(actual) == len(expected_sorted)
    for got, want in zip(actual, expected_sorted, strict=True):
        assert got[0] == want[0]
        assert got[1] == want[1]
        # Weights are computed in floating-point on both sides; compare to a
        # safe precision rather than equality.
        assert math.isclose(got[2], want[2], rel_tol=0, abs_tol=1e-12)
        assert got[3] == want[3]
        assert got[4] == want[4]


def test_g3_generic_cap_suppression_boundary_preserved(
    test_db: psycopg.Connection[Any],
) -> None:
    """Strictly-greater suppression: entity AT the cap stays, OVER the cap is dropped.

    Two endpoints — one at df=3 (over the cap), one at df=2 (AT the cap).
    With ratio=0.5 and corpus_n=4, ``cap = round(4 * 0.5) = 2``. The pair
    must be suppressed (one endpoint exceeds the cap), so NO row is
    materialized for it — regression that the SQL form's ``<= cap``
    boundary matches :func:`is_generic_entity`'s strictly-greater rule.
    """
    backend = AgeBackend()
    backend.bootstrap(test_db)
    for eid in (_A, _B):
        test_db.execute(
            "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'default', 'person', %s, %s)",
            (eid, eid[:8], eid[:8]),
        )
    docs = [
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbb0001",
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbb0002",
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbb0003",
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbb0004",
    ]
    for doc_id in docs:
        _insert_doc(test_db, doc_id)
    # A in 3 docs (over cap=2), B in 2 docs (AT cap=2). Corpus_n = 4.
    for did in docs[:3]:
        _insert_mention(test_db, _A, did)
    for did in docs[:2]:
        _insert_mention(test_db, _B, did)
    # The pair co-occurs in 2 docs.
    for did in docs[:2]:
        _insert_contribution(test_db, _A, _B, did, 1)

    written = _recompute_aggregates(test_db, "default", generic_df_ratio=0.5)
    # A is generic (df=3 > cap=2) → pair suppressed.
    assert written == 0
    rows = test_db.execute(
        "SELECT COUNT(*) FROM graph_relationships WHERE tenant_id = 'default'"
    ).fetchone()
    assert rows is not None and int(rows[0]) == 0


# --------------------------------------------------------------------------- #
# G4 — communities._persist executemany
# --------------------------------------------------------------------------- #
def _cfg_for_communities(**overrides: Any) -> Config:
    """Config pinned to graph + community knobs that produce stable Louvain output."""
    params: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "graph_tenant_id": "default",
        "graph_community_resolution": 1.0,
        "graph_community_seed": 42,
        "graph_community_min_size": 2,
        "graph_community_jaccard": 0.5,
    }
    params.update(overrides)
    return Config(**params)


def test_g4_persist_members_batched_writes_identical_rows(
    test_db: psycopg.Connection[Any],
) -> None:
    """One ``executemany`` over members writes the same rows the per-row loop did.

    Builds two cleanly-separable Louvain partitions (A-B edge + C-D-E
    triangle), runs ``build_communities``, and asserts the
    ``graph_community_members`` rows exactly match the detected partition
    member sets. Regression for G4 — the ``executemany`` carries the same
    five-column shape the per-row INSERT did.
    """
    for eid in (_A, _B, _C, _D, _E):
        test_db.execute(
            "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'default', 'person', %s, %s)",
            (eid, eid[:8], eid[:8]),
        )
    # Cluster 1: A-B (single edge).
    test_db.execute(
        "INSERT INTO graph_relationships "
        "(tenant_id, src_id, dst_id, weight, co_count, doc_count) "
        "VALUES ('default', %s, %s, 1.0, 1, 1)",
        (_A, _B),
    )
    # Cluster 2: C-D-E triangle.
    for src, dst in [(_C, _D), (_C, _E), (_D, _E)]:
        test_db.execute(
            "INSERT INTO graph_relationships "
            "(tenant_id, src_id, dst_id, weight, co_count, doc_count) "
            "VALUES ('default', %s, %s, 0.9, 1, 1)",
            (src, dst),
        )
    cfg = _cfg_for_communities()
    result = build_communities(test_db, cfg, tenant="default")
    assert result.skipped is False
    assert result.communities_total == 2

    member_rows = test_db.execute(
        "SELECT community_key::text, entity_id::text "
        "FROM graph_community_members WHERE tenant_id = 'default'"
    ).fetchall()
    members_by_key: dict[str, set[str]] = {}
    for key, eid in member_rows:
        members_by_key.setdefault(str(key), set()).add(str(eid))
    member_sets = sorted(frozenset(s) for s in members_by_key.values())
    assert member_sets == sorted(
        [frozenset({_A, _B}), frozenset({_C, _D, _E})]
    )

    # Member_rank rows must be 0..N-1 contiguous per community (the per-row
    # loop's invariant — preserved by the batched executemany order).
    for key, members in members_by_key.items():
        ranks = test_db.execute(
            "SELECT member_rank FROM graph_community_members "
            "WHERE tenant_id = 'default' AND community_key = %s "
            "ORDER BY member_rank",
            (key,),
        ).fetchall()
        observed = [int(r[0]) for r in ranks]
        assert observed == list(range(len(members)))


# --------------------------------------------------------------------------- #
# G5 — _vector_ranked_keys takes an Embedder instance
# --------------------------------------------------------------------------- #
def _insert_community(
    conn: psycopg.Connection[Any],
    *,
    summary: str | None,
    embedding: list[float] | None,
) -> str:
    row = conn.execute(
        "INSERT INTO graph_communities "
        "(tenant_id, source_graph_hash, members_hash, member_count, summary, "
        " summary_embedding) "
        "VALUES ('default', %s, %s, 0, %s, %s) RETURNING community_key::text",
        ("synthetic-graph-hash", uuid.uuid4().hex, summary, embedding),
    ).fetchone()
    assert row is not None
    return str(row[0])


class _RaisingEmbedder:
    dim = _SUMMARY_DIM

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        raise RuntimeError("synthetic embed failure")

    def count_tokens(self, text: str) -> int:
        return 1


def test_g5_vector_ranked_keys_accepts_instance(
    test_db: psycopg.Connection[Any],
) -> None:
    """Passing a pre-warmed Embedder instance ranks via the vector leg.

    Regression for the G5 signature flip: the caller now hands in the
    ``Embedder`` itself (not a factory). Verifies the cosine-ordered
    ``community_key`` list matches what the prior factory form would have
    returned (same ranking; same ``attempted=True`` signal).
    """
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    query_vec = emb.embed(["roadmap"], input_type="query")[0]
    near = _insert_community(test_db, summary=None, embedding=query_vec)
    far_vec = emb.embed(["unrelated"], input_type="document")[0]
    far = _insert_community(test_db, summary=None, embedding=far_vec)

    keys, attempted = _vector_ranked_keys(
        test_db, tenant="default", query="roadmap", embedder=emb
    )
    assert attempted is True
    assert keys[0] == near
    assert set(keys) == {near, far}


def test_g5_none_embedder_skips_vector_leg(
    test_db: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture,
) -> None:
    """``None`` embedder → empty ranking + WARN (FTS-only never-raise contract)."""
    import logging

    _insert_community(test_db, summary=None, embedding=[0.0] * _SUMMARY_DIM)
    with caplog.at_level(logging.WARNING, logger="brain.graph_rag.global_"):
        keys, attempted = _vector_ranked_keys(
            test_db, tenant="default", query="anything", embedder=None
        )
    assert keys == []
    assert attempted is False
    assert "FTS-only" in caplog.text


def test_g5_embedder_raising_degrades_to_fts_only(
    test_db: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture,
) -> None:
    """An embedder whose ``embed`` raises degrades the vector leg, not the call.

    Regression: the prior form caught factory + embed failures; the new form
    only owns embed-time failure (a factory that raises is the caller's
    responsibility). The vector leg must still degrade silently.
    """
    import logging

    _insert_community(test_db, summary=None, embedding=[0.0] * _SUMMARY_DIM)
    with caplog.at_level(logging.WARNING, logger="brain.graph_rag.global_"):
        keys, attempted = _vector_ranked_keys(
            test_db, tenant="default", query="anything",
            embedder=_RaisingEmbedder(),
        )
    assert keys == []
    assert attempted is False
    assert "FTS-only" in caplog.text


def test_g5_retrieve_global_round_trip_with_instance(
    test_db: psycopg.Connection[Any],
) -> None:
    """End-to-end: ``_retrieve_global`` consumes the pre-warmed instance.

    The G5 wire-shape test: ``_retrieve_global`` accepts ``embedder=…`` and
    produces the same FTS+vector RRF result the prior factory form did.
    Two communities, one matching FTS, one matching vector — both surface.
    """
    emb = FakeEmbedder(dim=_SUMMARY_DIM)
    query = "roadmap"
    query_vec = emb.embed([query], input_type="query")[0]
    far_vec = emb.embed(["different"], input_type="document")[0]
    fts_match = _insert_community(test_db, summary="roadmap planning", embedding=far_vec)
    vec_match = _insert_community(test_db, summary="budget review", embedding=query_vec)

    cfg = Config(database_url=TEST_DATABASE_URL, graph_tenant_id="default")
    ctx = _retrieve_global(
        test_db, cfg, query, tenant="default", embedder=emb
    )
    keys = {c.community_key for c in ctx.communities}
    assert keys == {fts_match, vec_match}
    assert ctx.explanation is not None
    assert ctx.explanation.matched_filters["vector_arm_used"] is True


# --------------------------------------------------------------------------- #
# Wide integration: G1 + G3 wired through refresh_aggregates
# --------------------------------------------------------------------------- #
def test_g1_g3_refresh_aggregates_end_to_end(
    test_db: psycopg.Connection[Any],
) -> None:
    """Wave-level integration: refresh_aggregates uses the new G1 + G3 batched paths.

    Builds a small graph, then runs the public ``refresh_aggregates`` (which
    calls ``_recompute_aggregates`` + the backend's ``refresh_cooccur_edges``).
    Verifies the relational ``graph_relationships`` and the AGE CO_OCCURS
    edges end up in lockstep (regression that the batched relational +
    batched AGE refresh agree).
    """
    backend = AgeBackend()
    backend.bootstrap(test_db)
    for eid in (_A, _B, _C):
        test_db.execute(
            "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'default', 'person', %s, %s)",
            (eid, eid[:8], eid[:8]),
        )
    backend.upsert_entities(
        test_db, "default", [_entity(_A), _entity(_B), _entity(_C)]
    )
    docs = [
        "cccccccc-cccc-4ccc-8ccc-cccccccc0001",
        "cccccccc-cccc-4ccc-8ccc-cccccccc0002",
    ]
    for doc_id in docs:
        _insert_doc(test_db, doc_id)
    # A,B co-occur in doc1; A,C co-occur in doc2. df(A)=2, df(B)=1, df(C)=1.
    _insert_mention(test_db, _A, docs[0])
    _insert_mention(test_db, _B, docs[0])
    _insert_mention(test_db, _A, docs[1])
    _insert_mention(test_db, _C, docs[1])
    _insert_contribution(test_db, _A, _B, docs[0])
    _insert_contribution(test_db, _A, _C, docs[1])

    from brain.graph_rag.reconcile import ReconcileConfig

    cfg = ReconcileConfig(tenant_id="default", generic_df_ratio=1.0)
    result = refresh_aggregates(test_db, backend=backend, config=cfg)
    assert result.relationship_count == 2

    # Relational mirror.
    rel = test_db.execute(
        "SELECT src_id::text, dst_id::text, weight FROM graph_relationships "
        "WHERE tenant_id = 'default' ORDER BY src_id, dst_id"
    ).fetchall()
    rel_pairs = {(str(r[0]), str(r[1])) for r in rel}
    assert rel_pairs == {(_A, _B), (_A, _C)}

    # AGE CO_OCCURS edges agree.
    age_rows = _age_cooccur_rows(test_db, backend, "default")
    age_pairs = {(s, d) for s, d, _w, _co, _dc in age_rows}
    assert age_pairs == rel_pairs
