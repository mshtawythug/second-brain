"""Tests for the `brain analyze` CLI command.

`brain analyze` runs ANALYZE against the configured database to refresh
planner statistics — the remediation `brain doctor` suggests for the
post-pg_restore "chunks stats WARN — never analyzed" state. All fixtures are
synthetic; no production data.
"""
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


def _ingest_one(conn: psycopg.Connection, embedder: object) -> None:
    ingest_document(
        conn,
        embedder=embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="synthetic note",
            content="hello world from a synthetic fixture",
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )


def test_analyze_chunks_default(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """`brain analyze` with no args ANALYZEs chunks and reports success."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    _ingest_one(test_db, fake_embedder)

    result = CliRunner().invoke(app, ["analyze"])

    assert result.exit_code == 0, result.output
    assert "chunks" in result.output
    assert "done" in result.output.lower()


def test_analyze_explicit_table(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """An explicit existing table name is accepted and analyzed."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    result = CliRunner().invoke(app, ["analyze", "documents"])

    assert result.exit_code == 0, result.output
    assert "documents" in result.output


def test_analyze_unknown_table_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """An unknown table name fails as a BadParameter (exit code 2)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    result = CliRunner().invoke(app, ["analyze", "no_such_table"])

    assert result.exit_code == 2, result.output
    assert "unknown table" in result.output.lower()


def test_analyze_all_tables(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """`brain analyze --all` ANALYZEs the whole database."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    result = CliRunner().invoke(app, ["analyze", "--all"])

    assert result.exit_code == 0, result.output
    assert "all tables" in result.output.lower()


def test_analyze_clears_doctor_warn(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """After `brain analyze`, the chunks table reports a non-NULL last_analyze.

    This is the end-to-end contract behind the doctor remediation: running the
    command makes the stale-stats WARN condition (both analyze timestamps NULL)
    no longer hold.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    _ingest_one(test_db, fake_embedder)

    result = CliRunner().invoke(app, ["analyze"])
    assert result.exit_code == 0, result.output

    row = test_db.execute(
        "SELECT last_analyze FROM pg_stat_user_tables WHERE relname = 'chunks'"
    ).fetchone()
    assert row is not None
    assert row[0] is not None
