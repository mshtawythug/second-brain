"""Tests for auto-ANALYZE wiring after bulk writes (ingest-dir / reembed / vault sync).

Two layers:
- Guard tests (spy on ``brain.cli._analyze_after_bulk_write``) prove the ANALYZE
  fires only when a run actually wrote — deterministic, no DB stats latency.
- One integration test proves the real ANALYZE lands in
  ``pg_stat_user_tables.last_analyze`` for ``chunks`` and ``documents``.
"""
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _patch_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import FakeEmbedder

    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())


def _wire_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # Sandbox the vault so ingest mirror-writes don't touch the real vault.
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    _patch_embedder(monkeypatch)


# --------------------------------------------------------------------------- #
# ingest-dir
# --------------------------------------------------------------------------- #
def test_ingest_dir_calls_analyze_when_docs_written(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    _wire_env(monkeypatch, tmp_path)
    spy = MagicMock()
    monkeypatch.setattr("brain.cli._analyze_after_bulk_write", spy)

    result = CliRunner().invoke(app, ["ingest-dir", str(fixtures_dir)])

    assert result.exit_code == 0, result.output
    spy.assert_called_once()
    assert spy.call_args.kwargs["context"] == "ingest-dir"


def test_ingest_dir_skips_analyze_when_nothing_written(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    _wire_env(monkeypatch, tmp_path)
    runner = CliRunner()
    # First run writes; second run re-sees identical content → all skipped.
    first = runner.invoke(app, ["ingest-dir", str(fixtures_dir)])
    assert first.exit_code == 0, first.output

    spy = MagicMock()
    monkeypatch.setattr("brain.cli._analyze_after_bulk_write", spy)
    second = runner.invoke(app, ["ingest-dir", str(fixtures_dir)])

    assert second.exit_code == 0, second.output
    spy.assert_not_called()


def test_ingest_dir_dry_run_skips_analyze(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    _wire_env(monkeypatch, tmp_path)
    spy = MagicMock()
    monkeypatch.setattr("brain.cli._analyze_after_bulk_write", spy)

    result = CliRunner().invoke(app, ["ingest-dir", str(fixtures_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    spy.assert_not_called()


# --------------------------------------------------------------------------- #
# reembed
# --------------------------------------------------------------------------- #
def test_reembed_calls_analyze_when_chunks_embedded(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    _wire_env(monkeypatch, tmp_path)
    runner = CliRunner()
    seed = runner.invoke(app, ["ingest-dir", str(fixtures_dir)])
    assert seed.exit_code == 0, seed.output

    spy = MagicMock()
    monkeypatch.setattr("brain.cli._analyze_after_bulk_write", spy)
    # --all re-embeds every chunk → embedded >= 1.
    result = runner.invoke(app, ["reembed", "--all", "--no-finalize"])

    assert result.exit_code == 0, result.output
    spy.assert_called_once()
    assert spy.call_args.kwargs["context"] == "reembed"


def test_reembed_skips_analyze_when_nothing_embedded(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    _wire_env(monkeypatch, tmp_path)
    runner = CliRunner()
    seed = runner.invoke(app, ["ingest-dir", str(fixtures_dir)])
    assert seed.exit_code == 0, seed.output

    spy = MagicMock()
    monkeypatch.setattr("brain.cli._analyze_after_bulk_write", spy)
    # No --all and every chunk already embedded → nothing to embed.
    result = runner.invoke(app, ["reembed", "--no-finalize"])

    assert result.exit_code == 0, result.output
    assert "nothing to embed" in result.output.lower()
    spy.assert_not_called()


# --------------------------------------------------------------------------- #
# vault sync
# --------------------------------------------------------------------------- #
def test_vault_sync_calls_analyze_when_written(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    _wire_env(monkeypatch, tmp_path)
    vault = tmp_path / "synced_vault"
    vault.mkdir()
    # Needs frontmatter with a title (sync skips files with no frontmatter as
    # documentation); no ``id`` so the sync creates a fresh row.
    (vault / "note.md").write_text(
        "---\ntitle: A Note\n---\n\nSome body text.\n"
    )

    spy = MagicMock()
    monkeypatch.setattr("brain.cli._analyze_after_bulk_write", spy)
    result = CliRunner().invoke(app, ["vault", "sync", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    spy.assert_called_once()
    assert spy.call_args.kwargs["context"] == "vault sync"


def test_vault_sync_dry_run_skips_analyze(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    _wire_env(monkeypatch, tmp_path)
    vault = tmp_path / "synced_vault"
    vault.mkdir()
    # Needs frontmatter with a title (sync skips files with no frontmatter as
    # documentation); no ``id`` so the sync creates a fresh row.
    (vault / "note.md").write_text(
        "---\ntitle: A Note\n---\n\nSome body text.\n"
    )

    spy = MagicMock()
    monkeypatch.setattr("brain.cli._analyze_after_bulk_write", spy)
    result = CliRunner().invoke(
        app, ["vault", "sync", "--vault", str(vault), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    spy.assert_not_called()


# --------------------------------------------------------------------------- #
# a failed ANALYZE must never fail the already-committed bulk write
# --------------------------------------------------------------------------- #
def test_ingest_dir_survives_analyze_error(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ANALYZE runs after the ingest already committed, so a failure there is
    downgraded to a warning — the command still exits 0."""
    _wire_env(monkeypatch, tmp_path)

    def _boom(conn: object, tables: object) -> None:
        raise psycopg.OperationalError("simulated ANALYZE lock timeout")

    # Patch the underlying ANALYZE runner (not the wrapper) so the real
    # try/except in `_analyze_after_bulk_write` is exercised.
    monkeypatch.setattr("brain.cli.analyze_tables", _boom)

    with caplog.at_level(logging.WARNING, logger="brain.cli"):
        result = CliRunner().invoke(app, ["ingest-dir", str(fixtures_dir)])

    assert result.exit_code == 0, result.output
    assert any(
        "auto-ANALYZE" in rec.message and "failed" in rec.message
        for rec in caplog.records
    ), [rec.message for rec in caplog.records]


# --------------------------------------------------------------------------- #
# real ANALYZE lands in pg_stat_user_tables (integration)
# --------------------------------------------------------------------------- #
def test_ingest_dir_updates_pg_stat_last_analyze(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """After a real ingest-dir, ANALYZE lands on chunks + documents.

    ``test_db`` reset the schema, so ``last_analyze`` starts NULL for both
    tables. The CLI opens its own connection and runs ANALYZE at the end of
    the ingest; ``pg_stat_user_tables`` is database-global, so we read it back
    from the fixture connection.
    """
    _wire_env(monkeypatch, tmp_path)

    before = _last_analyze(test_db)
    assert before["chunks"] is None
    assert before["documents"] is None

    result = CliRunner().invoke(app, ["ingest-dir", str(fixtures_dir)])
    assert result.exit_code == 0, result.output

    after = _last_analyze(test_db)
    assert after["chunks"] is not None, "chunks should have been ANALYZEd"
    assert after["documents"] is not None, "documents should have been ANALYZEd"


def _last_analyze(conn: psycopg.Connection) -> dict[str, object]:
    conn.execute("SELECT pg_stat_clear_snapshot()")
    rows = conn.execute(
        "SELECT relname, last_analyze FROM pg_stat_user_tables "
        "WHERE relname IN ('chunks', 'documents')"
    ).fetchall()
    return {relname: last_analyze for relname, last_analyze in rows}
