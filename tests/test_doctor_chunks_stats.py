"""Tests for the ``_check_chunks_stats`` doctor sub-check (perf wave T1).

Rewritten for the C6 fix: the check now reads the AUTHORITATIVE planner
catalogs through :func:`brain.queries.planner_stats_state` instead of the
cumulative-activity counters in ``pg_stat_user_tables``. Those counters are
held in shared memory and discarded on an unclean shutdown, on crash recovery,
and on ``pg_stat_reset*()`` — reading them reported a fully-analyzed table as
"never analyzed" and printed row counts wrong by orders of magnitude (13 for a
13,078-row table on the live machine).

Tests therefore patch ``brain.cli.planner_stats_state`` and assert the doctor's
VERDICT logic, rather than mocking raw SQL tuples. The check stays a soft WARN
that never flips doctor's exit code.

All values are synthetic; no production data.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from brain.cli import _check_chunks_stats
from brain.queries import PlannerStatsState


def _state(
    *,
    exists: bool = True,
    has_rows: bool = True,
    has_planner_stats: bool = True,
    last_analyzed: datetime | None = None,
    estimated_rows: int = 100,
) -> PlannerStatsState:
    return PlannerStatsState(
        exists=exists,
        has_rows=has_rows,
        has_planner_stats=has_planner_stats,
        last_analyzed=last_analyzed,
        estimated_rows=estimated_rows,
    )


def _run(state: PlannerStatsState | Exception) -> MagicMock:
    """Invoke the check with ``planner_stats_state`` stubbed; return the secho mock."""
    conn = MagicMock(spec=psycopg.Connection)
    kwargs = (
        {"side_effect": state} if isinstance(state, Exception) else {"return_value": state}
    )
    with (
        patch("brain.cli.planner_stats_state", **kwargs),
        patch("brain.cli.typer.secho") as mock_secho,
    ):
        _check_chunks_stats(conn)  # must never raise
    return mock_secho


def test_chunks_stats_ok_when_analyzed(capsys: pytest.CaptureFixture) -> None:
    """Planner stats present and the counters intact → OK with the timestamp."""
    ts = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    conn = MagicMock(spec=psycopg.Connection)
    with patch("brain.cli.planner_stats_state", return_value=_state(last_analyzed=ts)):
        _check_chunks_stats(conn)
    out = capsys.readouterr().out
    assert "chunks stats" in out
    assert "OK" in out
    assert "2026-05-25" in out


def test_reset_activity_counters_render_ok_not_warn(
    capsys: pytest.CaptureFixture,
) -> None:
    """THE C6 regression: stats present but counters reset must NOT warn.

    This is the live machine's actual state after the 2026-07-12 restart —
    ``pg_statistic`` holds 10 column entries for ``chunks`` while
    ``last_analyze``/``last_autoanalyze`` are both NULL. The planner is fully
    equipped and no ANALYZE is needed; warning here trains the user to ignore
    doctor, which is its own way of reproducing the outage.
    """
    conn = MagicMock(spec=psycopg.Connection)
    state = _state(last_analyzed=None, estimated_rows=13078)
    with (
        patch("brain.cli.planner_stats_state", return_value=state),
        patch("brain.cli.typer.secho") as mock_secho,
    ):
        _check_chunks_stats(conn)

    out = capsys.readouterr().out
    assert "OK" in out
    assert "WARN" not in out
    assert "13,078" in out, "must report the crash-durable estimate, not n_live_tup"
    assert "counters reset" in out
    mock_secho.assert_not_called(), "a yellow line here is the false positive"


def test_warn_only_when_planner_stats_are_genuinely_absent() -> None:
    """Empty ``pg_statistic`` on a non-empty table is the real never-analyzed case."""
    mock_secho = _run(_state(has_planner_stats=False, estimated_rows=500))

    assert mock_secho.called
    text = mock_secho.call_args[0][0]
    assert "WARN" in text
    assert "never analyzed" in text
    assert "500" in text
    assert "brain analyze" in text
    assert mock_secho.call_args[1].get("fg") == "yellow"


def test_never_analyzed_reports_unknown_rather_than_a_bogus_zero() -> None:
    """A never-analyzed table has ``reltuples = -1`` → normalized 0.

    Printing "0 rows" for a table that is demonstrably non-empty would be the
    same class of lie the old ``n_live_tup`` reporting told.
    """
    mock_secho = _run(_state(has_planner_stats=False, estimated_rows=0))

    text = mock_secho.call_args[0][0]
    assert "WARN" in text
    assert "unknown" in text


def test_silent_when_table_is_empty() -> None:
    """Fresh install before any ingest — no statistics are expected."""
    conn = MagicMock(spec=psycopg.Connection)
    with (
        patch("brain.cli.planner_stats_state", return_value=_state(has_rows=False)),
        patch("brain.cli.typer.secho") as mock_secho,
        patch("brain.cli.typer.echo") as mock_echo,
    ):
        _check_chunks_stats(conn)
    mock_secho.assert_not_called()
    mock_echo.assert_not_called()


def test_silent_when_table_is_absent() -> None:
    conn = MagicMock(spec=psycopg.Connection)
    with (
        patch("brain.cli.planner_stats_state", return_value=_state(exists=False)),
        patch("brain.cli.typer.secho") as mock_secho,
        patch("brain.cli.typer.echo") as mock_echo,
    ):
        _check_chunks_stats(conn)
    mock_secho.assert_not_called()
    mock_echo.assert_not_called()


def test_empty_check_does_not_branch_on_the_row_estimate() -> None:
    """A non-empty but never-analyzed table estimates 0 rows — it must still warn.

    Branching on ``estimated_rows == 0`` instead of the bounded EXISTS probe is
    what made the previous implementation silently skip the very table most in
    need of the warning.
    """
    mock_secho = _run(
        _state(has_rows=True, has_planner_stats=False, estimated_rows=0)
    )

    assert mock_secho.called
    assert "WARN" in mock_secho.call_args[0][0]


def test_warn_on_db_error() -> None:
    """A probe failure is a soft WARN, never an exception or a false OK."""
    mock_secho = _run(psycopg.OperationalError("connection reset"))

    assert mock_secho.called
    assert "WARN" in mock_secho.call_args[0][0]
    assert mock_secho.call_args[1].get("fg") == "yellow"
