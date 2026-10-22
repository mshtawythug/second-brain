"""Unit tests for ``brain.vault.graph_lane.GraphLane``, the watcher's graph lane.

Plan: ``docs/plans/2026-10-22-vault-watcher-startup-blind.md``. The syncer is
a recording fake whose FIRST call blocks until released, which pins a request
"in flight" while the test submits more. L1-L10 use no DB: the lane's
connection is a fake from an injected factory. Every list assertion waits on a
FIFO barrier (a ``remove``, which is never deduped) rather than on a call
count, so a mutant that changes the count still fails at the targeted
assertion. L11 is the one DB test: a real ``GraphSyncer`` on the lane, against
PR #53's per-tenant graph write lock.
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
    lane.remove(None, "queued")  # type: ignore[arg-type]  # pending when the connect fails
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
    errors = " ".join(r.getMessage() for r in caplog.records)
    assert "1 graph remove(s) not applied (queued)" in errors, (
        f"the failed connect did not name the removes it dropped: {errors!r}"
    )
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


# ---------------------------------------------------------------------------
# L11: the lane composes with PR #53's per-tenant graph write lock. A REAL
# GraphSyncer reconcile runs on the lane's own autocommit connection (its write
# transaction takes the tenant lock; the extraction runs with no transaction
# open), waits behind another session holding that lock, and completes once
# it is released. The backend is #53's NullBackend: only the relational write
# path, and its lock, are under test.
# ---------------------------------------------------------------------------


def _watermarks(conn: Any, document_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_index_state WHERE document_id = %s", (document_id,)
    ).fetchone()
    return int(row[0])


def _advisory_waiters(conn: Any) -> int:
    row = conn.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    ).fetchone()
    return int(row[0])


def test_lane_reconcile_waits_on_the_tenant_write_lock(test_db: Any) -> None:
    """L11: a real reconcile on the lane waits behind the tenant lock, then completes."""
    from brain.db import connect_raw
    from brain.graph_rag.reconcile import ReconcileConfig
    from brain.graph_rag.sync import GraphSyncer
    from brain.graph_rag.write_lock import acquire_graph_write_lock
    from tests.conftest import TEST_DATABASE_URL
    from tests.graph_fakes import NullBackend, seed_note_doc

    doc = seed_note_doc(test_db, "lane-doc")
    syncer = GraphSyncer(
        ReconcileConfig(tenant_id="default", generic_df_ratio=1.0),
        enabled=True,
        backend=NullBackend(),  # type: ignore[arg-type]
    )
    lane = GraphLane(syncer, lambda: connect_raw(TEST_DATABASE_URL))
    holder = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    try:
        with holder.transaction():
            acquire_graph_write_lock(holder, "default")
            lane.start()
            lane.reconcile(None, doc)  # type: ignore[arg-type]
            waited = _settled(lambda: _advisory_waiters(holder) >= 1)
            written_while_held = _watermarks(test_db, doc)
        done = _settled(lambda: _watermarks(test_db, doc) >= 1)
    finally:
        holder.close()
        lane.close(WAIT_FOR_BACKSTOP_S)

    assert waited, "the lane's reconcile did not wait on the tenant write lock"
    assert written_while_held == 0, "the lane reconciled while another writer held the lock"
    assert done, "the lane's reconcile never completed after the tenant lock was released"
