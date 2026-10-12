"""`brain.ui.graph_layout.layout` — the pure, deterministic one-ring geometry.

No database here: the function takes a :class:`~brain.vault.graph.GraphData`
and returns coordinates, so every case is a hand-built graph whose expected
answer can be written down.
"""
from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from brain.ui import graph_layout
from brain.ui.graph_layout import (
    DEFAULT_CAP,
    NEIGHBOUR_RADIUS,
    RING_RADIUS,
    ROOT_RADIUS,
    LayoutEdge,
    layout,
)
from brain.vault.graph import GraphData, GraphEdge, GraphNode

#: Pure geometry: nothing here opens a connection (see test_layout_module_does_no_io),
#: so the suite runs without the test database or its lock.
pytestmark = pytest.mark.nodb

ROOT = "aaaaaaaa-0000-4000-8000-00000000000a"


def _id(n: int) -> str:
    return f"bbbbbbbb-0000-4000-8000-{n:012d}"


def _node(doc_id: str, title: str, kind: str = "vault") -> GraphNode:
    return GraphNode(document_id=doc_id, title=title, kind=kind)


def _edge(src: str, dst: str, kind: str = "wiki", text: str = "[[x]]") -> GraphEdge:
    return GraphEdge(
        src_document_id=src,
        dst_document_id=dst,
        link_kind=kind,
        link_text=text,
        display_text=None,
    )


def _star(titles: list[str]) -> GraphData:
    """Root plus one neighbour per title, each linked from the root."""
    nodes = [_node(ROOT, "Root Note")]
    edges = []
    for i, title in enumerate(titles):
        nodes.append(_node(_id(i), title))
        edges.append(_edge(ROOT, _id(i)))
    return GraphData(nodes=nodes, edges=edges)


def test_root_is_first_and_at_the_centre() -> None:
    result = layout(_star(["Beta", "Alpha"]), root=ROOT, size=320)

    first = result.nodes[0]
    assert first.id == ROOT and first.root is True
    assert (first.x, first.y) == (160.0, 160.0)
    assert first.r == ROOT_RADIUS
    assert all(n.root is False for n in result.nodes[1:])
    assert all(n.r == NEIGHBOUR_RADIUS for n in result.nodes[1:])
    assert (result.width, result.height) == (320, 320)


def test_root_need_not_be_first_in_the_input() -> None:
    graph = _star(["Alpha"])
    reordered = GraphData(nodes=list(reversed(graph.nodes)), edges=graph.edges)
    assert layout(reordered, root=ROOT).nodes[0].id == ROOT


def test_same_input_twice_is_the_same_output() -> None:
    graph = _star(["Gamma", "alpha", "Beta", "delta"])
    assert layout(graph, root=ROOT) == layout(graph, root=ROOT)


def test_neighbours_sort_by_lowercased_title_then_id() -> None:
    nodes = [
        _node(ROOT, "Root Note"),
        _node(_id(3), "beta"),
        _node(_id(2), "Alpha"),
        _node(_id(9), "Same"),
        _node(_id(1), "same"),
        _node(_id(5), "Zed"),
    ]
    result = layout(GraphData(nodes=nodes, edges=[]), root=ROOT)

    assert [n.id for n in result.nodes[1:]] == [_id(2), _id(3), _id(1), _id(9), _id(5)]


def test_ring_starts_at_twelve_and_goes_clockwise_on_the_ring_radius() -> None:
    result = layout(_star(["A", "B", "C", "D"]), root=ROOT, size=320)
    centre = 160.0
    expected = [
        (centre, centre - RING_RADIUS),  # 12 o'clock
        (centre + RING_RADIUS, centre),  # 3 o'clock (SVG y grows downward)
        (centre, centre + RING_RADIUS),  # 6 o'clock
        (centre - RING_RADIUS, centre),  # 9 o'clock
    ]
    for node, (x, y) in zip(result.nodes[1:], expected, strict=True):
        assert node.x == pytest.approx(x, abs=1e-6)
        assert node.y == pytest.approx(y, abs=1e-6)
        assert math.hypot(node.x - centre, node.y - centre) == pytest.approx(
            RING_RADIUS, abs=1e-6
        )


def test_cap_keeps_the_first_by_sort_order_and_counts_the_rest() -> None:
    titles = [f"Note {i:02d}" for i in range(30)]
    result = layout(_star(titles), root=ROOT, cap=24)

    drawn = [n.title for n in result.nodes[1:]]
    assert drawn == titles[:24]
    assert result.truncated == 6
    assert len(result.edges) == 24


def test_under_the_cap_nothing_is_truncated() -> None:
    assert layout(_star(["A", "B"]), root=ROOT, cap=24).truncated == 0


def test_an_edge_is_kept_only_when_both_endpoints_are_drawn() -> None:
    titles = ["A", "B", "C"]
    graph = _star(titles)
    # C (index 2) is beyond cap=2; an A→C edge must not ship.
    graph = GraphData(nodes=graph.nodes, edges=[*graph.edges, _edge(_id(0), _id(2))])
    result = layout(graph, root=ROOT, cap=2)

    drawn = {n.id for n in result.nodes}
    assert _id(2) not in drawn
    for e in result.edges:
        assert e.src in drawn and e.dst in drawn
    assert result.edges == [
        LayoutEdge(src=ROOT, dst=_id(0), kind="wiki"),
        LayoutEdge(src=ROOT, dst=_id(1), kind="wiki"),
    ]


def test_edges_dedupe_on_src_dst_kind_but_keep_distinct_kinds() -> None:
    nodes = [_node(ROOT, "Root Note"), _node(_id(0), "A")]
    edges = [
        _edge(ROOT, _id(0), text="[[A]]"),
        _edge(ROOT, _id(0), text="[[A|alias]]"),
        _edge(ROOT, _id(0), kind="derived", text=""),
    ]
    result = layout(GraphData(nodes=nodes, edges=edges), root=ROOT)

    assert result.edges == [
        LayoutEdge(src=ROOT, dst=_id(0), kind="derived"),
        LayoutEdge(src=ROOT, dst=_id(0), kind="wiki"),
    ]


def test_edges_are_ordered_by_src_dst_kind_whatever_the_input_order() -> None:
    nodes = [_node(ROOT, "Root Note"), _node(_id(0), "A"), _node(_id(1), "B")]
    edges = [
        _edge(_id(1), ROOT, kind="wiki"),
        _edge(ROOT, _id(1), kind="wiki"),
        _edge(ROOT, _id(0), kind="wiki"),
        _edge(ROOT, _id(0), kind="embed", text="![[A]]"),
        _edge(ROOT, _id(0), kind="derived", text=""),
    ]
    result = layout(GraphData(nodes=nodes, edges=edges), root=ROOT)

    assert [(e.src, e.dst, e.kind) for e in result.edges] == [
        (ROOT, _id(0), "derived"),
        (ROOT, _id(0), "embed"),
        (ROOT, _id(0), "wiki"),
        (ROOT, _id(1), "wiki"),
        (_id(1), ROOT, "wiki"),
    ]


def test_node_kind_and_title_are_carried_through() -> None:
    nodes = [_node(ROOT, "Root Note"), _node(_id(0), "Thread", kind="ingested")]
    result = layout(GraphData(nodes=nodes, edges=[]), root=ROOT)
    assert (result.nodes[1].title, result.nodes[1].kind) == ("Thread", "ingested")


def test_a_root_absent_from_the_graph_is_an_error_not_an_invented_node() -> None:
    with pytest.raises(ValueError, match="root"):
        layout(GraphData(nodes=[_node(_id(0), "A")], edges=[]), root=ROOT)


def test_the_ring_fits_inside_the_canvas_and_keeps_discs_clear_at_the_cap() -> None:
    """The constants are only meaningful at the default canvas; pin that.

    This checks DISCS, not labels. Arc spacing says nothing about horizontal
    labels: at 5 neighbours three share the root label's row, each cut to
    about half a title, so past graph.js's ``MAX_LABELLED_NEIGHBOURS`` (4,
    reasoned in the comment above it) they show only on hover or focus. That 4
    is held as ``_SPEC_LABEL_THRESHOLD`` in ``tests/ui_graph_geometry.py`` and
    pinned in ``tests/test_ui_browser_graph_layout.py``, behaviourally by
    ``test_a_ring_is_crowded_from_one_past_the_specified_threshold``.
    """
    assert RING_RADIUS + NEIGHBOUR_RADIUS < 320 / 2
    # At the cap, adjacent neighbour centres are more than two disc diameters
    # apart along the arc (~28.8 > 24), so no two discs touch.
    assert 2 * math.pi * RING_RADIUS / DEFAULT_CAP > 2 * (2 * NEIGHBOUR_RADIUS)


def test_layout_module_does_no_io() -> None:
    """Pure by construction: no DB, filesystem or randomness imports."""
    tree = ast.parse(Path(graph_layout.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not imported & {"psycopg", "random", "os", "pathlib", "time"}, imported
