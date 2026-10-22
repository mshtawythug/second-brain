"""Unit tests for ``brain.vault.graph_lane.GraphLane``, the watcher's graph lane.

Plan: ``docs/plans/2026-10-22-vault-watcher-startup-blind.md``. The syncer is
a recording fake whose FIRST call blocks until released, which pins a request
"in flight" while the test submits more. No DB: the lane's connection is a
fake from an injected factory. Every list assertion waits on a FIFO barrier (a
``remove``, which is never deduped) rather than on a call count, so a mutant
that changes the count still fails at the targeted assertion.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

from brain.vault.graph_lane import GraphLane
from tests.thread_wait import WAIT_FOR_BACKSTOP_S, wait_for


class _Conn:
    def __init__(self) -> None:
        self.autocommit = False
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Syncer:
    """Records ``(op, doc)`` and the connection; the first call blocks until ``release``."""

    def __init__(self, raise_first: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.conns: list[Any] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._raise_first = raise_first

    def _record(self, op: str, conn: Any, document_id: str) -> None:
        if self._first:
            self._first = False
            if self._raise_first:
                raise RuntimeError("synthetic reconcile failure")
            self.calls.append((op, document_id))
            self.conns.append(conn)
            self.entered.set()
            self.release.wait(WAIT_FOR_BACKSTOP_S * 3)
            return
        self.calls.append((op, document_id))
        self.conns.append(conn)

    def reconcile(self, conn: Any, document_id: str) -> None:
        self._record("reconcile", conn, document_id)

    def remove(self, conn: Any, document_id: str) -> None:
        self._record("remove", conn, document_id)


@contextmanager
def _lane(syncer: _Syncer) -> Iterator[tuple[GraphLane, _Conn]]:
    conn = _Conn()
    lane = GraphLane(syncer, lambda: conn)  # type: ignore[arg-type,return-value]
    lane.start()
    try:
        yield lane, conn
    finally:
        syncer.release.set()
        lane.close(WAIT_FOR_BACKSTOP_S)


def _a_in_flight(lane: GraphLane, syncer: _Syncer) -> None:
    lane.reconcile(None, "a")  # type: ignore[arg-type]
    assert syncer.entered.wait(WAIT_FOR_BACKSTOP_S), "fixture: a's reconcile never started"


def _drained(lane: GraphLane, syncer: _Syncer) -> list[tuple[str, str]]:
    """Release, push a barrier remove, wait for it; return the calls before it."""
    syncer.release.set()
    token = f"barrier-{uuid.uuid4()}"
    lane.remove(None, token)  # type: ignore[arg-type]
    wait_for(lambda: ("remove", token) in syncer.calls)
    return syncer.calls[: syncer.calls.index(("remove", token))]


def test_pending_reconcile_is_deduped_and_fifo_is_kept() -> None:
    """L1."""
    syncer = _Syncer()
    with _lane(syncer) as (lane, _conn):
        _a_in_flight(lane, syncer)
        for doc in ("b", "b", "c", "b"):
            lane.reconcile(None, doc)  # type: ignore[arg-type]
        calls = _drained(lane, syncer)

    assert calls == [("reconcile", "a"), ("reconcile", "b"), ("reconcile", "c")], (
        f"a pending reconcile was not deduped, or FIFO order was lost: {calls!r}"
    )


def test_reconcile_requested_while_in_flight_runs_again() -> None:
    """L2: an edit landing mid-reconcile is reconciled after it."""
    syncer = _Syncer()
    with _lane(syncer) as (lane, _conn):
        _a_in_flight(lane, syncer)
        lane.reconcile(None, "a")  # type: ignore[arg-type]
        calls = _drained(lane, syncer)

    assert calls == [("reconcile", "a"), ("reconcile", "a")], (
        f"a reconcile requested while in flight was dropped: {calls!r}"
    )


def test_remove_is_never_deduped_and_keeps_its_place() -> None:
    """L3: reconcile b, remove b, reconcile b runs all three, in order."""
    syncer = _Syncer()
    with _lane(syncer) as (lane, _conn):
        _a_in_flight(lane, syncer)
        lane.reconcile(None, "b")  # type: ignore[arg-type]
        lane.remove(None, "b")  # type: ignore[arg-type]
        lane.reconcile(None, "b")  # type: ignore[arg-type]
        lane.remove(None, "c")  # type: ignore[arg-type]
        lane.remove(None, "c")  # type: ignore[arg-type]
        calls = _drained(lane, syncer)

    assert calls == [
        ("reconcile", "a"),
        ("reconcile", "b"),
        ("remove", "b"),
        ("reconcile", "b"),
        ("remove", "c"),
        ("remove", "c"),
    ], f"a remove was deduped or reordered: {calls!r}"


def test_close_drops_reconciles_drains_removes_and_ignores_later_requests() -> None:
    """L4."""
    syncer = _Syncer()
    with _lane(syncer) as (lane, _conn):
        _a_in_flight(lane, syncer)
        lane.reconcile(None, "b")  # type: ignore[arg-type]
        lane.remove(None, "c")  # type: ignore[arg-type]

        lane.close(0)
        lane.reconcile(None, "d")  # type: ignore[arg-type]
        lane.remove(None, "e")  # type: ignore[arg-type]
        syncer.release.set()
        exited = lane.close(WAIT_FOR_BACKSTOP_S)

    assert exited, "the lane thread did not exit after close"
    assert syncer.calls == [("reconcile", "a"), ("remove", "c")], (
        f"close did not drop reconciles, drain removes, and ignore later requests: {syncer.calls!r}"
    )


def test_close_is_bounded_while_a_reconcile_is_in_flight() -> None:
    """L5: close returns False on timeout; the thread then finishes and exits."""
    syncer = _Syncer()
    with _lane(syncer) as (lane, conn):
        _a_in_flight(lane, syncer)
        timed_out = lane.close(0.05) is False
        syncer.release.set()
        exited = lane.close(WAIT_FOR_BACKSTOP_S)

    assert timed_out, "close claimed an exit while a reconcile was in flight"
    assert exited, "the lane thread never exited"
    assert conn.closed, "the lane did not close its own connection"


def test_syncer_gets_the_lanes_own_connection() -> None:
    """L6: the caller's connection is ignored; the lane uses its own (autocommit)."""
    syncer = _Syncer()
    syncer.release.set()
    with _lane(syncer) as (lane, conn):
        lane.reconcile(object(), "a")  # type: ignore[arg-type]
        _drained(lane, syncer)

    assert syncer.conns[0] is conn and conn.autocommit, (
        "the syncer did not run on the lane's own autocommit connection"
    )


def test_a_failing_request_does_not_stop_the_lane(caplog: pytest.LogCaptureFixture) -> None:
    """L7: one request raising must not stop the lane; the next one still runs.

    The failure is logged by class only: an exception message can quote row
    values, which must never reach a WARNING log.
    """
    syncer = _Syncer(raise_first=True)
    with (
        caplog.at_level(logging.WARNING, logger="brain.vault.graph_lane"),
        _lane(syncer) as (lane, _conn),
    ):
        lane.reconcile(None, "a")  # type: ignore[arg-type]
        lane.reconcile(None, "b")  # type: ignore[arg-type]
        token = f"barrier-{uuid.uuid4()}"
        lane.remove(None, token)  # type: ignore[arg-type]
        processed = _settled(lambda: ("remove", token) in syncer.calls)

    assert processed and ("reconcile", "b") in syncer.calls, (
        f"the lane stopped after one failing request: {syncer.calls!r}"
    )
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "synthetic reconcile failure" not in logged, (
        f"the failure was not logged by class only: {logged!r}"
    )


def _settled(predicate: Any) -> bool:
    try:
        wait_for(predicate)
    except AssertionError:
        return False
    return True


def test_close_reports_removes_left_undrained_by_a_timeout() -> None:
    """L8: a remove still queued when close times out is reported, not silently lost."""
    syncer = _Syncer()
    with _lane(syncer) as (lane, _conn):
        _a_in_flight(lane, syncer)
        lane.remove(None, "gone")  # type: ignore[arg-type]
        timed_out = lane.close(0.05) is False
        undrained = lane.undrained_removes()

    assert timed_out, "fixture: close must time out while a reconcile is in flight"
    assert undrained == ["gone"], f"the undrained remove was not reported: {undrained!r}"


def test_a_failed_connect_disables_the_lane(caplog: pytest.LogCaptureFixture) -> None:
    """L9: a lane that cannot connect logs it and turns later submits into no-ops."""

    def _refuse() -> Any:
        raise psycopg.OperationalError("synthetic connect failure")

    lane = GraphLane(_Syncer(), _refuse)  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="brain.vault.graph_lane"):
        lane.start()
        # The lane marks itself closed BEFORE it logs, so the record proves it.
        logged = _settled(
            lambda: any("could not connect" in r.getMessage() for r in caplog.records)
        )
    lane.remove(None, "late")  # type: ignore[arg-type]  # no close() first
    queued = lane.undrained_removes()
    exited = lane.close(WAIT_FOR_BACKSTOP_S)

    assert logged, "the failed connect was not logged"
    assert queued == [], "a submit after a failed connect was queued"
    assert exited, "the lane thread did not exit after a failed connect"


def test_an_idle_lane_closes_at_once() -> None:
    """L10: closing an idle lane wakes its thread; it must not wait out the timeout."""
    syncer = _Syncer()
    syncer.release.set()
    conn = _Conn()
    lane = GraphLane(syncer, lambda: conn)  # type: ignore[arg-type,return-value]
    lane.start()
    token = f"barrier-{uuid.uuid4()}"
    lane.remove(None, token)  # type: ignore[arg-type]
    wait_for(lambda: ("remove", token) in syncer.calls)  # the lane is now idle

    assert lane.close(1.0) is True, "closing an idle lane did not wake its thread"
