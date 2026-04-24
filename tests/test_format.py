"""Unit tests for the output formatting helpers."""
import json

import pytest
from rich.console import Console
from rich.table import Table

from brain import format as fmt
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
