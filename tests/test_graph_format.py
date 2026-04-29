"""Byte-deterministic-output tests for :mod:`brain.vault.graph_format`.

The formatters' contract is "same input → same bytes, every time." We
pin exact strings against small fixtures so any change to layout,
sorting, escaping, or whitespace shows up as a diff.

No DB needed — the formatters are pure functions over
:class:`brain.vault.graph.GraphData`.
"""
import json

import pytest

from brain.vault.graph import GraphData, GraphEdge, GraphNode
from brain.vault.graph_format import to_dot, to_json, to_mermaid


def _three_node_two_edge() -> GraphData:
    """A → B → C: three vault notes, two wiki edges. The reference fixture."""
    nodes = [
        GraphNode(
            document_id="11111111-1111-1111-1111-111111111111",
            title="Alpha",
            kind="vault",
        ),
        GraphNode(
            document_id="22222222-2222-2222-2222-222222222222",
            title="Bravo",
            kind="vault",
        ),
        GraphNode(
            document_id="33333333-3333-3333-3333-333333333333",
            title="Charlie",
            kind="vault",
        ),
    ]
    edges = [
        GraphEdge(
            src_document_id=nodes[0].document_id,
            dst_document_id=nodes[1].document_id,
            link_kind="wiki",
            link_text="[[Bravo]]",
            display_text=None,
        ),
        GraphEdge(
            src_document_id=nodes[1].document_id,
            dst_document_id=nodes[2].document_id,
            link_kind="wiki",
            link_text="[[Charlie]]",
            display_text=None,
        ),
    ]
    return GraphData(nodes=nodes, edges=edges)


def test_to_json_round_trips_via_json_loads() -> None:
    """Output must parse as valid JSON and contain expected keys."""
    out = to_json(_three_node_two_edge())
    parsed = json.loads(out)
    assert set(parsed.keys()) == {"nodes", "edges"}
    assert len(parsed["nodes"]) == 3
    assert len(parsed["edges"]) == 2
    assert parsed["nodes"][0]["title"] == "Alpha"
    assert parsed["edges"][0]["text"] == "[[Bravo]]"


def test_to_json_is_byte_stable() -> None:
    """Repeated calls return identical bytes — pin exact output."""
    graph = _three_node_two_edge()
    a = to_json(graph)
    b = to_json(graph)
    assert a == b
    expected = (
        '{\n'
        '  "edges": [\n'
        '    {\n'
        '      "display": null,\n'
        '      "dst": "22222222-2222-2222-2222-222222222222",\n'
        '      "kind": "wiki",\n'
        '      "src": "11111111-1111-1111-1111-111111111111",\n'
        '      "text": "[[Bravo]]"\n'
        '    },\n'
        '    {\n'
        '      "display": null,\n'
        '      "dst": "33333333-3333-3333-3333-333333333333",\n'
        '      "kind": "wiki",\n'
        '      "src": "22222222-2222-2222-2222-222222222222",\n'
        '      "text": "[[Charlie]]"\n'
        '    }\n'
        '  ],\n'
        '  "nodes": [\n'
        '    {\n'
        '      "id": "11111111-1111-1111-1111-111111111111",\n'
        '      "kind": "vault",\n'
        '      "title": "Alpha"\n'
        '    },\n'
        '    {\n'
        '      "id": "22222222-2222-2222-2222-222222222222",\n'
        '      "kind": "vault",\n'
        '      "title": "Bravo"\n'
        '    },\n'
        '    {\n'
        '      "id": "33333333-3333-3333-3333-333333333333",\n'
        '      "kind": "vault",\n'
        '      "title": "Charlie"\n'
        '    }\n'
        '  ]\n'
        '}'
    )
    assert a == expected


def test_to_json_keeps_full_title_unlike_dot_mermaid() -> None:
    """JSON keeps the full title; only DOT/Mermaid truncate."""
    long = "x" * 200
    graph = GraphData(
        nodes=[
            GraphNode(
                document_id="aaaaaaaa-1111-1111-1111-111111111111",
                title=long,
                kind="vault",
            )
        ],
        edges=[],
    )
    out = to_json(graph)
    parsed = json.loads(out)
    assert parsed["nodes"][0]["title"] == long


def test_to_dot_byte_stable_for_fixture() -> None:
    out = to_dot(_three_node_two_edge())
    expected = (
        "digraph G {\n"
        "  rankdir=LR;\n"
        '  node [shape=box, style="rounded,filled"];\n'
        '  n_11111111 [label="Alpha", fillcolor="lightblue"];\n'
        '  n_22222222 [label="Bravo", fillcolor="lightblue"];\n'
        '  n_33333333 [label="Charlie", fillcolor="lightblue"];\n'
        '  n_11111111 -> n_22222222 [label="[[Bravo]]"];\n'
        '  n_22222222 -> n_33333333 [label="[[Charlie]]"];\n'
        "}\n"
    )
    assert out == expected


def test_to_dot_empty_graph_emits_valid_syntax() -> None:
    out = to_dot(GraphData(nodes=[], edges=[]))
    assert out == (
        "digraph G {\n"
        "  rankdir=LR;\n"
        '  node [shape=box, style="rounded,filled"];\n'
        "}\n"
    )


def test_to_mermaid_empty_graph_emits_valid_syntax() -> None:
    out = to_mermaid(GraphData(nodes=[], edges=[]))
    assert out == "graph TD\n"


def test_to_mermaid_byte_stable_for_fixture() -> None:
    out = to_mermaid(_three_node_two_edge())
    expected = (
        "graph TD\n"
        "  classDef vault fill:#cfe5f5,stroke:#3a7ab8,color:#000;\n"
        "  classDef ingested fill:#dddddd,stroke:#888,color:#000;\n"
        '  n_11111111["Alpha"]\n'
        "  class n_11111111 vault;\n"
        '  n_22222222["Bravo"]\n'
        "  class n_22222222 vault;\n"
        '  n_33333333["Charlie"]\n'
        "  class n_33333333 vault;\n"
        '  n_11111111 -->|"[[Bravo]]"| n_22222222\n'
        '  n_22222222 -->|"[[Charlie]]"| n_33333333\n'
    )
    assert out == expected


def test_to_dot_truncates_long_titles() -> None:
    long = "x" * 200
    graph = GraphData(
        nodes=[
            GraphNode(
                document_id="aaaaaaaa-1111-1111-1111-111111111111",
                title=long,
                kind="vault",
            )
        ],
        edges=[],
    )
    out = to_dot(graph)
    # 60-char truncation: 59 'x' followed by an ellipsis.
    assert ("x" * 59 + "…") in out
    # Full 200-char title should not appear.
    assert ("x" * 200) not in out


def test_to_mermaid_truncates_long_titles() -> None:
    long = "x" * 200
    graph = GraphData(
        nodes=[
            GraphNode(
                document_id="aaaaaaaa-1111-1111-1111-111111111111",
                title=long,
                kind="vault",
            )
        ],
        edges=[],
    )
    out = to_mermaid(graph)
    assert ("x" * 59 + "…") in out
    assert ("x" * 200) not in out


def test_to_dot_escapes_special_chars_in_titles() -> None:
    graph = GraphData(
        nodes=[
            GraphNode(
                document_id="aaaaaaaa-1111-1111-1111-111111111111",
                title='Has "quotes" and \\backslash and\nnewline',
                kind="vault",
            )
        ],
        edges=[],
    )
    out = to_dot(graph)
    # Backslash, then quote, then newline — all escaped.
    assert r'Has \"quotes\" and \\backslash and\nnewline' in out


def test_to_mermaid_escapes_special_chars_in_titles() -> None:
    graph = GraphData(
        nodes=[
            GraphNode(
                document_id="aaaaaaaa-1111-1111-1111-111111111111",
                title='Has "quotes" #hash and\nnewline',
                kind="vault",
            )
        ],
        edges=[],
    )
    out = to_mermaid(graph)
    # # → #35; FIRST so the entity escapes don't double-escape.
    # " → #quot;
    # \n → <br/>
    assert "Has #quot;quotes#quot; #35;hash and<br/>newline" in out


def test_to_dot_uses_dashed_style_for_embed_edges() -> None:
    nodes = [
        GraphNode(
            document_id="11111111-1111-1111-1111-111111111111",
            title="A",
            kind="vault",
        ),
        GraphNode(
            document_id="22222222-2222-2222-2222-222222222222",
            title="B",
            kind="vault",
        ),
    ]
    edges = [
        GraphEdge(
            src_document_id=nodes[0].document_id,
            dst_document_id=nodes[1].document_id,
            link_kind="embed",
            link_text="![[B]]",
            display_text=None,
        )
    ]
    out = to_dot(GraphData(nodes=nodes, edges=edges))
    assert 'style="dashed"' in out


def test_to_mermaid_empty_label_omits_pipes() -> None:
    """An edge whose effective label is empty still renders, just without the
    ``|"label"|`` segment — defensive branch for malformed input."""
    nodes = [
        GraphNode(
            document_id="11111111-1111-1111-1111-111111111111",
            title="A",
            kind="vault",
        ),
        GraphNode(
            document_id="22222222-2222-2222-2222-222222222222",
            title="B",
            kind="vault",
        ),
    ]
    # link_text and display_text both empty → label collapses to "".
    edges = [
        GraphEdge(
            src_document_id=nodes[0].document_id,
            dst_document_id=nodes[1].document_id,
            link_kind="wiki",
            link_text="",
            display_text=None,
        )
    ]
    out = to_mermaid(GraphData(nodes=nodes, edges=edges))
    assert "  n_11111111 --> n_22222222\n" in out
    assert '|""|' not in out


def test_to_mermaid_uses_dotted_arrow_for_embed_edges() -> None:
    nodes = [
        GraphNode(
            document_id="11111111-1111-1111-1111-111111111111",
            title="A",
            kind="vault",
        ),
        GraphNode(
            document_id="22222222-2222-2222-2222-222222222222",
            title="B",
            kind="vault",
        ),
    ]
    edges = [
        GraphEdge(
            src_document_id=nodes[0].document_id,
            dst_document_id=nodes[1].document_id,
            link_kind="embed",
            link_text="![[B]]",
            display_text=None,
        )
    ]
    out = to_mermaid(GraphData(nodes=nodes, edges=edges))
    assert "-.->" in out


def test_to_dot_ingested_uses_lightgray_fill() -> None:
    graph = GraphData(
        nodes=[
            GraphNode(
                document_id="11111111-1111-1111-1111-111111111111",
                title="Krisp call",
                kind="ingested",
            )
        ],
        edges=[],
    )
    out = to_dot(graph)
    assert 'fillcolor="lightgray"' in out


def test_to_mermaid_ingested_node_gets_ingested_class() -> None:
    graph = GraphData(
        nodes=[
            GraphNode(
                document_id="11111111-1111-1111-1111-111111111111",
                title="Krisp call",
                kind="ingested",
            )
        ],
        edges=[],
    )
    out = to_mermaid(graph)
    assert "class n_11111111 ingested;" in out


def test_to_dot_uses_display_text_when_set() -> None:
    nodes = [
        GraphNode(
            document_id="11111111-1111-1111-1111-111111111111",
            title="A",
            kind="vault",
        ),
        GraphNode(
            document_id="22222222-2222-2222-2222-222222222222",
            title="B",
            kind="vault",
        ),
    ]
    edges = [
        GraphEdge(
            src_document_id=nodes[0].document_id,
            dst_document_id=nodes[1].document_id,
            link_kind="wiki",
            link_text="[[B|person-a]]",
            display_text="person-a",
        )
    ]
    out = to_dot(GraphData(nodes=nodes, edges=edges))
    assert 'label="person-a"' in out
    assert 'label="[[B|person-a]]"' not in out


def test_to_mermaid_node_id_collision_resolved_by_hash_suffix() -> None:
    """Two docs sharing the first 8 hex chars get distinct safe ids."""
    nodes = [
        GraphNode(
            document_id="11111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            title="A",
            kind="vault",
        ),
        GraphNode(
            document_id="11111111-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            title="B",
            kind="vault",
        ),
    ]
    out = to_mermaid(GraphData(nodes=nodes, edges=[]))
    # First node gets the bare prefix, second gets a hash suffix appended.
    assert "  n_11111111[" in out
    assert "  n_11111111_" in out  # collision-disambiguator


def test_to_dot_ordering_is_independent_of_input_order() -> None:
    """Reverse the input list — the output must be byte-identical."""
    graph = _three_node_two_edge()
    reversed_graph = GraphData(
        nodes=list(reversed(graph.nodes)),
        edges=list(reversed(graph.edges)),
    )
    assert to_dot(graph) == to_dot(reversed_graph)


def test_to_json_ordering_is_independent_of_input_order() -> None:
    graph = _three_node_two_edge()
    reversed_graph = GraphData(
        nodes=list(reversed(graph.nodes)),
        edges=list(reversed(graph.edges)),
    )
    assert to_json(graph) == to_json(reversed_graph)


def test_to_mermaid_ordering_is_independent_of_input_order() -> None:
    graph = _three_node_two_edge()
    reversed_graph = GraphData(
        nodes=list(reversed(graph.nodes)),
        edges=list(reversed(graph.edges)),
    )
    assert to_mermaid(graph) == to_mermaid(reversed_graph)


@pytest.mark.parametrize("kind", ["vault", "ingested"])
def test_node_label_60_char_threshold(kind: str) -> None:
    """A 60-char title is preserved verbatim; 61 gets truncated to 59 + '…'."""
    sixty = "y" * 60
    sixty_one = "y" * 61
    g60 = GraphData(
        nodes=[
            GraphNode(
                document_id="11111111-1111-1111-1111-111111111111",
                title=sixty,
                kind=kind,
            )
        ],
        edges=[],
    )
    g61 = GraphData(
        nodes=[
            GraphNode(
                document_id="11111111-1111-1111-1111-111111111111",
                title=sixty_one,
                kind=kind,
            )
        ],
        edges=[],
    )
    assert sixty in to_dot(g60)
    assert ("y" * 59 + "…") in to_dot(g61)
    assert sixty_one not in to_dot(g61)
