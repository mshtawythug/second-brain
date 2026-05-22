"""Tests for the `brain status` CLI command."""
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
