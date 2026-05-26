"""Byte-deterministic-output tests for :mod:`brain.vault.graph_format`.

The formatters' contract is "same input → same bytes, every time." We
pin exact strings against small fixtures so any change to layout,
sorting, escaping, or whitespace shows up as a diff.

No DB needed — the formatters are pure functions over
:class:`brain.vault.graph.GraphData`.
"""
import io
import json
from typing import Any

import pytest
from rich.console import Console
from rich.table import Table

from brain.format import (
    _entity_json,
    _graph_themes_table,
    alias_result_json,
    alias_result_summary,
)
from brain.graph_rag.aliases import AliasResult
from brain.graph_rag.schema import GraphEntity, ThemeGroup
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
    """Repeated calls return identical bytes — pin exact output.

    Every edge carries ``rule`` / ``weight`` / ``evidence``; for the
    wiki-only fixture they're ``null``, but the keys are always present
    so downstream consumers can iterate edges without conditional
    field-existence checks.
    """
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
        '      "evidence": null,\n'
        '      "kind": "wiki",\n'
        '      "rule": null,\n'
        '      "src": "11111111-1111-1111-1111-111111111111",\n'
        '      "text": "[[Bravo]]",\n'
        '      "weight": null\n'
        '    },\n'
        '    {\n'
        '      "display": null,\n'
        '      "dst": "33333333-3333-3333-3333-333333333333",\n'
        '      "evidence": null,\n'
        '      "kind": "wiki",\n'
        '      "rule": null,\n'
        '      "src": "22222222-2222-2222-2222-222222222222",\n'
        '      "text": "[[Charlie]]",\n'
        '      "weight": null\n'
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


# ---------------------------------------------------------------------------
# Derived-edge tier styling (Task C.2)
#
# Helpers + fixtures for building one-edge derived graphs. Each test
# pins a single behavior — arrow shape, attribute list, or `linkStyle`
# index — so a regression in one tier doesn't cascade across the suite.
# ---------------------------------------------------------------------------


def _two_node_derived(
    rule: str, weight: float, evidence: dict[str, object] | None = None
) -> GraphData:
    """A → B with a single derived edge of the given rule/weight."""
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
            link_kind="derived",
            link_text="",
            display_text=None,
            rule=rule,
            weight=weight,
            evidence=evidence if evidence is not None else {},
        )
    ]
    return GraphData(nodes=nodes, edges=edges)


def test_mermaid_wiki_edge_uses_solid_arrow() -> None:
    """Wiki edges keep the plain ``-->`` arrow — never bold or dotted."""
    out = to_mermaid(_three_node_two_edge())
    assert "-->" in out
    assert "==>" not in out
    # `-.->` would also be wrong here. The fixture has only wiki edges.
    assert "-.->" not in out


def test_mermaid_r1_edge_uses_bold_arrow() -> None:
    """R1 (``shared_thread``) renders with Mermaid's bold ``==>`` arrow."""
    out = to_mermaid(_two_node_derived("shared_thread", 1.0))
    assert "==>" in out
    # No linkStyle is needed for R1 — bold is built into the arrow shape.
    assert "linkStyle" not in out


def test_mermaid_r3_edge_uses_gray_linkstyle() -> None:
    """R3 (``same_day_participant``) is plain ``-->`` plus gray linkStyle."""
    out = to_mermaid(_two_node_derived("same_day_participant", 0.7))
    # Plain arrow — color comes from the linkStyle directive, not the shape.
    assert " --> " in out
    assert "==>" not in out
    # Single edge → linkStyle indexes 0.
    assert "linkStyle 0 stroke:gray" in out


def test_mermaid_r2_edge_uses_dotted_arrow() -> None:
    """R2 (``shared_participant``) renders with the dotted ``-.->`` arrow."""
    out = to_mermaid(_two_node_derived("shared_participant", 0.4))
    assert "-.->" in out
    # No linkStyle for R2 — dotted shape carries the styling.
    assert "linkStyle" not in out


def test_dot_wiki_edge_has_default_style() -> None:
    """Wiki edges in DOT carry only the ``label="..."`` attribute — no
    derived ``style=`` or ``color=`` overrides on the edge itself.

    Nodes still carry ``fillcolor=`` (a node attribute, unrelated to
    the per-edge color we're guarding against here), so we inspect each
    edge line individually instead of grepping the whole graph.
    """
    out = to_dot(_three_node_two_edge())
    edge_lines = [
        line for line in out.splitlines() if " -> " in line
    ]
    # Sanity: the fixture has two wiki edges → two `->` lines.
    assert len(edge_lines) == 2
    for line in edge_lines:
        assert "style=bold" not in line
        assert "style=dotted" not in line
        assert "style=solid" not in line
        assert 'style="dashed"' not in line
        assert "color=" not in line


def test_dot_r1_edge_has_bold_style() -> None:
    """R1 derived edges in DOT pick up ``style=bold, color=black``."""
    out = to_dot(_two_node_derived("shared_thread", 1.0))
    assert "style=bold" in out
    assert "color=black" in out


def test_dot_r3_edge_has_solid_gray_style() -> None:
    """R3 derived edges in DOT pick up ``style=solid, color=gray``."""
    out = to_dot(_two_node_derived("same_day_participant", 0.7))
    assert "style=solid, color=gray" in out


def test_dot_r2_edge_has_dotted_light_gray_style() -> None:
    """R2 derived edges in DOT pick up ``style=dotted, color="#cccccc"``.

    The hex color is quoted because Graphviz parses bare ``#`` as a
    comment delimiter; the spec's exact form with quotes is reproduced
    verbatim.
    """
    out = to_dot(_two_node_derived("shared_participant", 0.4))
    assert 'style=dotted, color="#cccccc"' in out


def test_json_output_includes_rule_weight_evidence_for_derived_edges() -> None:
    """JSON for a derived edge surfaces the metadata-rule provenance."""
    evidence = {"thread_id": "abc123"}
    graph = _two_node_derived("shared_thread", 1.0, evidence=evidence)
    parsed = json.loads(to_json(graph))
    edge = parsed["edges"][0]
    assert edge["rule"] == "shared_thread"
    assert edge["weight"] == 1.0
    assert edge["evidence"] == evidence
    assert edge["kind"] == "derived"


def test_json_output_excludes_or_nullifies_rule_for_wiki_edges() -> None:
    """Wiki edges in JSON carry ``null`` for ``rule`` / ``weight`` /
    ``evidence`` — the keys are always present (matching the project's
    ``display: null`` convention) so consumers can iterate without
    membership checks."""
    parsed = json.loads(to_json(_three_node_two_edge()))
    for edge in parsed["edges"]:
        assert edge["rule"] is None
        assert edge["weight"] is None
        assert edge["evidence"] is None


def test_mermaid_mixed_edges_correct_linkstyle_indices() -> None:
    """A graph with multiple derived rules emits one ``linkStyle`` per R3
    edge, each targeting the 0-indexed position of that edge in the
    rendered Mermaid output.

    Edges are sorted by ``(src, dst, link_text, link_kind)``. With the
    ids below the sort lands them in this order:

    0. A→B  wiki                       (-->)
    1. A→C  shared_thread              (==>)
    2. A→D  same_day_participant       (-->) ← linkStyle 2 stroke:gray
    3. A→E  shared_participant         (-.->)
    4. B→C  same_day_participant       (-->) ← linkStyle 4 stroke:gray

    So the formatter must emit exactly two ``linkStyle`` lines — one
    for index 2, one for index 4 — and nothing for the wiki / R1 / R2
    edges between them.
    """
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
        GraphNode(
            document_id="33333333-3333-3333-3333-333333333333",
            title="C",
            kind="vault",
        ),
        GraphNode(
            document_id="44444444-4444-4444-4444-444444444444",
            title="D",
            kind="vault",
        ),
        GraphNode(
            document_id="55555555-5555-5555-5555-555555555555",
            title="E",
            kind="vault",
        ),
    ]

    def _wiki(src_idx: int, dst_idx: int, text: str) -> GraphEdge:
        return GraphEdge(
            src_document_id=nodes[src_idx].document_id,
            dst_document_id=nodes[dst_idx].document_id,
            link_kind="wiki",
            link_text=text,
            display_text=None,
        )

    def _derived(src_idx: int, dst_idx: int, rule: str, weight: float) -> GraphEdge:
        return GraphEdge(
            src_document_id=nodes[src_idx].document_id,
            dst_document_id=nodes[dst_idx].document_id,
            link_kind="derived",
            link_text="",
            display_text=None,
            rule=rule,
            weight=weight,
            evidence={},
        )

    edges = [
        _wiki(0, 1, "[[B]]"),                              # A→B wiki
        _derived(0, 2, "shared_thread", 1.0),              # A→C R1
        _derived(0, 3, "same_day_participant", 0.7),       # A→D R3 → linkStyle 2
        _derived(0, 4, "shared_participant", 0.4),         # A→E R2
        _derived(1, 2, "same_day_participant", 0.7),       # B→C R3 → linkStyle 4
    ]
    out = to_mermaid(GraphData(nodes=nodes, edges=edges))

    # Exactly the two expected linkStyle lines, no others.
    assert "  linkStyle 2 stroke:gray;" in out
    assert "  linkStyle 4 stroke:gray;" in out
    assert out.count("linkStyle") == 2

    # Sanity: each non-R3 derived edge picked the right arrow shape.
    assert " ==> " in out  # R1
    assert " -.-> " in out  # R2

    # And the linkStyle lines come AFTER the edges (Mermaid renderers
    # are happiest that way; the formatter buffers them deliberately).
    body = out.splitlines()
    last_edge_idx = max(
        i for i, line in enumerate(body) if " --> " in line or " ==> " in line or " -.-> " in line
    )
    first_linkstyle_idx = min(
        i for i, line in enumerate(body) if "linkStyle " in line
    )
    assert first_linkstyle_idx > last_edge_idx


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


# ===========================================================================
# GraphRAG format tests (brain.format._entity_json / _graph_themes_table)
# A3: scoped_doc_count in JSON + themes human renderer
# ===========================================================================


def _render_table_to_text(table: Table) -> str:
    """Render a Rich :class:`~rich.table.Table` to plain text (no ANSI)."""
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, no_color=True, width=200)
    console.print(table)
    return buf.getvalue()


def _make_entity(**kw: Any) -> GraphEntity:
    base: dict[str, Any] = {"id": "x", "entity_type": "topic", "name": "N", "canonical_key": "n"}
    base.update(kw)
    return GraphEntity(**base)


def test_entity_json_includes_scoped_doc_count() -> None:
    """``_entity_json`` includes ``scoped_doc_count`` when set."""
    out = _entity_json(_make_entity(doc_count=16, scoped_doc_count=3))
    assert out["doc_count"] == 16 and out["scoped_doc_count"] == 3


def test_entity_json_scoped_null_when_unset() -> None:
    """``_entity_json`` emits ``scoped_doc_count: null`` when not set."""
    out = _entity_json(_make_entity(entity_type="org"))
    assert out["scoped_doc_count"] is None


def test_graph_themes_table_shows_scoped_count() -> None:
    """The themes table inlines ``(scoped)`` per entity when ``scoped_doc_count`` is set."""
    grp = ThemeGroup(
        group_id=0,
        entities=[_make_entity(doc_count=16, scoped_doc_count=3)],
        doc_ids=["d1"],
        score=1.0,
    )
    rendered = _render_table_to_text(_graph_themes_table([grp]))
    assert "N (3)" in rendered


def test_graph_themes_table_omits_parens_when_scoped_none() -> None:
    """When ``scoped_doc_count`` is None, entity name renders without parenthetical."""
    grp = ThemeGroup(
        group_id=0,
        entities=[_make_entity()],
        doc_ids=["d1"],
        score=1.0,
    )
    rendered = _render_table_to_text(_graph_themes_table([grp]))
    # Name alone, no trailing " (N)".
    assert "N" in rendered
    assert "N (" not in rendered


# ===========================================================================
# Wave C3 — alias_result_json + alias_result_summary renderers
# ===========================================================================


def test_alias_result_json_emits_all_seven_fields() -> None:
    """``alias_result_json`` returns ALL 7 :class:`AliasResult` fields verbatim."""
    res = AliasResult(
        tenant_id="t-test",
        rules_total=3,
        rules_applied=2,
        mentions_repointed=5,
        contributions_repointed=4,
        sources_orphaned=2,
        dry_run=False,
    )
    out = alias_result_json(res)
    assert out == {
        "tenant_id": "t-test",
        "rules_total": 3,
        "rules_applied": 2,
        "mentions_repointed": 5,
        "contributions_repointed": 4,
        "sources_orphaned": 2,
        "dry_run": False,
    }


def test_alias_result_json_dry_run_preserves_flag() -> None:
    """The dry-run flag rides through the JSON serializer unchanged."""
    res = AliasResult(tenant_id="t-test", rules_total=1, dry_run=True)
    out = alias_result_json(res)
    assert out["dry_run"] is True
    assert out["rules_total"] == 1


def test_alias_result_summary_real_apply() -> None:
    """The one-line human summary mentions all counters and the tenant id."""
    res = AliasResult(
        tenant_id="t-test",
        rules_total=2,
        rules_applied=2,
        mentions_repointed=3,
        contributions_repointed=4,
        sources_orphaned=2,
        dry_run=False,
    )
    line = alias_result_summary(res)
    assert line.startswith("graphrag aliases apply:")
    assert "2/2 rule(s) applied" in line
    assert "3 mention(s) repointed" in line
    assert "4 contribution(s) repointed" in line
    assert "2 source(s) orphaned" in line
    assert "tenant 't-test'" in line


def test_alias_result_summary_dry_run_prefix() -> None:
    """``dry_run=True`` prepends a ``(dry-run)`` prefix to the summary line."""
    res = AliasResult(tenant_id="t-test", rules_total=1, rules_applied=1, dry_run=True)
    line = alias_result_summary(res)
    assert line.startswith("(dry-run) graphrag aliases apply:")
