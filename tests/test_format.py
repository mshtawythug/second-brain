"""Unit tests for the output formatting helpers."""
import json
from datetime import UTC, datetime

import pytest
from rich.console import Console
from rich.table import Table

from brain import format as fmt
from brain.graph_rag.schema import (
    CommunityGroup,
    CommunityRecord,
    GraphContext,
    GraphEntity,
    GraphExplanation,
    ThemeGroup,
)
from brain.search import SearchResult


def test_emit_json_prints_payload(capsys: pytest.CaptureFixture[str]) -> None:
    fmt.emit_json({"hello": "world", "n": 3})
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed == {"hello": "world", "n": 3}


def test_emit_json_handles_non_serializable_via_default() -> None:
    # ``default=str`` should stringify anything json can't serialize natively.
    class Opaque:
        def __str__(self) -> str:
            return "opaque-value"

    fmt.emit_json({"thing": Opaque()})


def test_search_table_builds_rich_table_with_rows() -> None:
    results = [
        SearchResult(
            document_id="abcdef1234567890",
            title="Doc One",
            source_kind="manual",
            snippet="first line\nsecond line",
            score=0.1234,
            content_type="text/plain",
            tags=["work"],
        ),
        SearchResult(
            document_id="9999aaaa",
            title="Doc Two",
            source_kind=None,  # exercises the "manual" fallback
            snippet="x" * 500,  # exercises the 120-char snippet truncation
            score=0.9,
            content_type="text/markdown",
            tags=[],
        ),
    ]
    table = fmt.search_table(results)
    assert isinstance(table, Table)
    assert table.row_count == 2
    # Render the table to a buffer so we exercise the row formatting end-to-end.
    console = Console(record=True, width=200)
    console.print(table)
    rendered = console.export_text()
    assert "abcdef12" in rendered
    assert "Doc One" in rendered
    assert "Doc Two" in rendered
    assert "manual" in rendered
    assert "0.123" in rendered
    # Snippet should be truncated and newlines flattened.
    assert "first line second line" in rendered


def test_search_table_custom_title() -> None:
    """The ``title`` keyword renames the table (graph reuse → 'Documents')."""
    table = fmt.search_table([], title="Documents")
    assert table.title == "Documents"


# ---------------------------------------------------------------------------
# GraphContext rendering (wave G2-h)
# ---------------------------------------------------------------------------


def _entity(key: str, *, etype: str = "person", docs: int = 2) -> GraphEntity:
    return GraphEntity(
        id=f"id-{key}",
        entity_type=etype,
        name=key.title(),
        canonical_key=key,
        tenant_id="default",
        doc_count=docs,
    )


def _themes_context() -> GraphContext:
    return GraphContext(
        session_id="sess-1",
        mode="themes",
        query="",
        tenant_id="default",
        person="Dana Lee",
        themes=[
            ThemeGroup(
                group_id=0,
                entities=[_entity("bob"), _entity("carol")],
                doc_ids=["doc-1", "doc-2"],
                score=1.0,
                summary="A synthetic theme summary.",
            )
        ],
        entities=[_entity("bob"), _entity("carol")],
        docs=[
            SearchResult(
                document_id="doc-1aaaa",
                title="Doc One",
                source_kind="gmail",
                snippet="snippet text",
                score=2.0,
                content_type="email",
                tags=["t"],
            )
        ],
        explanation=GraphExplanation(
            mode="themes",
            tenant_id="default",
            seed_entity_ids=["id-dana"],
            person_keys=["dana lee"],
            generic_df_cap=3,
            matched_filters={"person": "Dana Lee"},
        ),
    )


def _local_context() -> GraphContext:
    return GraphContext(
        session_id="sess-2",
        mode="local",
        query="bob",
        tenant_id="default",
        entities=[_entity("bob"), _entity("alice")],
        docs=[],
        explanation=GraphExplanation(mode="local", depth=2, frontier_cap=200),
    )


def test_graph_context_json_themes_shape() -> None:
    """The themes JSON serializer carries every wire-shape field."""
    payload = fmt.graph_context_json(_themes_context())
    assert payload["mode"] == "themes"
    assert payload["person"] == "Dana Lee"
    assert payload["session_id"] == "sess-1"
    assert payload["requested_mode"] is None
    assert payload["degraded_from"] is None
    # Themes serialize entities + doc_ids + score + summary.
    theme = payload["themes"][0]
    assert theme["group_id"] == 0
    assert theme["score"] == 1.0
    assert theme["summary"] == "A synthetic theme summary."
    assert {e["canonical_key"] for e in theme["entities"]} == {"bob", "carol"}
    assert theme["doc_ids"] == ["doc-1", "doc-2"]
    # Docs reuse the search-hit shape.
    assert payload["docs"][0]["id"] == "doc-1aaaa"
    assert payload["docs"][0]["title"] == "Doc One"
    # Explanation diagnostics, no raw Cypher.
    assert payload["explanation"]["mode"] == "themes"
    assert payload["explanation"]["generic_df_cap"] == 3
    # Round-trips through json (no non-serializable values).
    assert json.loads(json.dumps(payload))["mode"] == "themes"


def test_graph_context_json_explanation_none() -> None:
    ctx = GraphContext(session_id="s", mode="local", query="q", explanation=None)
    assert fmt.graph_context_json(ctx)["explanation"] is None


def test_graph_context_renderable_themes() -> None:
    """Human render shows the header + themes table + documents table."""
    console = Console(record=True, width=200)
    console.print(fmt.graph_context_renderable(_themes_context()))
    rendered = console.export_text()
    assert "mode=themes" in rendered
    assert "person=Dana Lee" in rendered
    assert "tenant=default" in rendered
    assert "Themes" in rendered
    assert "Bob" in rendered and "Carol" in rendered
    assert "Documents" in rendered
    assert "Doc One" in rendered


def test_graph_context_renderable_local_entities() -> None:
    """Local mode (no themes) renders the entities neighbourhood table."""
    console = Console(record=True, width=200)
    console.print(fmt.graph_context_renderable(_local_context()))
    rendered = console.export_text()
    assert "mode=local" in rendered
    assert "Entities" in rendered
    assert "Bob" in rendered and "Alice" in rendered


def test_graph_context_renderable_degradation_note() -> None:
    """A degraded context renders the global→local note."""
    ctx = GraphContext(
        session_id="s",
        mode="local",
        query="recurring themes",
        tenant_id="default",
        requested_mode="auto",
        degraded_from="global",
        degradation_reason="global_unavailable_g2",
    )
    console = Console(record=True, width=200)
    console.print(fmt.graph_context_renderable(ctx))
    rendered = console.export_text()
    assert "degraded" in rendered
    assert "global" in rendered
    assert "global_unavailable_g2" in rendered


def test_graph_context_renderable_empty() -> None:
    """An all-empty context renders the header + a no-results line."""
    ctx = GraphContext(session_id="s", mode="local", query="nothing")
    console = Console(record=True, width=200)
    console.print(fmt.graph_context_renderable(ctx))
    rendered = console.export_text()
    assert "no graph results" in rendered


# ---------------------------------------------------------------------------
# CommunityGroup rendering (global mode) + community admin listing (wave G3-f)
# ---------------------------------------------------------------------------


def _global_context() -> GraphContext:
    return GraphContext(
        session_id="sess-g",
        mode="global",
        query="cluster",
        tenant_id="default",
        communities=[
            CommunityGroup(
                community_key="aaaaaaaa-1111-2222-3333-444444444444",
                level=0,
                member_count=3,
                score=0.0328,
                summary="Community covering Bob, Carol.",
                entities=[_entity("bob"), _entity("carol")],
                doc_ids=["doc-1", "doc-2"],
            ),
            CommunityGroup(
                community_key="bbbbbbbb-5555-6666-7777-888888888888",
                level=0,
                member_count=2,
                score=0.0161,
                summary=None,
                entities=[_entity("alice")],
                doc_ids=["doc-3"],
            ),
        ],
        entities=[_entity("bob"), _entity("carol"), _entity("alice")],
        docs=[
            SearchResult(
                document_id="doc-1aaaa",
                title="Cluster Doc",
                source_kind="note",
                snippet="snippet text",
                score=2.0,
                content_type="note",
                tags=[],
            )
        ],
        explanation=GraphExplanation(mode="global", tenant_id="default"),
    )


def test_graph_context_json_communities_shape() -> None:
    """Global mode serializes the top-level ``communities`` key (spec §17c Q5)."""
    payload = fmt.graph_context_json(_global_context())
    assert payload["mode"] == "global"
    assert "communities" in payload
    first = payload["communities"][0]
    assert first["community_key"] == "aaaaaaaa-1111-2222-3333-444444444444"
    assert first["level"] == 0
    assert first["member_count"] == 3
    assert first["score"] == 0.0328
    assert first["summary"] == "Community covering Bob, Carol."
    assert {e["canonical_key"] for e in first["entities"]} == {"bob", "carol"}
    assert first["doc_ids"] == ["doc-1", "doc-2"]
    # A NULL summary serializes as None (never the string "null").
    assert payload["communities"][1]["summary"] is None
    # Existing keys stay present + stable (additive wire-shape discipline).
    for key in ("themes", "entities", "docs", "explanation", "degraded_from"):
        assert key in payload
    # Round-trips through json.
    assert json.loads(json.dumps(payload))["communities"][0]["member_count"] == 3


def test_graph_context_json_non_global_has_empty_communities() -> None:
    """Themes/local contexts carry an empty (never absent) ``communities`` key."""
    assert fmt.graph_context_json(_themes_context())["communities"] == []
    assert fmt.graph_context_json(_local_context())["communities"] == []


def test_graph_context_renderable_communities() -> None:
    """Global mode renders the Communities table (not entities) + documents."""
    console = Console(record=True, width=200)
    console.print(fmt.graph_context_renderable(_global_context()))
    rendered = console.export_text()
    assert "mode=global" in rendered
    assert "Communities" in rendered
    assert "Bob" in rendered and "Carol" in rendered
    assert "Community covering" in rendered
    assert "Documents" in rendered
    # No raw Cypher ever surfaces.
    assert "cypher" not in rendered.lower()


def _community_record(
    *, key: str, members: int, summary: str | None
) -> CommunityRecord:
    return CommunityRecord(
        community_key=key,
        source_graph_hash="hash",
        members_hash="mh",
        tenant_id="default",
        level=0,
        member_count=members,
        edge_count=members,
        total_weight=1.5,
        summary=summary,
        summary_model="fake-model:1b" if summary else None,
        summary_at=datetime(2026, 5, 22, tzinfo=UTC) if summary else None,
    )


def test_community_record_json_shape() -> None:
    """The admin-listing JSON serializer carries the stored-row fields."""
    record = _community_record(
        key="cccccccc-9999-0000-1111-222222222222",
        members=4,
        summary="Stored community summary.",
    )
    payload = fmt.community_record_json(record)
    assert payload["community_key"] == "cccccccc-9999-0000-1111-222222222222"
    assert payload["level"] == 0
    assert payload["member_count"] == 4
    assert payload["edge_count"] == 4
    assert payload["total_weight"] == 1.5
    assert payload["summary"] == "Stored community summary."
    assert payload["summary_model"] == "fake-model:1b"
    assert payload["summary_at"] == "2026-05-22T00:00:00+00:00"
    # NULL summary → summary/model/at all None.
    null_payload = fmt.community_record_json(
        _community_record(key="dddd", members=1, summary=None)
    )
    assert null_payload["summary"] is None
    assert null_payload["summary_model"] is None
    assert null_payload["summary_at"] is None
    assert json.loads(json.dumps(payload))["member_count"] == 4


def test_community_records_table_renders_rows() -> None:
    """The admin table shows each community's short key + counts + preview."""
    records = [
        _community_record(key="abcdef12-0000", members=3, summary="First summary."),
        _community_record(key="ffffffff-9999", members=1, summary=None),
    ]
    console = Console(record=True, width=200)
    console.print(fmt.community_records_table(records))
    rendered = console.export_text()
    assert "Communities" in rendered
    assert "abcdef12" in rendered  # short key
    assert "First summary." in rendered
    assert "(none)" in rendered  # NULL-summary preview


def test_community_records_table_empty() -> None:
    """An empty community list still renders the header-only table."""
    console = Console(record=True, width=200)
    console.print(fmt.community_records_table([]))
    assert "Communities" in console.export_text()
