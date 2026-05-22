"""Smoke + unit tests for the GraphRAG benchmark generator (wave G1-d).

The benchmark fixture (:mod:`tests.graphrag.benchmark_fixture`) exists to feed
G2's P95 perf gate at full scale (50k entities / 500k CO_OCCURS / 1M mentions /
10 tenants). The full-scale load is FAR too slow for the default suite, so here
we only:

* exercise the deterministic pure helpers (split / pair generation / validation)
  with no DB, and
* run one TINY end-to-end load (50 entities, 2 tenants) through the SAME code
  path — relational COPY + AGE materialization — and assert the relational +
  AGE row counts, so the generator is fully covered without the heavy load.

All data is synthetic (``Person {i}``); no PII.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import psycopg
import pytest

from brain.db import DEFAULT_GRAPH_NAME
from brain.graph_rag.backends import AgeBackend
from tests.graphrag.benchmark_fixture import (
    BENCHMARK_SPEC_FULL,
    BenchmarkSpec,
    _default_documents,
    _edge_attrs,
    _edge_pairs,
    _mention_pairs,
    _split,
    _uuid,
    _validate_tenant,
    generate_benchmark_graph,
)

# A tiny spec that drives the identical code path as BENCHMARK_SPEC_FULL.
# Explicit ``documents`` keeps the expected counts obvious (6 docs/tenant).
_SMOKE = BenchmarkSpec(
    entities=50, cooccur_edges=40, mentions=120, tenants=2, documents=12
)


# --------------------------------------------------------------------------- #
# AGE assertion helpers (independent raw Cypher)
# --------------------------------------------------------------------------- #
def _cypher_scalar(
    conn: psycopg.Connection[Any], query: str, params: Mapping[str, Any]
) -> list[tuple[Any, ...]]:
    conn.execute('SET search_path = ag_catalog, "$user", public')
    try:
        rows = conn.execute(
            f"SELECT * FROM ag_catalog.cypher('{DEFAULT_GRAPH_NAME}', "
            f"$$ {query} $$, %s::ag_catalog.agtype) AS (v ag_catalog.agtype)",
            (json.dumps(params),),
        ).fetchall()
    finally:
        conn.execute("RESET search_path")
    return rows


def _age_count(conn: psycopg.Connection[Any], query: str) -> int:
    return int(str(_cypher_scalar(conn, query, {})[0][0]))


def _rel_count(conn: psycopg.Connection[Any], table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608 - fixed name
    assert row is not None
    return int(row[0])


# --------------------------------------------------------------------------- #
# 1. Pure helpers (no DB)
# --------------------------------------------------------------------------- #
def test_split_is_even_and_sums() -> None:
    assert _split(10, 3) == [4, 3, 3]
    assert sum(_split(1_000_000, 10)) == 1_000_000
    assert _split(0, 4) == [0, 0, 0, 0]


def test_split_rejects_zero_parts() -> None:
    with pytest.raises(ValueError, match="parts must be >= 1"):
        _split(5, 0)


def test_uuid_is_deterministic() -> None:
    assert _uuid(1234, "bench-t0", "entity", 7) == _uuid(1234, "bench-t0", "entity", 7)
    # Different seed / tenant / kind / index → different uuid.
    assert _uuid(1234, "bench-t0", "entity", 7) != _uuid(9999, "bench-t0", "entity", 7)
    assert _uuid(1234, "bench-t0", "entity", 7) != _uuid(1234, "bench-t1", "entity", 7)
    assert _uuid(1234, "bench-t0", "entity", 7) != _uuid(1234, "bench-t0", "doc", 7)


def test_default_documents_uses_explicit_when_set() -> None:
    assert _default_documents(BenchmarkSpec(10, 0, 10, 1, documents=7)) == 7


def test_default_documents_derives_when_unset() -> None:
    # max(mentions//10, entities, tenants, 1).
    assert _default_documents(BenchmarkSpec(50, 0, 120, 2)) == 50
    assert _default_documents(BenchmarkSpec(5, 0, 1000, 3)) == 100


def test_mention_pairs_are_unique() -> None:
    pairs = _mention_pairs(60, 6, 25)
    assert len(pairs) == 60
    assert len(set(pairs)) == 60  # unique (doc, entity)


def test_edge_pairs_are_unique_unordered_and_distinct_endpoints() -> None:
    pairs = _edge_pairs(40, 25)
    assert len(pairs) == 40
    unordered = {frozenset(p) for p in pairs}
    assert len(unordered) == 40  # no duplicate unordered pair
    assert all(a != b for a, b in pairs)  # no self-edge


def test_validate_tenant_rejects_too_many_mentions() -> None:
    with pytest.raises(ValueError, match="mentions"):
        _validate_tenant(entities=5, documents=2, mentions=11, edges=0)


def test_validate_tenant_rejects_edges_without_two_entities() -> None:
    with pytest.raises(ValueError, match="< 2 entities"):
        _validate_tenant(entities=1, documents=10, mentions=1, edges=1)


def test_validate_tenant_rejects_too_many_edges() -> None:
    with pytest.raises(ValueError, match="max distinct entity pairs"):
        _validate_tenant(entities=3, documents=10, mentions=3, edges=4)


def test_validate_tenant_rejects_edges_without_enough_mentions() -> None:
    with pytest.raises(ValueError, match="every entity mentioned"):
        _validate_tenant(entities=5, documents=10, mentions=3, edges=2)


def test_full_spec_constants() -> None:
    assert BENCHMARK_SPEC_FULL.entities == 50_000
    assert BENCHMARK_SPEC_FULL.cooccur_edges == 500_000
    assert BENCHMARK_SPEC_FULL.mentions == 1_000_000
    assert BENCHMARK_SPEC_FULL.tenants == 10
    assert BENCHMARK_SPEC_FULL.documents == 100_000


# --------------------------------------------------------------------------- #
# 2. Tiny relational-only load
# --------------------------------------------------------------------------- #
def test_smoke_relational_load_counts(test_db: psycopg.Connection[Any]) -> None:
    result = generate_benchmark_graph(test_db, _SMOKE, materialize_age=False)

    assert result.tenants == 2
    assert result.documents == 12
    assert result.entities == 50
    assert result.mentions == 120
    assert result.contributions == 40
    assert result.relationships == 40
    assert result.age_materialized is False

    assert _rel_count(test_db, "graph_entities") == 50
    assert _rel_count(test_db, "graph_entity_mentions") == 120
    assert _rel_count(test_db, "graph_edge_contributions") == 40
    assert _rel_count(test_db, "graph_relationships") == 40
    assert _rel_count(test_db, "documents") == 12


# --------------------------------------------------------------------------- #
# 3. Tiny load WITH AGE materialization
# --------------------------------------------------------------------------- #
def test_smoke_age_materialization_counts(test_db: psycopg.Connection[Any]) -> None:
    result = generate_benchmark_graph(test_db, _SMOKE, materialize_age=True)
    assert result.age_materialized is True

    # Entity vertices: every entity gets a vertex (50 total across 2 tenants).
    assert _age_count(test_db, "MATCH (e:Entity) RETURN count(e)") == 50
    # CO_OCCURS edges mirror graph_relationships (40 total).
    assert _age_count(test_db, "MATCH ()-[r:CO_OCCURS]->() RETURN count(r)") == 40
    # MENTIONED_IN edges mirror the mentions (120 total).
    assert _age_count(test_db, "MATCH ()-[r:MENTIONED_IN]->() RETURN count(r)") == 120
    # Document vertices: 6 docs/tenant carry mentions → 12 total.
    assert _age_count(test_db, "MATCH (d:Document) RETURN count(d)") == 12
    # Per-tenant isolation: each tenant holds exactly its own 25 entities.
    assert (
        _age_count(test_db, "MATCH (e:Entity {tenant_id: 'bench-t0'}) RETURN count(e)")
        == 25
    )
    assert (
        _age_count(test_db, "MATCH (e:Entity {tenant_id: 'bench-t1'}) RETURN count(e)")
        == 25
    )


# --------------------------------------------------------------------------- #
# 4. Tiny load WITH AGE materialization via the FAST bulk path (age_bulk=True)
# --------------------------------------------------------------------------- #
# The G2 perf gate (test_graphrag_benchmark_gate.py) loads the full corpus via
# the direct-COPY bulk path. These tiny tests prove that path is correct: it
# produces the SAME counts the per-edge primitive path does and yields a graph
# the real AgeBackend can traverse — without ever paying the full-scale load.
def test_smoke_age_bulk_materialization_counts(
    test_db: psycopg.Connection[Any],
) -> None:
    """The bulk loader's AGE counts equal the primitive path's (bulk≡primitive)."""
    result = generate_benchmark_graph(test_db, _SMOKE, materialize_age=True, age_bulk=True)
    assert result.age_materialized is True

    # Identical to test_smoke_age_materialization_counts (the primitive path).
    assert _age_count(test_db, "MATCH (e:Entity) RETURN count(e)") == 50
    assert _age_count(test_db, "MATCH ()-[r:CO_OCCURS]->() RETURN count(r)") == 40
    assert _age_count(test_db, "MATCH ()-[r:MENTIONED_IN]->() RETURN count(r)") == 120
    assert _age_count(test_db, "MATCH (d:Document) RETURN count(d)") == 12
    assert (
        _age_count(test_db, "MATCH (e:Entity {tenant_id: 'bench-t0'}) RETURN count(e)")
        == 25
    )
    assert (
        _age_count(test_db, "MATCH (e:Entity {tenant_id: 'bench-t1'}) RETURN count(e)")
        == 25
    )


def test_smoke_age_bulk_traverse_reads_back(
    test_db: psycopg.Connection[Any],
) -> None:
    """A bulk-loaded graph is queryable by the real AgeBackend.traverse.

    Deterministic from the generator: per tenant ``_edge_pairs(20, 25)`` yields
    edges ``(i, i+1)`` for ``i in 0..19``; entity 0 has exactly the ``(0, 1)``
    edge (index 0 → weight ``_edge_attrs(0)[0]`` = 0.01). With the floor at 0.0 a
    depth-1 traversal from entity 0 reaches exactly entity 1 at that affinity.
    """
    generate_benchmark_graph(test_db, _SMOKE, materialize_age=True, age_bulk=True)

    tenant = "bench-t0"
    seed = str(_uuid(_SMOKE.seed, tenant, "entity", 0))
    neighbour = str(_uuid(_SMOKE.seed, tenant, "entity", 1))
    expected_weight = _edge_attrs(0)[0]  # 0.01

    backend = AgeBackend()
    hits = backend.traverse(
        test_db, tenant, seed, depth=1, frontier_cap=100, min_edge_weight=0.0
    )

    assert [h.entity_uuid for h in hits] == [neighbour]
    assert hits[0].affinity == pytest.approx(expected_weight)
    assert hits[0].hops == 1
    assert hits[0].tenant_id == tenant


def test_smoke_age_bulk_tenant_isolated(test_db: psycopg.Connection[Any]) -> None:
    """Bulk-loaded tenants are isolated — t0's seed never reaches a t1 entity."""
    generate_benchmark_graph(test_db, _SMOKE, materialize_age=True, age_bulk=True)

    seed_t0 = str(_uuid(_SMOKE.seed, "bench-t0", "entity", 0))
    backend = AgeBackend()
    hits = backend.traverse(
        test_db, "bench-t0", seed_t0, depth=2, frontier_cap=100, min_edge_weight=0.0
    )

    # Every reached entity is a bench-t0 entity (none of bench-t1's 25 entities).
    t1_ids = {str(_uuid(_SMOKE.seed, "bench-t1", "entity", i)) for i in range(25)}
    assert all(h.entity_uuid not in t1_ids for h in hits)
    assert hits  # the traversal is non-trivial (reaches at least one neighbour)
