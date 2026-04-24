"""Tests for the `brain search` CLI command."""
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="COMPANY_REDACTED Notes",
            content="COMPANY_REDACTED was a great gig",
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )


def test_search_returns_results(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "company-id"])
    assert result.exit_code == 0, result.output
    assert "COMPANY_REDACTED Notes" in result.output


def test_search_json_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "company-id", "--json"])
    assert result.exit_code == 0, result.output
    # Rich's print_json may emit pretty-printed JSON across lines; relax:
    assert "COMPANY_REDACTED Notes" in result.stdout


def test_search_no_results_message(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    # Use --fts-only so the fake embedder's vector leg (which always matches the
    # single ingested doc as nearest neighbor) doesn't produce results.
    result = CliRunner().invoke(
        app, ["search", "nonexistent-unique-term-xyz", "--fts-only"]
    )
    assert result.exit_code == 0, result.output
    assert "(no results)" in result.output
