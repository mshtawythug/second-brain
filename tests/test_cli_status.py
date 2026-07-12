"""Tests for the `brain status` CLI command."""
import json
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def test_status_reports_counts(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="x",
            content="hello world",
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "documents" in result.output.lower()
    assert "1" in result.output


def test_status_on_empty_db(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """Status on an empty database prints 'never' for the last-ingest timestamp."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "never" in result.output


def test_status_json_shape(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """--json emits {documents, chunks, sources, by_source, last_ingest} (Task 5.1)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="x",
            content="hello world",
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    result = CliRunner().invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"documents", "chunks", "sources", "by_source", "last_ingest"}
    assert payload["documents"] == 1
    assert payload["chunks"] >= 1
    # A manual file ingest creates no `sources` row (only krisp/gmail/slack do),
    # but by_source coalesces the NULL source kind to "manual".
    assert isinstance(payload["sources"], int)
    assert payload["by_source"] == {"manual": 1}
    # last_ingest is an ISO-8601 string once at least one doc exists.
    assert isinstance(payload["last_ingest"], str)


def test_status_json_empty_db_last_ingest_null(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """On an empty DB --json reports last_ingest=null and empty by_source."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["documents"] == 0
    assert payload["last_ingest"] is None
    assert payload["by_source"] == {}
