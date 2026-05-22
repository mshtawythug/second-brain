"""Scoped-subgraph theme grouping — pure-Python CC + bridge guard (wave G2-e).

Pure logic, no DB, no I/O. Implements spec §6b step 3 / §17b Q1 (Codex-ruled):
group the small *scoped* subgraph of theme-eligible entities into
:class:`~brain.graph_rag.schema.ThemeGroup`s using

1. **Edge threshold** — keep a scoped edge iff its ``normalized_lift`` weight
   ``>= min_edge_weight`` (cfg ``graph_min_edge_weight``, default ``0.20``).
2. **Bridge guard** — over the thresholded graph, drop a graph-theoretic *bridge*
   edge IFF removing it leaves two sides EACH with ``>= 2`` theme-eligible
   entities AND the bridge ``weight < bridge_keep_weight`` (``0.50``). KEEP
   bridges with ``weight >= bridge_keep_weight``; KEEP *leaf* bridges (either
   side of size ``1``). Non-bridge edges are never dropped here.
3. **Connected components** — over the post-guard graph; each component (incl.
   isolated theme-eligible entities as singleton groups) becomes one
   :class:`ThemeGroup`.
4. **Deterministic ranking** — groups ordered by size DESC, then internal
   cohesion (sum of surviving in-group edge weights) DESC, then the
   lexicographic member key. ``theme_limit`` truncates the ranked list.

**networkx is intentionally NOT used.** The spec pulls it into G2 only if this
pure implementation of the same threshold/bridge/CC rules fails the acceptance
fixture (``tests/test_graphrag_grouping.py``) — it does not. Bridge detection is
an iterative Tarjan low-link sweep; components are an iterative BFS.

Inputs are the existing schema value objects: ``entities`` are the scoped,
already-theme-eligible :class:`~brain.graph_rag.schema.GraphEntity` rows (the
*caller* — G2-f — excludes the seed X / owner / generic entities, spec §17.5),
and ``edges`` are the in-scope :class:`~brain.graph_rag.schema.Edge` rows whose
``weight`` is the in-scope normalized lift. The function reads only ``id`` /
``canonical_key`` / ``entity_type`` from entities and ``src_id`` / ``dst_id`` /
``weight`` from edges; it never mutates the caller's sequences.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Sequence

from ..errors import GroupingError
from .schema import Edge, GraphEntity, ThemeGroup

__all__ = [
    "DEFAULT_BRIDGE_KEEP_WEIGHT",
    "DEFAULT_MIN_EDGE_WEIGHT",
    "group_themes",
]

# Normalized-lift floor for keeping a scoped edge. Mirrors
# ``config.DEFAULT_GRAPH_MIN_EDGE_WEIGHT`` / ``cfg.graph_min_edge_weight``
# (spec §17b Q1/Q5). Defined locally (like ``cooccur.DEFAULT_COOCCUR_WINDOW``
# and ``weighting.DEFAULT_GENERIC_DF``) so the pure-logic layer carries no
# runtime dependency on :mod:`brain.config`; production passes the cfg value.
DEFAULT_MIN_EDGE_WEIGHT = 0.20

# Bridge weight at/above which a bridge is always kept (spec §17b Q1). A bridge
# below this is dropped only when both sides have ``>= 2`` theme-eligible
# entities (a leaf bridge — one side of size 1 — is always kept).
DEFAULT_BRIDGE_KEEP_WEIGHT = 0.50


def group_themes(
    entities: Sequence[GraphEntity],
    edges: Sequence[Edge],
    *,
    min_edge_weight: float = DEFAULT_MIN_EDGE_WEIGHT,
    bridge_keep_weight: float = DEFAULT_BRIDGE_KEEP_WEIGHT,
    theme_limit: int | None = None,
) -> list[ThemeGroup]:
    """Group a scoped subgraph into ranked :class:`ThemeGroup`s (spec §6b/§17b Q1).

    Args:
        entities: The scoped, already theme-eligible entities. Each defines a
            node; every entity lands in exactly one group (an entity with no
            surviving edge forms a singleton group). Duplicate ids are collapsed
            (first occurrence wins). The sequence is not mutated.
        edges: In-scope undirected weighted edges (``weight`` = normalized lift
            in ``(0, 1]``). Edges referencing an entity absent from ``entities``,
            and self-loops, are ignored. A duplicate endpoint pair keeps its
            maximum weight (deterministic). The sequence is not mutated.
        min_edge_weight: Keep an edge iff ``weight >= min_edge_weight``
            (default :data:`DEFAULT_MIN_EDGE_WEIGHT`). Must be in ``[0.0, 1.0]``.
        bridge_keep_weight: Bridges with ``weight >= bridge_keep_weight`` are
            always kept (default :data:`DEFAULT_BRIDGE_KEEP_WEIGHT`). Must be in
            ``[0.0, 1.0]``.
        theme_limit: When set, return only the top ``theme_limit`` ranked groups
            (cfg ``graph_theme_limit``, default 5 in production). ``None`` keeps
            all groups. Must be ``>= 1`` when set.

    Returns:
        Ranked ``ThemeGroup``s: ``group_id`` is the 0-based rank, ``entities`` is
        the group's members (sorted), ``score`` is the group's internal cohesion
        (sum of surviving in-group edge weights), ``doc_ids`` / ``summary`` are
        left at their defaults (populated by the G2-f caller). Membership and
        ordering are deterministic — invariant under input reordering.

    Raises:
        GroupingError: ``min_edge_weight`` / ``bridge_keep_weight`` outside
            ``[0.0, 1.0]``, or ``theme_limit`` set ``< 1``.
    """
    _validate_params(min_edge_weight, bridge_keep_weight, theme_limit)

    # First-occurrence-wins dedup so a stray duplicate entity row is deterministic.
    entity_by_id: dict[str, GraphEntity] = {}
    for entity in entities:
        entity_by_id.setdefault(entity.id, entity)

    weights = _thresholded_edge_weights(edges, entity_by_id.keys(), min_edge_weight)
    adjacency = _build_adjacency(entity_by_id.keys(), weights)

    bridges = _find_bridges(entity_by_id.keys(), adjacency)
    dropped = _bridges_to_drop(bridges, weights, adjacency, bridge_keep_weight)
    surviving = {pair: w for pair, w in weights.items() if pair not in dropped}
    final_adjacency = _build_adjacency(entity_by_id.keys(), surviving)

    components = _connected_components(entity_by_id.keys(), final_adjacency)
    groups = _build_groups(components, entity_by_id, surviving)
    if theme_limit is not None:
        return groups[:theme_limit]
    return groups


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate_params(
    min_edge_weight: float, bridge_keep_weight: float, theme_limit: int | None
) -> None:
    """Fail fast on degenerate grouping knobs (caller bug, no DB touched)."""
    if not 0.0 <= min_edge_weight <= 1.0:
        raise GroupingError(
            f"min_edge_weight must be in [0.0, 1.0] (got {min_edge_weight})"
        )
    if not 0.0 <= bridge_keep_weight <= 1.0:
        raise GroupingError(
            f"bridge_keep_weight must be in [0.0, 1.0] (got {bridge_keep_weight})"
        )
    if theme_limit is not None and theme_limit < 1:
        raise GroupingError(
            f"theme_limit must be a positive integer or None (got {theme_limit})"
        )


# --------------------------------------------------------------------------- #
# Graph construction (canonical, deduped, threshold-filtered)
# --------------------------------------------------------------------------- #
def _canonical(a: str, b: str) -> tuple[str, str]:
    """Order an endpoint pair ``(lo, hi)`` so each undirected edge has one key."""
    return (a, b) if a < b else (b, a)


def _thresholded_edge_weights(
    edges: Sequence[Edge], node_ids: Iterable[str], min_edge_weight: float
) -> dict[tuple[str, str], float]:
    """Canonical-pair → weight for edges that survive the threshold.

    Drops self-loops, edges with an endpoint outside the node set, and edges
    below ``min_edge_weight``. A duplicate pair keeps its maximum weight so the
    result is independent of input order.
    """
    nodes = set(node_ids)
    weights: dict[tuple[str, str], float] = {}
    for edge in edges:
        if edge.src_id == edge.dst_id:
            continue  # no self loops
        if edge.src_id not in nodes or edge.dst_id not in nodes:
            continue  # endpoint not in the scoped node set
        if edge.weight < min_edge_weight:
            continue
        pair = _canonical(edge.src_id, edge.dst_id)
        prior = weights.get(pair)
        if prior is None or edge.weight > prior:
            weights[pair] = edge.weight
    return weights


def _build_adjacency(
    node_ids: Iterable[str], weights: dict[tuple[str, str], float]
) -> dict[str, list[str]]:
    """Undirected adjacency over all nodes; lists sorted for stable traversal."""
    adjacency: dict[str, set[str]] = {node: set() for node in node_ids}
    for src, dst in weights:
        adjacency[src].add(dst)
        adjacency[dst].add(src)
    return {node: sorted(neighbours) for node, neighbours in adjacency.items()}


# --------------------------------------------------------------------------- #
# Bridge detection (iterative Tarjan low-link; deduped ⇒ simple parent skip)
# --------------------------------------------------------------------------- #
def _find_bridges(
    node_ids: Iterable[str], adjacency: dict[str, list[str]]
) -> set[tuple[str, str]]:
    """Return the canonical pairs that are graph-theoretic bridges.

    A *bridge* is an edge whose removal increases the connected-component count.
    Iterative DFS computes discovery times ``disc`` and low-link values ``low``;
    a tree edge ``(parent, child)`` is a bridge iff ``low[child] > disc[parent]``.
    Edges are deduped to a single edge per pair upstream, so skipping one
    parent-edge occurrence is correct (no parallel-edge confusion). The bridge
    set is a graph invariant — independent of the DFS start/visit order.
    """
    disc: dict[str, int] = {}
    low: dict[str, int] = {}
    bridges: set[tuple[str, str]] = set()
    timer = 0

    for start in sorted(node_ids):
        if start in disc:
            continue
        timer = _dfs_bridges(start, adjacency, disc, low, bridges, timer)
    return bridges


def _dfs_bridges(
    start: str,
    adjacency: dict[str, list[str]],
    disc: dict[str, int],
    low: dict[str, int],
    bridges: set[tuple[str, str]],
    timer: int,
) -> int:
    """Iterative DFS from ``start`` populating ``disc``/``low``/``bridges``.

    Returns the advanced ``timer``. The explicit stack holds
    ``(node, parent, neighbour-iterator)`` frames so recursion depth is bounded
    by the heap, not the Python call stack (safe for long path-graphs).
    """
    disc[start] = low[start] = timer
    timer += 1
    stack: list[tuple[str, str | None, Iterator[str]]] = [
        (start, None, iter(adjacency[start]))
    ]
    while stack:
        node, parent, neighbours = stack[-1]
        descended = False
        for nbr in neighbours:
            if nbr == parent:
                continue  # the single edge back to the DFS parent
            if nbr not in disc:
                disc[nbr] = low[nbr] = timer
                timer += 1
                stack.append((nbr, node, iter(adjacency[nbr])))
                descended = True
                break
            low[node] = min(low[node], disc[nbr])
        if descended:
            continue
        stack.pop()
        if stack:
            par = stack[-1][0]
            low[par] = min(low[par], low[node])
            if low[node] > disc[par]:
                bridges.add(_canonical(par, node))
    return timer


def _bridges_to_drop(
    bridges: set[tuple[str, str]],
    weights: dict[tuple[str, str], float],
    adjacency: dict[str, list[str]],
    bridge_keep_weight: float,
) -> set[tuple[str, str]]:
    """Apply the §17b Q1 bridge guard; return the bridge pairs to drop.

    Each bridge is evaluated against the *thresholded* graph (only that one
    bridge removed): dropped iff its weight ``< bridge_keep_weight`` AND both
    resulting sides hold ``>= 2`` theme-eligible entities. Bridges at/above the
    keep weight, and leaf bridges (a side of size 1), are kept. All qualifying
    bridges are dropped together.
    """
    dropped: set[tuple[str, str]] = set()
    for pair in bridges:
        if weights[pair] >= bridge_keep_weight:
            continue  # strong bridge — always kept
        src, dst = pair
        src_side = _reachable_excluding_edge(adjacency, src, pair)
        dst_side = _reachable_excluding_edge(adjacency, dst, pair)
        if len(src_side) >= 2 and len(dst_side) >= 2:
            dropped.add(pair)
        # else: at least one leaf side — keep the bridge.
    return dropped


def _reachable_excluding_edge(
    adjacency: dict[str, list[str]], start: str, excluded: tuple[str, str]
) -> set[str]:
    """Nodes reachable from ``start`` without traversing the ``excluded`` edge.

    Used to size the two sides a bridge separates. For a true bridge the start
    side and the other endpoint's side are disjoint and partition the component.
    """
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nbr in adjacency[node]:
            if _canonical(node, nbr) == excluded:
                continue  # the bridge under evaluation is removed
            if nbr not in seen:
                seen.add(nbr)
                queue.append(nbr)
    return seen


# --------------------------------------------------------------------------- #
# Connected components + group assembly (deterministic ranking)
# --------------------------------------------------------------------------- #
def _connected_components(
    node_ids: Iterable[str], adjacency: dict[str, list[str]]
) -> list[list[str]]:
    """Connected components over ``adjacency``; isolated nodes are singletons.

    Iterative BFS seeded in sorted node order, so component discovery order is
    deterministic (final group ordering is re-sorted by rank regardless).
    """
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in sorted(node_ids):
        if start in seen:
            continue
        component: list[str] = []
        queue = deque([start])
        seen.add(start)
        while queue:
            node = queue.popleft()
            component.append(node)
            for nbr in adjacency[node]:
                if nbr not in seen:
                    seen.add(nbr)
                    queue.append(nbr)
        components.append(component)
    return components


def _build_groups(
    components: list[list[str]],
    entity_by_id: dict[str, GraphEntity],
    surviving: dict[tuple[str, str], float],
) -> list[ThemeGroup]:
    """Build ranked ``ThemeGroup``s from components + surviving-edge cohesion.

    Each group's ``score`` is the sum of surviving edge weights internal to it
    (singletons score ``0.0``). Groups rank by size DESC, score DESC, then the
    lexicographic member key ``(canonical_key, entity_type, id)`` ASC — a total
    order over disjoint membership, so ranking is fully deterministic.
    ``group_id`` is the resulting 0-based rank.
    """
    component_of = {
        node: idx for idx, component in enumerate(components) for node in component
    }
    # Accumulate in sorted-pair order so the float sum is byte-identical
    # regardless of the caller's edge ordering (determinism contract).
    scores = [0.0] * len(components)
    for (src, _dst), weight in sorted(surviving.items()):
        scores[component_of[src]] += weight

    ranked: list[tuple[float, list[GraphEntity]]] = []
    sort_keys: list[tuple[int, float, tuple[tuple[str, str, str], ...]]] = []
    for idx, component in enumerate(components):
        members = sorted(
            (entity_by_id[node] for node in component),
            key=lambda e: (e.canonical_key, e.entity_type, e.id),
        )
        member_key = tuple((e.canonical_key, e.entity_type, e.id) for e in members)
        sort_keys.append((-len(members), -scores[idx], member_key))
        ranked.append((scores[idx], members))

    order = sorted(range(len(ranked)), key=lambda i: sort_keys[i])
    return [
        ThemeGroup(group_id=rank, entities=ranked[i][1], score=ranked[i][0])
        for rank, i in enumerate(order)
    ]
