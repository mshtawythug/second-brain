"""Unit tests for ``brain.vault.db_reconnect``: the watcher's bounded reconnect.

Plan: ``docs/plans/2026-10-22-vault-watcher-db-reconnect.md`` (C1-C8). No DB:
connections are fakes from an injected factory, and ``sleep`` / ``stopping``
are injected, so no test sleeps through a backoff.
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

import psycopg
import pytest

from brain.vault.db_reconnect import (
    ReconnectAbandoned,
    ReconnectingConnection,
    backoff_delays,
)

_SECRET = "row value that must never reach a log"


class _Conn:
    def __init__(self, name: str) -> None:
        self.name = name
        self.autocommit = False
        self.closed = False
        self.close_calls = 0

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1


class _Factory:
    """Hands out ``_Conn``s; the first ``fail`` calls raise instead."""

    def __init__(self, fail: int = 0) -> None:
        self.fail = fail
        self.calls = 0
        self.made: list[_Conn] = []

    def __call__(self) -> _Conn:
        self.calls += 1
        if self.calls <= self.fail:
            raise psycopg.OperationalError(f"synthetic connect failure: {_SECRET}")
        conn = _Conn(f"c{len(self.made) + 1}")
        self.made.append(conn)
        return conn


def _die(conn: _Conn) -> None:
    """What a statement on a terminated backend does: the conn closes, then raises."""
    conn.closed = True
    raise psycopg.OperationalError(f"terminating connection: {_SECRET}")


def _link(
    factory: _Factory,
    *,
    conn: _Conn | None = None,
    sleeps: list[float] | None = None,
    stopping: Any = lambda: False,
) -> ReconnectingConnection:
    recorded = sleeps if sleeps is not None else []
    return ReconnectingConnection(
        factory,  # type: ignore[arg-type]
        label="test",
        sleep=recorded.append,
        stopping=stopping,
        conn=conn,  # type: ignore[arg-type]
    )


def test_backoff_doubles_from_one_second_and_caps_at_thirty() -> None:
    """C1."""
    assert list(itertools.islice(backoff_delays(), 7)) == [1, 2, 4, 8, 16, 30, 30]


def test_a_lost_connection_is_replaced_and_the_job_rerun_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C2: the job that hit the dead connection re-runs on a fresh autocommit one."""
    factory = _Factory()
    first = _Conn("first")
    link = _link(factory, conn=first)
    seen: list[str] = []

    def job(conn: _Conn) -> str:
        seen.append(conn.name)
        if conn is first:
            _die(conn)
        return f"done on {conn.name}"

    with caplog.at_level(logging.DEBUG, logger="brain.vault.db_reconnect"):
        result = link.call(job)  # type: ignore[arg-type]

    assert seen == ["first", "c1"], f"the job was not re-run once on a fresh connection: {seen}"
    assert result == "done on c1", "the retried job's value was not returned"
    assert factory.made[0].autocommit, "the fresh connection is not autocommit"
    assert first.close_calls >= 1, "the dead connection was not closed"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "OperationalError" in logged and _SECRET not in logged, (
        f"the loss was not logged by exception class only: {logged!r}"
    )


def test_an_error_on_a_healthy_connection_does_not_reconnect() -> None:
    """C3: LockNotAvailable / DeadlockDetected leave the connection open: no reconnect."""
    factory = _Factory()
    healthy = _Conn("healthy")
    link = _link(factory, conn=healthy)
    runs: list[str] = []

    def job(conn: _Conn) -> None:
        runs.append(conn.name)
        raise psycopg.errors.LockNotAvailable("synthetic lock timeout")

    with pytest.raises(psycopg.errors.LockNotAvailable):
        link.call(job)  # type: ignore[arg-type]

    assert runs == ["healthy"], f"a healthy-connection error re-ran the job: {runs}"
    assert factory.calls == 0, "a healthy-connection error reconnected"


def test_failed_connects_back_off_then_restore_and_log_transitions_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C4: three failed connects sleep 1, 2, 4; logs: loss once, restore once, no content."""
    factory = _Factory(fail=3)
    sleeps: list[float] = []
    dead = _Conn("dead")
    dead.closed = True
    link = _link(factory, conn=dead, sleeps=sleeps)

    with caplog.at_level(logging.DEBUG, logger="brain.vault.db_reconnect"):
        name = link.call(lambda c: c.name)  # type: ignore[arg-type,attr-defined]

    assert name == "c1", "the job did not run on the restored connection"
    assert sleeps == [1, 2, 4], f"the backoff did not follow the schedule: {sleeps}"
    above_debug = [r for r in caplog.records if r.levelno > logging.DEBUG]
    levels = [r.levelname for r in above_debug]
    assert levels == ["WARNING", "INFO"], f"logged more than the two transitions: {levels}"
    assert "3 failed attempt(s)" in above_debug[1].getMessage(), (
        f"the restore did not count the failed attempts: {above_debug[1].getMessage()!r}"
    )
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert _SECRET not in logged, f"an exception message reached the log: {logged!r}"


def test_a_stopping_link_gives_up_after_one_failed_attempt_without_sleeping() -> None:
    """C5."""
    factory = _Factory(fail=99)
    sleeps: list[float] = []
    link = _link(factory, sleeps=sleeps, stopping=lambda: True)

    with pytest.raises(ReconnectAbandoned):
        link.call(lambda c: None)  # type: ignore[arg-type]

    assert factory.calls == 1, f"a stopping link kept trying: {factory.calls} attempts"
    assert sleeps == [], f"a stopping link slept: {sleeps}"


def test_a_retry_that_loses_its_connection_again_propagates() -> None:
    """C6: at most two runs per job, so a flapping DB cannot loop one job forever."""
    factory = _Factory()
    link = _link(factory, conn=_Conn("first"))
    runs: list[str] = []

    def job(conn: _Conn) -> None:
        runs.append(conn.name)
        _die(conn)

    with pytest.raises(psycopg.OperationalError):
        link.call(job)  # type: ignore[arg-type]

    assert runs == ["first", "c1"], f"the job did not run exactly twice: {runs}"


def test_close_closes_the_current_connection_and_is_idempotent() -> None:
    """C7: after a reconnect, close() closes the NEW connection."""
    factory = _Factory()
    first = _Conn("first")
    first.closed = True
    link = _link(factory, conn=first)
    link.call(lambda c: None)  # type: ignore[arg-type]

    link.close()
    link.close()

    assert factory.made[0].closed, "close() did not close the current connection"


def test_a_stop_during_backoff_gets_one_more_attempt_then_gives_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C8: shutdown wakes the backoff; the link tries once more, then gives up."""
    factory = _Factory(fail=99)
    stop = {"set": False}
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        stop["set"] = True  # shutdown requested while sleeping

    link = ReconnectingConnection(
        factory,  # type: ignore[arg-type]
        label="test",
        sleep=sleep,
        stopping=lambda: stop["set"],
    )

    with (
        caplog.at_level(logging.WARNING, logger="brain.vault.db_reconnect"),
        pytest.raises(ReconnectAbandoned),
    ):
        link.call(lambda c: None)  # type: ignore[arg-type]

    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "OperationalError" in warnings[0], (
        f"the first failed connect was not logged once, by class: {warnings!r}"
    )
    assert _SECRET not in warnings[0], f"an exception message reached the log: {warnings!r}"
    assert (factory.calls, sleeps) == (2, [1]), (
        f"a stop during backoff did not get exactly one more attempt: "
        f"{factory.calls} attempts, sleeps {sleeps}"
    )


def test_a_job_that_returns_on_a_now_closed_connection_is_rerun() -> None:
    """C9: ``GraphSyncer`` swallows its own errors, so a loss can come back as a return."""
    factory = _Factory()
    first = _Conn("first")
    link = _link(factory, conn=first)
    seen: list[str] = []

    def never_raises(conn: _Conn) -> None:
        seen.append(conn.name)
        if conn is first:
            conn.closed = True  # the statement failed; the callee logged and returned

    link.call(never_raises)  # type: ignore[arg-type]

    assert seen == ["first", "c1"], (
        f"a job that returned on a dead connection was not re-run: {seen}"
    )


def test_the_backoff_survives_a_long_outage() -> None:
    """C10: the 2,000th delay is still the cap (``1.0 * 2**1024`` overflows a float).

    Found by mutation D7: a reconnect loop past about 1,024 attempts (about 8.5
    hours of outage at the 30s cap) raised ``OverflowError`` out of the backoff
    instead of waiting.
    """
    delays = list(itertools.islice(backoff_delays(), 2000))

    assert delays[-1] == 30, f"the backoff did not hold at its cap: {delays[-1]!r}"
