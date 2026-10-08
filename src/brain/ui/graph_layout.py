"""Pure one-ring geometry for a note's neighbourhood graph — no I/O, no randomness.

The server owns the geometry and the client only draws it (phase-5 ruling R2):
a force simulation in the browser would make the same note land differently on
every open, and would need a physics library the static tree does not ship. A
single ring is enough for a depth-1 view and is trivially deterministic — the
same :class:`~brain.vault.graph.GraphData` always produces the same picture.

This module imports nothing that can reach a database, the filesystem or a
graph build (spec §6.5); ``tests/test_ui_routes_graph.py`` asserts the import
rule and ``tests/test_ui_graph_layout.py`` asserts the purity.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..vault.graph import GraphData, GraphNode

#: Default canvas edge, in px. Square; the client scales it with a viewBox.
DEFAULT_SIZE = 320

#: Default neighbour cap (ruling R4). Beyond this a ring of labels stops being
#: legible at :data:`DEFAULT_SIZE`; the remainder is reported as ``truncated``.
DEFAULT_CAP = 24

#: Root disc radius: larger than a neighbour so the centre reads as "you are here".
ROOT_RADIUS = 9

#: Neighbour disc radius: big enough to click, small enough that 24 fit the ring.
NEIGHBOUR_RADIUS = 6

#: Ring radius at the default 320px canvas. 24 nodes on a 110px ring are
#: 2π·110/24 ≈ 28.8px apart along the arc — more than two ~12px label lines, so
#: adjacent labels do not collide — and 160 − 110 − 6 = 44px remain between a
#: node's edge and the canvas edge for its label to sit outside the ring.
RING_RADIUS = 110.0


@dataclass(frozen=True)
class LayoutNode:
    """One drawn document: centre ``(x, y)`` and disc radius ``r`` in canvas px."""

    id: str
    title: str
    kind: str
    x: float
    y: float
    r: int
    root: bool


@dataclass(frozen=True)
class LayoutEdge:
    """One drawn edge. ``kind`` is the source ``link_kind``: ``wiki`` or ``derived``."""

    src: str
    dst: str
    kind: str


@dataclass(frozen=True)
class LayoutResult:
    """The whole picture. ``nodes[0]`` is always the root."""

    width: int
    height: int
    nodes: list[LayoutNode]
    edges: list[LayoutEdge]
    truncated: int


def _sort_key(node: GraphNode) -> tuple[str, str]:
    """Title case-insensitively, then id — the same order ``graph_data`` uses."""
    return (node.title.lower(), node.document_id)


def _ring_position(index: int, count: int, centre: float) -> tuple[float, float]:
    """Point ``index`` of ``count`` on the ring: 12 o'clock first, then clockwise.

    SVG's y axis grows downward, so clockwise on screen is ``+sin`` in x and
    ``-cos`` in y.
    """
    theta = 2 * math.pi * index / count
    return (
        centre + RING_RADIUS * math.sin(theta),
        centre - RING_RADIUS * math.cos(theta),
    )


def layout(
    graph: GraphData, *, root: str, cap: int = DEFAULT_CAP, size: int = DEFAULT_SIZE
) -> LayoutResult:
    """Place ``root`` at the centre and up to ``cap`` neighbours on one ring.

    Every node in ``graph`` other than the root is a neighbour. Neighbours are
    ordered by ``(title.lower(), id)``; the first ``cap`` are drawn and the rest
    are counted in ``truncated``. An edge is kept only when BOTH endpoints are
    drawn — an edge to a node that is not on screen would name an id the reader
    cannot see — and edges are deduplicated on ``(src, dst, kind)``, because a
    note with several ``[[refs]]`` to one target yields several ``links`` rows.

    :raises ValueError: if ``root`` is not among ``graph.nodes``. Inventing a
        root node here would need a title this function was never given.
    """
    by_id = {node.document_id: node for node in graph.nodes}
    root_node = by_id.get(root)
    if root_node is None:
        raise ValueError(f"root {root!r} is not a node of the graph")

    neighbours = sorted(
        (node for node in graph.nodes if node.document_id != root), key=_sort_key
    )
    drawn = neighbours[:cap]
    centre = size / 2

    nodes = [
        LayoutNode(
            id=root_node.document_id,
            title=root_node.title,
            kind=root_node.kind,
            x=centre,
            y=centre,
            r=ROOT_RADIUS,
            root=True,
        )
    ]
    for index, node in enumerate(drawn):
        x, y = _ring_position(index, len(drawn), centre)
        nodes.append(
            LayoutNode(
                id=node.document_id,
                title=node.title,
                kind=node.kind,
                x=x,
                y=y,
                r=NEIGHBOUR_RADIUS,
                root=False,
            )
        )

    on_screen = {node.id for node in nodes}
    edges: list[LayoutEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in graph.edges:
        key = (edge.src_document_id, edge.dst_document_id, edge.link_kind)
        if key in seen:
            continue
        if key[0] not in on_screen or key[1] not in on_screen:
            continue
        seen.add(key)
        edges.append(LayoutEdge(src=key[0], dst=key[1], kind=key[2]))

    return LayoutResult(
        width=size,
        height=size,
        nodes=nodes,
        edges=edges,
        truncated=len(neighbours) - len(drawn),
    )
