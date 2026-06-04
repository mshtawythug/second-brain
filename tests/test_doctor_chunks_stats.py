"""Tests for the ``_check_chunks_stats`` doctor sub-check (perf wave T1).

Uses unittest.mock to patch pg_stat_user_tables so the test does not depend
on actual autovacuum/analyze state in the test DB. The helper is a soft WARN
only — it never flips doctor's exit code.

All rows are synthetic; no production data.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from brain.cli import _check_chunks_stats


def _make_conn(stat_row: tuple | None) -> MagicMock:
    """Build a mock psycopg.Connection whose execute().fetchone() returns stat_row."""
    conn = MagicMock(spec=psycopg.Connection)
    result = MagicMock()
    result.fetchone.return_value = stat_row
    conn.execute.return_value = result
    return conn


def test_chunks_stats_ok_when_analyze_set(capsys: pytest.CaptureFixture) -> None:
    """When last_analyze is set, prints OK line."""
    ts = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    conn = _make_conn((ts, None, 100))
    _check_chunks_stats(conn)
    out = capsys.readouterr().out
    assert "chunks stats" in out
    assert "OK" in out
    assert "2026-05-25" in out


def test_chunks_stats_ok_when_autoanalyze_set(capsys: pytest.CaptureFixture) -> None:
    """When last_autoanalyze is set (but last_analyze NULL), prints OK line."""
    ts = datetime(2026, 5, 24, 8, 0, 0, tzinfo=UTC)
    conn = _make_conn((None, ts, 50))
    _check_chunks_stats(conn)
    out = capsys.readouterr().out
    assert "chunks stats" in out
    assert "OK" in out


def test_chunks_stats_warn_when_both_null_and_nonempty(capsys: pytest.CaptureFixture) -> None:
    """Both analyze timestamps NULL and table non-empty → yellow WARN."""
    conn = _make_conn((None, None, 500))
    with patch("brain.cli.typer.secho") as mock_secho:
        _check_chunks_stats(conn)
    # secho should have been called with yellow fg
    assert mock_secho.called
    call_args = mock_secho.call_args
    assert "WARN" in call_args[0][0]
    assert "500" in call_args[0][0]
    # The remediation now points at the `brain analyze` command (which runs
    # the ANALYZE SQL), so users get a copy-pasteable shell command.
    assert "brain analyze" in call_args[0][0]
    assert call_args[1].get("fg") == "yellow"


def test_chunks_stats_silent_when_table_empty(capsys: pytest.CaptureFixture) -> None:
    """Empty table (n_live_tup=0) → no output at all (fresh install)."""
    conn = _make_conn((None, None, 0))
    with patch("brain.cli.typer.secho") as mock_secho, patch("brain.cli.typer.echo") as mock_echo:
        _check_chunks_stats(conn)
    mock_secho.assert_not_called()
    mock_echo.assert_not_called()


def test_chunks_stats_silent_when_stat_row_none(capsys: pytest.CaptureFixture) -> None:
    """No row in pg_stat_user_tables → silent (table not yet tracked)."""
    conn = _make_conn(None)
    with patch("brain.cli.typer.secho") as mock_secho, patch("brain.cli.typer.echo") as mock_echo:
        _check_chunks_stats(conn)
    mock_secho.assert_not_called()
    mock_echo.assert_not_called()


def test_chunks_stats_warn_on_db_error(capsys: pytest.CaptureFixture) -> None:
    """DB error on the probe → soft WARN, no exception propagated."""
    conn = MagicMock(spec=psycopg.Connection)
    conn.execute.side_effect = psycopg.OperationalError("connection reset")
    with patch("brain.cli.typer.secho") as mock_secho:
        _check_chunks_stats(conn)  # must not raise
    assert mock_secho.called
    warn_text = mock_secho.call_args[0][0]
    assert "WARN" in warn_text
    assert mock_secho.call_args[1].get("fg") == "yellow"


def test_chunks_stats_uses_most_recent_timestamp(capsys: pytest.CaptureFixture) -> None:
    """When both timestamps are set, the more recent one appears in the output."""
    older = datetime(2026, 5, 20, 0, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC)
    conn = _make_conn((older, newer, 200))
    _check_chunks_stats(conn)
    out = capsys.readouterr().out
    assert "2026-05-25" in out
    assert "2026-05-20" not in out
