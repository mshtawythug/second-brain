"""Tests for the GraphRAG community-detection core (wave G3-b, spec §17c).

Two layers:

* **Pure** (no DB) — hashing determinism, Louvain detection (2-cluster split,
  MIN_SIZE drop, MAX cap, member ranking + subgraph stats, repeat-run
  determinism), and the greedy best-Jaccard stable-identity matcher.
* **Integration** (the ``test_db`` fixture → the Apache-AGE test instance on
  port 5434) — :func:`brain.graph_rag.communities.build_communities` persists
  ``graph_communities`` + ``graph_community_members`` tenant-scoped, the dirty
  gate skips on an unchanged graph and ``force`` overrides, a Jaccard-reused
  community_key preserves its summary (delta-gate), a removed community is
  deleted with its members CASCADE'd, and two tenants never mix.

All entities are synthetic (Px / Qx canonical keys); no PII.
"""
from __future__ import annotations

import os
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.errors import GraphTenantError
from brain.graph_rag.communities import (
    BUILD_VERSION,
    CommunityBuildResult,
    DetectedCommunity,
    build_communities,
    compute_members_hash,
    compute_source_graph_hash,
    detect_communities,
    match_communities,
)
from brain.graph_rag.schema import CommunityMember

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# Louvain knobs used across the pure tests — the chosen Config defaults.
_RESOLUTION = 1.0
_SEED = 1234


# --------------------------------------------------------------------------- #
# Pure helpers — fixtures for the detection / matching tests
# --------------------------------------------------------------------------- #
def _two_cluster_edges() -> list[tuple[str, str, float]]:
    """Two dense triangles joined by one weak bridge (n3—n4 @ 0.05)."""
    return [
        ("n1", "n2", 0.8),
        ("n1", "n3", 0.8),
        ("n2", "n3", 0.8),
        ("n4", "n5", 0.8),
        ("n4", "n6", 0.8),
        ("n5", "n6", 0.8),
        ("n3", "n4", 0.05),
    ]


def _clique_edges(nodes: list[str], weight: float = 0.8) -> list[tuple[str, str, float]]:
    """All canonical pairs of ``nodes`` at ``weight`` (a complete subgraph)."""
    edges: list[tuple[str, str, float]] = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            src, dst = sorted((nodes[i], nodes[j]))
            edges.append((src, dst, weight))
    return edges


def _dc(ids: set[str]) -> DetectedCommunity:
    """A minimal :class:`DetectedCommunity` over ``ids`` for matcher tests."""
    member_ids = sorted(ids)
    return DetectedCommunity(
        members_hash=compute_members_hash(member_ids),
        members=tuple(
            CommunityMember(entity_id=mid, member_rank=r)
            for r, mid in enumerate(member_ids)
        ),
        member_count=len(member_ids),
        edge_count=0,
        total_weight=0.0,
    )


# --------------------------------------------------------------------------- #
# Pure: hashing determinism
# --------------------------------------------------------------------------- #
def test_members_hash_is_order_independent() -> None:
    assert compute_members_hash(["c", "a", "b"]) == compute_members_hash(["a", "b", "c"])


def test_members_hash_distinguishes_sets() -> None:
    assert compute_members_hash(["a", "b"]) != compute_members_hash(["a", "b", "c"])


def test_source_graph_hash_is_order_independent() -> None:
    edges = _two_cluster_edges()
    assert compute_source_graph_hash(edges) == compute_source_graph_hash(
        list(reversed(edges))
    )


def test_source_graph_hash_moves_on_weight_change() -> None:
    base = compute_source_graph_hash([("a", "b", 0.5)])
    bumped = compute_source_graph_hash([("a", "b", 0.6)])
    assert base != bumped


def test_source_graph_hash_empty_is_stable() -> None:
    assert compute_source_graph_hash([]) == compute_source_graph_hash([])


# --------------------------------------------------------------------------- #
# Pure: detection
# --------------------------------------------------------------------------- #
def test_detect_two_clusters() -> None:
    detected = detect_communities(
        _two_cluster_edges(), resolution=_RESOLUTION, seed=_SEED, min_size=3
    )
    assert len(detected) == 2
    member_sets = sorted((set(dc.member_ids) for dc in detected), key=sorted)
    assert {"n1", "n2", "n3"} in member_sets
    assert {"n4", "n5", "n6"} in member_sets


def test_detect_is_deterministic() -> None:
    edges = _two_cluster_edges()
    first = detect_communities(edges, resolution=_RESOLUTION, seed=_SEED, min_size=3)
    second = detect_communities(edges, resolution=_RESOLUTION, seed=_SEED, min_size=3)
    # Frozen dataclasses compare by value, incl. the ranked-member tuples.
    assert first == second


def test_detect_empty_graph_returns_empty() -> None:
    assert detect_communities([], resolution=_RESOLUTION, seed=_SEED, min_size=3) == []


def test_detect_min_size_drops_small_communities() -> None:
    # A triangle (3) + a disconnected single edge (2 nodes).
    edges = [("a", "b", 0.8), ("a", "c", 0.8), ("b", "c", 0.8), ("x", "y", 0.8)]
    keep_all = detect_communities(edges, resolution=_RESOLUTION, seed=_SEED, min_size=2)
    assert sorted(dc.member_count for dc in keep_all) == [2, 3]
    triangle_only = detect_communities(
        edges, resolution=_RESOLUTION, seed=_SEED, min_size=3
    )
    assert len(triangle_only) == 1
    assert set(triangle_only[0].member_ids) == {"a", "b", "c"}


def test_detect_max_cap_keeps_largest() -> None:
    # Three disconnected cliques of sizes 5, 4, 3.
    edges = (
        _clique_edges(["a1", "a2", "a3", "a4", "a5"])
        + _clique_edges(["b1", "b2", "b3", "b4"])
        + _clique_edges(["c1", "c2", "c3"])
    )
    detected = detect_communities(
        edges, resolution=_RESOLUTION, seed=_SEED, min_size=3, max_communities=2
    )
    assert len(detected) == 2
    # The size-3 clique is dropped; the 5 and 4 survive.
    assert sorted(dc.member_count for dc in detected) == [4, 5]


def test_detect_member_ranking_and_stats() -> None:
    detected = detect_communities(
        _clique_edges(["a", "b", "c"], weight=0.8),
        resolution=_RESOLUTION,
        seed=_SEED,
        min_size=3,
    )
    assert len(detected) == 1
    community = detected[0]
    # Triangle: 3 edges, total weight 3 * 0.8.
    assert community.edge_count == 3
    assert community.total_weight == pytest.approx(2.4)
    # member_rank is a 0..n-1 permutation, ordered by descending weighted degree.
    ranks = [m.member_rank for m in community.members]
    assert ranks == [0, 1, 2]
    weights = [m.member_weight for m in community.members]
    assert weights == sorted(weights, reverse=True)


# --------------------------------------------------------------------------- #
# Pure: stable-identity matching
# --------------------------------------------------------------------------- #
def test_match_identical_set_reuses_key() -> None:
    existing = [("key-a", frozenset({"n1", "n2", "n3"}))]
    assigned, deleted = match_communities(
        [_dc({"n1", "n2", "n3"})], existing, threshold=0.5
    )
    assert assigned == ["key-a"]
    assert deleted == []


def test_match_small_perturbation_reuses_key() -> None:
    existing = [("key-a", frozenset({"n1", "n2", "n3"}))]
    # |∩|=3, |∪|=4 → Jaccard 0.75 >= 0.5.
    assigned, deleted = match_communities(
        [_dc({"n1", "n2", "n3", "n4"})], existing, threshold=0.5
    )
    assert assigned == ["key-a"]
    assert deleted == []


def test_match_large_change_mints_new_key() -> None:
    existing = [("key-a", frozenset({"n1", "n2", "n3"}))]
    assigned, deleted = match_communities(
        [_dc({"x7", "x8", "x9"})], existing, threshold=0.5
    )
    assert assigned == [None]
    assert deleted == ["key-a"]  # old community no longer present → delete


def test_match_threshold_boundary() -> None:
    existing = [("key-a", frozenset({"n1", "n2", "n3"}))]
    detected = [_dc({"n1", "n4", "n5"})]  # |∩|=1, |∪|=5 → Jaccard 0.2
    assert match_communities(detected, existing, threshold=0.5)[0] == [None]
    assert match_communities(detected, existing, threshold=0.2)[0] == ["key-a"]


def test_match_zero_overlap_never_reused_even_at_threshold_zero() -> None:
    existing = [("key-a", frozenset({"n1", "n2", "n3"}))]
    assigned, _deleted = match_communities([_dc({"z1", "z2"})], existing, threshold=0.0)
    assert assigned == [None]


def test_match_greedy_does_not_collapse_two_new_onto_one_key() -> None:
    existing = [("key-a", frozenset({"n1", "n2", "n3"}))]
    # Both detected overlap key-a; only the first (processing order) may claim it.
    assigned, deleted = match_communities(
        [_dc({"n1", "n2"}), _dc({"n1", "n3"})], existing, threshold=0.2
    )
    assert assigned.count("key-a") == 1
    assert None in assigned
    assert deleted == []


# --------------------------------------------------------------------------- #
# Integration helpers (test_db, port 5434)
# --------------------------------------------------------------------------- #
def _cfg(**overrides: Any) -> Config:
    params: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "graph_tenant_id": "default",
    }
    params.update(overrides)
    return Config(**params)


def _insert_entity(
    conn: psycopg.Connection[Any], tenant: str, name: str, canonical_key: str
) -> str:
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, 'person', %s, %s) RETURNING id::text",
        (tenant, name, canonical_key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_rel(
    conn: psycopg.Connection[Any], tenant: str, a: str, b: str, weight: float
) -> None:
    src, dst = sorted((a, b))
    conn.execute(
        "INSERT INTO graph_relationships "
        "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
        "VALUES (%s, %s, %s, 'co_occurs', %s, 1, 1)",
        (tenant, src, dst, weight),
    )


def _seed_two_clusters(
    conn: psycopg.Connection[Any], tenant: str = "default"
) -> tuple[list[str], list[str]]:
    """Two dense triangles + a weak bridge, scoped to ``tenant``."""
    c1 = [
        _insert_entity(conn, tenant, f"P-{tenant}-{i}", f"p-{tenant}-{i}")
        for i in range(3)
    ]
    c2 = [
        _insert_entity(conn, tenant, f"Q-{tenant}-{i}", f"q-{tenant}-{i}")
        for i in range(3)
    ]
    for a, b in [(0, 1), (0, 2), (1, 2)]:
        _insert_rel(conn, tenant, c1[a], c1[b], 0.8)
        _insert_rel(conn, tenant, c2[a], c2[b], 0.8)
    _insert_rel(conn, tenant, c1[2], c2[0], 0.05)  # weak bridge
    return c1, c2


def _members_by_community(
    conn: psycopg.Connection[Any], tenant: str
) -> dict[str, set[str]]:
    rows = conn.execute(
        "SELECT community_key::text, entity_id::text FROM graph_community_members "
        "WHERE tenant_id = %s",
        (tenant,),
    ).fetchall()
    out: dict[str, set[str]] = {}
    for key, entity_id in rows:
        out.setdefault(str(key), set()).add(str(entity_id))
    return out


def _community_for_entity(
    conn: psycopg.Connection[Any], tenant: str, entity_id: str
) -> str:
    for key, members in _members_by_community(conn, tenant).items():
        if entity_id in members:
            return key
    raise AssertionError(f"no community contains entity {entity_id}")


def _community_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM graph_communities WHERE tenant_id = %s", (tenant,)
    ).fetchone()
    assert row is not None
    return int(row[0])


# --------------------------------------------------------------------------- #
# Integration: persistence + counts
# --------------------------------------------------------------------------- #
def test_build_persists_communities_and_members(
    test_db: psycopg.Connection[Any],
) -> None:
    c1, c2 = _seed_two_clusters(test_db)
    result = build_communities(test_db, _cfg(), tenant="default")

    assert isinstance(result, CommunityBuildResult)
    assert result.skipped is False
    assert result.dirty is True
    assert result.communities_total == 2
    assert result.created == 2
    assert result.reused == 0
    assert result.deleted == 0
    assert _community_count(test_db, "default") == 2

    members = _members_by_community(test_db, "default")
    member_sets = sorted(members.values(), key=sorted)
    assert member_sets == sorted([set(c1), set(c2)], key=sorted)

    # Every community row carries the build_version + the run's fingerprint.
    rows = test_db.execute(
        "SELECT build_version, source_graph_hash, member_count, members_hash "
        "FROM graph_communities WHERE tenant_id = 'default'"
    ).fetchall()
    assert all(str(r[0]) == BUILD_VERSION for r in rows)
    assert {str(r[1]) for r in rows} == {result.source_graph_hash}
    assert all(int(r[2]) == 3 for r in rows)
    assert all(str(r[3]) and not str(r[3]).startswith("pending:") for r in rows)


def test_build_dirty_gate_skips_on_unchanged_graph(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_two_clusters(test_db)
    first = build_communities(test_db, _cfg(), tenant="default")
    assert first.skipped is False

    second = build_communities(test_db, _cfg(), tenant="default")
    assert second.skipped is True
    assert second.dirty is False
    assert second.communities_total == 2
    assert second.created == 0 and second.reused == 0 and second.deleted == 0


def test_build_force_rebuilds_unchanged_graph(
    test_db: psycopg.Connection[Any],
) -> None:
    c1, _c2 = _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")
    key_before = _community_for_entity(test_db, "default", c1[0])

    forced = build_communities(test_db, _cfg(), tenant="default", force=True)
    assert forced.skipped is False
    assert forced.dirty is False  # graph unchanged, but force ran the work
    assert forced.reused == 2  # identical sets → Jaccard 1.0 → keys reused
    assert forced.created == 0
    # Stable identity: the same entity stays in the same community_key.
    assert _community_for_entity(test_db, "default", c1[0]) == key_before


def test_build_reused_key_preserves_summary(
    test_db: psycopg.Connection[Any],
) -> None:
    c1, _c2 = _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")
    key = _community_for_entity(test_db, "default", c1[0])

    # G3-c would set these; seed a summary directly to prove the delta-gate.
    test_db.execute(
        "UPDATE graph_communities SET summary = %s, summary_model = %s, "
        "summary_at = NOW() WHERE tenant_id = 'default' AND community_key = %s",
        ("Cluster one summary.", "llama3.1:8b", key),
    )

    # Grow cluster one by a strongly-connected new member: membership changes
    # (members_hash moves) but Jaccard 3/4 = 0.75 >= 0.5 → key reused.
    e_new = _insert_entity(test_db, "default", "P-default-new", "p-default-new")
    for existing in c1:
        _insert_rel(test_db, "default", existing, e_new, 0.8)

    result = build_communities(test_db, _cfg(), tenant="default")
    assert result.skipped is False
    assert result.dirty is True
    assert result.reused >= 1

    # Same key, members grew, summary PRESERVED (delta-gate).
    assert _community_for_entity(test_db, "default", c1[0]) == key
    row = test_db.execute(
        "SELECT summary, summary_model, member_count FROM graph_communities "
        "WHERE tenant_id = 'default' AND community_key = %s",
        (key,),
    ).fetchone()
    assert row is not None
    assert str(row[0]) == "Cluster one summary."
    assert str(row[1]) == "llama3.1:8b"
    assert int(row[2]) == 4


def test_build_removes_vanished_community_and_cascades_members(
    test_db: psycopg.Connection[Any],
) -> None:
    c1, c2 = _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")
    assert _community_count(test_db, "default") == 2
    gone_key = _community_for_entity(test_db, "default", c2[0])

    # Remove cluster two's edges + the bridge → only cluster one remains a graph.
    test_db.execute(
        "DELETE FROM graph_relationships WHERE tenant_id = 'default' AND ("
        "  src_id::text = ANY(%s) OR dst_id::text = ANY(%s))",
        (c2, c2),
    )

    result = build_communities(test_db, _cfg(), tenant="default")
    assert result.deleted == 1
    assert _community_count(test_db, "default") == 1
    # The deleted community's members are gone (CASCADE).
    leftover = test_db.execute(
        "SELECT COUNT(*) FROM graph_community_members "
        "WHERE tenant_id = 'default' AND community_key = %s",
        (gone_key,),
    ).fetchone()
    assert leftover is not None and int(leftover[0]) == 0
    # The survivor is cluster one.
    survivor = _members_by_community(test_db, "default")
    assert list(survivor.values()) == [set(c1)]


def test_build_is_tenant_isolated(test_db: psycopg.Connection[Any]) -> None:
    a1, a2 = _seed_two_clusters(test_db, tenant="default")
    b1, b2 = _seed_two_clusters(test_db, tenant="other")

    build_communities(test_db, _cfg(graph_tenant_id="default"), tenant="default")
    build_communities(test_db, _cfg(graph_tenant_id="other"), tenant="other")

    assert _community_count(test_db, "default") == 2
    assert _community_count(test_db, "other") == 2

    default_entities = {
        e for members in _members_by_community(test_db, "default").values() for e in members
    }
    other_entities = {
        e for members in _members_by_community(test_db, "other").values() for e in members
    }
    assert default_entities == set(a1) | set(a2)
    assert other_entities == set(b1) | set(b2)
    assert default_entities.isdisjoint(other_entities)

    # Rebuilding one tenant (force) leaves the other tenant's rows untouched.
    other_keys_before = set(_members_by_community(test_db, "other").keys())
    build_communities(
        test_db, _cfg(graph_tenant_id="default"), tenant="default", force=True
    )
    assert set(_members_by_community(test_db, "other").keys()) == other_keys_before


def test_build_empty_graph_is_noop_not_skipped(
    test_db: psycopg.Connection[Any],
) -> None:
    # No entities / relationships at all.
    result = build_communities(test_db, _cfg(), tenant="default")
    assert result.communities_total == 0
    assert result.created == 0
    assert result.skipped is False  # zero-community tenants always re-evaluate
    assert _community_count(test_db, "default") == 0


def test_build_rejects_empty_tenant(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(GraphTenantError):
        build_communities(test_db, _cfg(), tenant="")
