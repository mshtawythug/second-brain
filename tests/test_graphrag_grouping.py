"""Unit tests for scoped-subgraph theme grouping (:mod:`brain.graph_rag.grouping`).

Pure-logic, DB-free, no monkey-patching. Builds in-memory ``GraphEntity`` /
``Edge`` value objects and asserts the §17b Q1 (Codex-ruled) grouping contract:
edge threshold + bridge guard + connected components + deterministic ranking.

The headline is :func:`test_acceptance_fixture_two_dense_clusters`, which encodes
the exact Codex acceptance fixture. The remaining tests cover the guard branches
(strong bridge kept, low bridge dropped, leaf bridge kept), determinism under
input reordering, ``theme_limit`` truncation, ranking tie-breaks, input hygiene
(dedup / self-loop / dangling endpoint), and parameter validation.

Entity ids are single lowercase letters so canonical ``src < dst`` ordering and
lexicographic tie-breaks are easy to read; they are synthetic, not real UUIDs.
"""
from __future__ import annotations

import pytest

from brain.errors import GroupingError
from brain.graph_rag.grouping import (
    DEFAULT_BRIDGE_KEEP_WEIGHT,
    DEFAULT_MIN_EDGE_WEIGHT,
    group_themes,
)
from brain.graph_rag.schema import Edge, GraphEntity, ThemeGroup


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _entity(eid: str, *, etype: str = "topic", key: str | None = None) -> GraphEntity:
    """A minimal theme-eligible entity; ``canonical_key`` defaults to ``topic-<id>``."""
    return GraphEntity(
        id=eid,
        entity_type=etype,
        name=eid.upper(),
        canonical_key=key if key is not None else f"topic-{eid}",
    )


def _edge(a: str, b: str, weight: float) -> Edge:
    """A canonical (``src < dst``) weighted edge — matches the DB CHECK shape."""
    src, dst = (a, b) if a < b else (b, a)
    return Edge(src_id=src, dst_id=dst, weight=weight)


def _ids(group: ThemeGroup) -> list[str]:
    return [e.id for e in group.entities]


def _summary(groups: list[ThemeGroup]) -> list[tuple[int, tuple[str, ...], float]]:
    """A comparable, order-stable digest used for determinism assertions."""
    return [(g.group_id, tuple(_ids(g)), round(g.score, 9)) for g in groups]


# Two 3-node dense clusters + one leaf, per the Codex acceptance fixture.
def _fixture_entities() -> list[GraphEntity]:
    return [_entity(c) for c in ("a", "b", "c", "d", "e", "f", "g")]


def _fixture_edges() -> list[Edge]:
    return [
        # Cluster 1 triangle {a, b, c}.
        _edge("a", "b", 0.70),
        _edge("b", "c", 0.75),
        _edge("a", "c", 0.80),
        # Cluster 2 triangle {d, e, f}.
        _edge("d", "e", 0.70),
        _edge("e", "f", 0.75),
        _edge("d", "f", 0.80),
        # Cross-cluster edge below threshold — dropped by the 0.20 floor.
        _edge("c", "d", 0.19),
        # Cross-cluster BRIDGE — the only surviving cross edge, dropped by guard.
        _edge("b", "e", 0.30),
        # LEAF attachment — a bridge with a size-1 side, kept.
        _edge("a", "g", 0.30),
    ]


# --------------------------------------------------------------------------- #
# Acceptance fixture (Codex Q1 — MUST pass)
# --------------------------------------------------------------------------- #
def test_acceptance_fixture_two_dense_clusters() -> None:
    groups = group_themes(_fixture_entities(), _fixture_edges())

    # Exactly two groups: the clusters do NOT merge.
    assert len(groups) == 2

    by_members = {frozenset(_ids(g)): g for g in groups}
    # Leaf g stays attached to its parent cluster {a, b, c}.
    assert frozenset({"a", "b", "c", "g"}) in by_members
    # The other cluster is untouched.
    assert frozenset({"d", "e", "f"}) in by_members

    cluster1 = by_members[frozenset({"a", "b", "c", "g"})]
    cluster2 = by_members[frozenset({"d", "e", "f"})]

    # Ranking: the 4-node cluster outranks the 3-node one (group_id 0 < 1).
    assert cluster1.group_id == 0
    assert cluster2.group_id == 1

    # Members are sorted deterministically by (canonical_key, entity_type, id).
    assert _ids(cluster1) == ["a", "b", "c", "g"]
    assert _ids(cluster2) == ["d", "e", "f"]

    # Cohesion = sum of surviving internal edge weights.
    assert cluster1.score == pytest.approx(0.70 + 0.75 + 0.80 + 0.30)
    assert cluster2.score == pytest.approx(0.70 + 0.75 + 0.80)

    # doc_ids / summary are the G2-f caller's concern — left at defaults here.
    assert cluster1.doc_ids == []
    assert cluster1.summary is None


def test_acceptance_fixture_is_deterministic_across_runs_and_orderings() -> None:
    entities = _fixture_entities()
    edges = _fixture_edges()

    baseline = _summary(group_themes(entities, edges))

    # Repeated invocations on the same input.
    for _ in range(5):
        assert _summary(group_themes(entities, edges)) == baseline

    # Reversed input order yields byte-identical groups (membership + ranking +
    # score), proving order-independence.
    reversed_run = group_themes(list(reversed(entities)), list(reversed(edges)))
    assert _summary(reversed_run) == baseline
    # Full structural equality, not just the digest.
    assert reversed_run == group_themes(entities, edges)


def test_below_threshold_cross_edge_alone_would_merge_is_actually_dropped() -> None:
    """If the 0.30 bridge is also removed, only the 0.19 edge links the clusters;
    it is below threshold, so the clusters still split into two groups."""
    edges = [e for e in _fixture_edges() if (e.src_id, e.dst_id) != ("b", "e")]
    groups = group_themes(_fixture_entities(), edges)
    member_sets = {frozenset(_ids(g)) for g in groups}
    assert frozenset({"a", "b", "c", "g"}) in member_sets
    assert frozenset({"d", "e", "f"}) in member_sets


# --------------------------------------------------------------------------- #
# Bridge-guard branches
# --------------------------------------------------------------------------- #
def test_strong_bridge_at_keep_weight_is_kept_and_merges_clusters() -> None:
    """A cross bridge at exactly 0.50 is kept, so the clusters merge."""
    edges = [
        _edge("a", "b", 0.70),
        _edge("b", "c", 0.75),
        _edge("a", "c", 0.80),
        _edge("d", "e", 0.70),
        _edge("e", "f", 0.75),
        _edge("d", "f", 0.80),
        _edge("c", "d", 0.50),  # bridge at the keep weight → kept
    ]
    entities = [_entity(c) for c in ("a", "b", "c", "d", "e", "f")]
    groups = group_themes(entities, edges)
    assert len(groups) == 1
    assert frozenset(_ids(groups[0])) == frozenset("abcdef")


def test_low_bridge_with_two_eligible_sides_is_dropped() -> None:
    """A cross bridge just below the keep weight, both sides >= 2, is dropped."""
    edges = [
        _edge("a", "b", 0.70),
        _edge("b", "c", 0.75),
        _edge("a", "c", 0.80),
        _edge("d", "e", 0.70),
        _edge("e", "f", 0.75),
        _edge("d", "f", 0.80),
        _edge("c", "d", 0.49),  # < 0.50, both sides size 3 → dropped
    ]
    entities = [_entity(c) for c in ("a", "b", "c", "d", "e", "f")]
    groups = group_themes(entities, edges)
    assert len(groups) == 2


def test_leaf_bridge_below_keep_weight_is_kept() -> None:
    """A bridge whose removal isolates a single leaf is kept regardless of weight."""
    edges = [
        _edge("a", "b", 0.70),
        _edge("b", "c", 0.75),
        _edge("a", "c", 0.80),
        _edge("a", "g", 0.21),  # leaf bridge, low weight → kept
    ]
    entities = [_entity(c) for c in ("a", "b", "c", "g")]
    groups = group_themes(entities, edges)
    assert len(groups) == 1
    assert frozenset(_ids(groups[0])) == frozenset("abcg")


def test_two_node_low_edge_is_a_leaf_bridge_and_kept() -> None:
    """An isolated 2-node edge below the keep weight: both sides are size 1, so
    it is a leaf bridge (not 'both sides >= 2') and is kept → one group."""
    entities = [_entity("a"), _entity("b")]
    groups = group_themes(entities, [_edge("a", "b", 0.30)])
    assert len(groups) == 1
    assert frozenset(_ids(groups[0])) == frozenset("ab")


def test_above_keep_weight_bridge_uses_keep_branch() -> None:
    """A bridge strictly above the keep weight takes the 'always kept' branch."""
    entities = [_entity(c) for c in ("a", "b", "c", "d", "e", "f")]
    edges = [
        _edge("a", "b", 0.70),
        _edge("b", "c", 0.75),
        _edge("a", "c", 0.80),
        _edge("d", "e", 0.70),
        _edge("e", "f", 0.75),
        _edge("d", "f", 0.80),
        _edge("c", "d", 0.90),  # > 0.50 → kept
    ]
    assert len(group_themes(entities, edges)) == 1


# --------------------------------------------------------------------------- #
# Degenerate inputs
# --------------------------------------------------------------------------- #
def test_empty_graph_returns_no_groups() -> None:
    assert group_themes([], []) == []


def test_single_node_is_a_singleton_group() -> None:
    groups = group_themes([_entity("a")], [])
    assert len(groups) == 1
    assert _ids(groups[0]) == ["a"]
    assert groups[0].group_id == 0
    assert groups[0].score == 0.0


def test_all_edges_below_threshold_yield_all_singletons() -> None:
    entities = [_entity(c) for c in ("a", "b", "c")]
    edges = [_edge("a", "b", 0.10), _edge("b", "c", 0.05)]
    groups = group_themes(entities, edges)
    assert len(groups) == 3
    assert all(len(g.entities) == 1 for g in groups)
    assert all(g.score == 0.0 for g in groups)
    # Singletons rank by lexicographic member key (all size 1, score 0).
    assert [_ids(g) for g in groups] == [["a"], ["b"], ["c"]]


def test_isolated_node_ranks_after_a_real_cluster() -> None:
    entities = [_entity(c) for c in ("a", "b", "z")]
    groups = group_themes(entities, [_edge("a", "b", 0.60)])
    assert [frozenset(_ids(g)) for g in groups] == [frozenset("ab"), frozenset("z")]
    assert groups[0].group_id == 0
    assert groups[1].group_id == 1


# --------------------------------------------------------------------------- #
# Input hygiene (dedup / self-loop / dangling endpoint / non-mutation)
# --------------------------------------------------------------------------- #
def test_duplicate_entity_ids_are_collapsed() -> None:
    groups = group_themes([_entity("a"), _entity("a", key="other")], [])
    assert len(groups) == 1
    # First occurrence wins.
    assert groups[0].entities[0].canonical_key == "topic-a"


def test_self_loop_edges_are_ignored() -> None:
    groups = group_themes([_entity("a")], [Edge(src_id="a", dst_id="a", weight=0.9)])
    assert len(groups) == 1
    assert groups[0].score == 0.0


def test_edge_to_unknown_entity_is_ignored() -> None:
    groups = group_themes([_entity("a")], [_edge("a", "ghost", 0.9)])
    assert len(groups) == 1
    assert _ids(groups[0]) == ["a"]
    assert groups[0].score == 0.0


def test_duplicate_pair_keeps_maximum_weight() -> None:
    """A duplicate endpoint pair keeps its max weight; the low copy alone would
    fall below threshold, but the high copy survives → the edge is kept."""
    entities = [_entity("a"), _entity("b")]
    edges = [_edge("a", "b", 0.10), _edge("a", "b", 0.80)]
    groups = group_themes(entities, edges)
    assert len(groups) == 1
    assert groups[0].score == pytest.approx(0.80)


def test_inputs_are_not_mutated() -> None:
    entities = _fixture_entities()
    edges = _fixture_edges()
    entities_snapshot = list(entities)
    edges_snapshot = list(edges)
    group_themes(entities, edges)
    assert entities == entities_snapshot
    assert edges == edges_snapshot


# --------------------------------------------------------------------------- #
# theme_limit truncation
# --------------------------------------------------------------------------- #
def test_theme_limit_truncates_to_top_ranked_groups() -> None:
    # Three disconnected clusters of decreasing size: {a,b,c,d}, {e,f,g}, {h,i}.
    entities = [_entity(c) for c in "abcdefghi"]
    edges = [
        _edge("a", "b", 0.60),
        _edge("b", "c", 0.60),
        _edge("c", "d", 0.60),
        _edge("a", "c", 0.60),
        _edge("e", "f", 0.60),
        _edge("f", "g", 0.60),
        _edge("e", "g", 0.60),
        _edge("h", "i", 0.60),
    ]
    full = group_themes(entities, edges)
    assert [len(g.entities) for g in full] == [4, 3, 2]

    limited = group_themes(entities, edges, theme_limit=2)
    assert len(limited) == 2
    assert [_ids(g) for g in limited] == [_ids(full[0]), _ids(full[1])]


def test_theme_limit_of_one_is_deterministic() -> None:
    groups = group_themes(_fixture_entities(), _fixture_edges(), theme_limit=1)
    assert len(groups) == 1
    assert frozenset(_ids(groups[0])) == frozenset({"a", "b", "c", "g"})


def test_theme_limit_larger_than_group_count_returns_all() -> None:
    groups = group_themes([_entity("a")], [], theme_limit=10)
    assert len(groups) == 1


# --------------------------------------------------------------------------- #
# Ranking tie-breaks (fully deterministic)
# --------------------------------------------------------------------------- #
def test_equal_size_and_score_groups_break_ties_lexicographically() -> None:
    # Two identical 2-node clusters; the {a,b} cluster sorts before {c,d}.
    entities = [_entity(c) for c in ("a", "b", "c", "d")]
    edges = [_edge("a", "b", 0.60), _edge("c", "d", 0.60)]
    groups = group_themes(entities, edges)
    assert [_ids(g) for g in groups] == [["a", "b"], ["c", "d"]]
    assert [g.group_id for g in groups] == [0, 1]
    # Reordering the input does not change the ranking.
    shuffled = group_themes(list(reversed(entities)), list(reversed(edges)))
    assert _summary(shuffled) == _summary(groups)


def test_higher_cohesion_outranks_lower_at_equal_size() -> None:
    entities = [_entity(c) for c in ("a", "b", "c", "d")]
    edges = [_edge("a", "b", 0.30), _edge("c", "d", 0.90)]
    groups = group_themes(entities, edges)
    # Same size (2); the stronger {c,d} cluster ranks first.
    assert _ids(groups[0]) == ["c", "d"]
    assert groups[0].score == pytest.approx(0.90)


# --------------------------------------------------------------------------- #
# Parameter validation
# --------------------------------------------------------------------------- #
def test_custom_min_edge_weight_changes_threshold() -> None:
    entities = [_entity("a"), _entity("b")]
    edges = [_edge("a", "b", 0.40)]
    # Default floor 0.20 keeps the edge; a 0.50 floor drops it.
    assert len(group_themes(entities, edges)) == 1
    assert len(group_themes(entities, edges, min_edge_weight=0.50)) == 2


@pytest.mark.parametrize("bad", [-0.01, 1.01])
def test_min_edge_weight_out_of_range_raises(bad: float) -> None:
    with pytest.raises(GroupingError, match="min_edge_weight"):
        group_themes([], [], min_edge_weight=bad)


@pytest.mark.parametrize("bad", [-0.01, 1.01])
def test_bridge_keep_weight_out_of_range_raises(bad: float) -> None:
    with pytest.raises(GroupingError, match="bridge_keep_weight"):
        group_themes([], [], bridge_keep_weight=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_theme_limit_below_one_raises(bad: int) -> None:
    with pytest.raises(GroupingError, match="theme_limit"):
        group_themes([], [], theme_limit=bad)


def test_module_defaults_match_spec() -> None:
    assert DEFAULT_MIN_EDGE_WEIGHT == 0.20
    assert DEFAULT_BRIDGE_KEEP_WEIGHT == 0.50
