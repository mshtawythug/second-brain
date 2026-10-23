"""Unit tests for ``brain.vault.graph_lane.GraphLane``, the watcher's graph lane.

Plan: ``docs/plans/2026-10-22-vault-watcher-startup-blind.md``. The syncer is
a recording fake whose FIRST call blocks until released, which pins a request
"in flight" while the test submits more. Most tests use no DB: the lane's
connection is a fake from an injected factory. Every list assertion waits on a
FIFO barrier (a ``remove``, which is never deduped) rather than on a call
count, so a mutant that changes the count still fails at the targeted
assertion. L11 and L13 run a real ``GraphSyncer`` on the test DB: L11 against
PR #53's per-tenant graph write lock, L13 across a terminated lane backend
(``docs/plans/2026-10-22-vault-watcher-db-reconnect.md``).
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
from tests.thread_wait import WAIT_FOR_BACKSTOP_S, blocked_in_wait, wait_for


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


def test_a_lane_that_cannot_connect_backs_off_and_close_still_ends_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """L9: a failed connect is retried with backoff; requests queue; close ends it.

    Before the reconnect fix (``docs/plans/2026-10-22-vault-watcher-db-reconnect.md``,
    R5) one failed connect turned the lane off for good. ``sleep`` is injected:
    it records each delay, then parks the lane until the test has closed it.
    """
    sleeps: list[float] = []
    parked = threading.Event()
    gate = threading.Event()

    def _refuse() -> Any:
        raise psycopg.OperationalError("synthetic connect failure")

    def _sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 3:
            parked.set()
            gate.wait(WAIT_FOR_BACKSTOP_S)

    lane = GraphLane(_Syncer(), _refuse, sleep=_sleep)  # type: ignore[arg-type]
    lane.remove(None, "queued")  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="brain.vault.graph_lane"):
        lane.start()
        assert parked.wait(WAIT_FOR_BACKSTOP_S), "the lane did not keep retrying its connect"
        lane.remove(None, "late")  # type: ignore[arg-type]  # queues during the outage
        queued = [d for d in lane.undrained_removes() if d != "queued"]  # "queued" is in flight
        # Close WITHOUT a timed-out close() first: a close that times out hands
        # the reporting to its caller (L15), and this test is about the lane's own.
        closed: list[bool] = []
        closer = threading.Thread(
            target=lambda: closed.append(lane.close(WAIT_FOR_BACKSTOP_S)), daemon=True
        )
        closer.start()
        wait_for(lane._is_closing)
        gate.set()
        closer.join(WAIT_FOR_BACKSTOP_S)
        exited = closed == [True]

    assert sleeps == [1, 2, 4], f"the connect retries did not back off: {sleeps}"
    assert queued == ["late"], f"a request did not queue during the outage: {queued}"
    assert exited, "close did not end a lane that cannot connect"
    errors = " ".join(r.getMessage() for r in caplog.records)
    assert "2 graph remove(s) not applied (queued, late)" in errors, (
        f"giving up did not name the removes it dropped: {errors!r}"
    )


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


# ---------------------------------------------------------------------------
# L12-L14: the lane reconnects after a Postgres restart
# (docs/plans/2026-10-22-vault-watcher-db-reconnect.md).
# ---------------------------------------------------------------------------


class _NamedConn(_Conn):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name


class _DyingSyncer:
    """Like ``GraphSyncer``, never raises. Connection c1 dies on its SECOND request.

    A request on a closed connection does nothing (``GraphSyncer`` would log and
    swallow the error), so only ``applied`` shows what really ran.
    """

    def __init__(self) -> None:
        self.applied: list[tuple[str, str, str]] = []
        self._on_c1 = 0

    def _run(self, op: str, conn: _NamedConn, document_id: str) -> None:
        if conn.closed:
            return
        if conn.name == "c1":
            self._on_c1 += 1
            if self._on_c1 == 2:
                conn.closed = True  # the server dropped it mid-request
                return
        self.applied.append((op, document_id, conn.name))

    def reconcile(self, conn: Any, document_id: str) -> None:
        self._run("reconcile", conn, document_id)

    def remove(self, conn: Any, document_id: str) -> None:
        self._run("remove", conn, document_id)


def test_the_lane_reruns_on_a_fresh_connection_after_its_connection_dies() -> None:
    """L12: the request that hit the dead connection, and every later one, run on c2."""
    made: list[_NamedConn] = []

    def _factory() -> _NamedConn:
        made.append(_NamedConn(f"c{len(made) + 1}"))
        return made[-1]

    syncer = _DyingSyncer()
    lane = GraphLane(syncer, _factory)  # type: ignore[arg-type]  # c2 never fails: no backoff
    lane.start()
    try:
        for doc in ("a", "b", "c"):
            lane.reconcile(None, doc)  # type: ignore[arg-type]
        token = f"barrier-{uuid.uuid4()}"
        lane.remove(None, token)  # type: ignore[arg-type]
        ran = _settled(lambda: ("remove", token, "c2") in syncer.applied)
    finally:
        lane.close(WAIT_FOR_BACKSTOP_S)

    assert ran and syncer.applied[:3] == [
        ("reconcile", "a", "c1"),
        ("reconcile", "b", "c2"),
        ("reconcile", "c", "c2"),
    ], f"the lane did not re-run on a fresh connection: {syncer.applied!r}"
    assert made[0].closed and len(made) == 2, "the dead connection was not replaced exactly once"


def _gone(conn: Any, pid: int) -> bool:
    row = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
    return bool(row == (0,))


def test_lane_reconciles_after_its_backend_is_terminated(test_db: Any) -> None:
    """L13: real DB; the lane's backend is terminated between two reconciles."""
    from brain.db import connect_raw
    from brain.graph_rag.reconcile import ReconcileConfig
    from brain.graph_rag.sync import GraphSyncer
    from tests.conftest import TEST_DATABASE_URL
    from tests.graph_fakes import NullBackend, seed_note_doc

    assert ":5434/" in TEST_DATABASE_URL, "this test terminates a backend: test DB only"
    first_doc = seed_note_doc(test_db, "lane-first")
    second_doc = seed_note_doc(test_db, "lane-second")
    made: list[Any] = []

    def _factory() -> Any:
        made.append(connect_raw(TEST_DATABASE_URL))
        return made[-1]

    syncer = GraphSyncer(
        ReconcileConfig(tenant_id="default", generic_df_ratio=1.0),
        enabled=True,
        backend=NullBackend(),  # type: ignore[arg-type]
    )
    lane = GraphLane(syncer, _factory)
    lane.start()
    try:
        lane.reconcile(None, first_doc)  # type: ignore[arg-type]
        wait_for(lambda: _watermarks(test_db, first_doc) >= 1)
        pid = made[0].info.backend_pid
        test_db.execute("SELECT pg_terminate_backend(%s)", (pid,))
        wait_for(lambda: _gone(test_db, pid))
        lane.reconcile(None, second_doc)  # type: ignore[arg-type]
        done = _settled(lambda: _watermarks(test_db, second_doc) >= 1)
    finally:
        lane.close(WAIT_FOR_BACKSTOP_S)
        for conn in made:
            conn.close()

    assert done, "the lane's reconcile after its backend was terminated never completed"


def test_close_wakes_a_lane_in_reconnect_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """L14: the DEFAULT sleep wakes on close; a 600s backoff must not hold up shutdown.

    ``close`` itself is bounded, not just its result: a sleep that holds the
    lane's condition (mutation G3, round 2) makes ``close`` block on that lock
    for the whole backoff and THEN report a clean exit.
    """
    monkeypatch.setattr("brain.vault.db_reconnect.BACKOFF_INITIAL_S", 600.0)
    monkeypatch.setattr("brain.vault.db_reconnect.BACKOFF_MAX_S", 600.0)

    def _refuse() -> Any:
        raise psycopg.OperationalError("synthetic connect failure")

    lane = GraphLane(_Syncer(), _refuse)  # type: ignore[arg-type]
    lane.reconcile(None, "a")  # type: ignore[arg-type]
    lane.start()
    # INSIDE the sleep, not just past the failed connect: a close that lands
    # before the lane's stopping() check never exercises the sleep (mutation G3).
    wait_for(lambda: blocked_in_wait(lane._thread, "_connect"))
    closed: list[bool] = []
    closer = threading.Thread(
        target=lambda: closed.append(lane.close(WAIT_FOR_BACKSTOP_S)), daemon=True
    )
    closer.start()
    closer.join(WAIT_FOR_BACKSTOP_S)

    assert closed == [True], "close did not wake a lane sleeping out its reconnect backoff"


def test_a_remove_in_flight_during_a_slow_reconnect_is_named_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """L15: a close that times out names the remove the lane has already popped.

    PR #56 review: ``undrained_removes`` named only QUEUED removes, so a remove
    in flight while the lane was still reconnecting was in neither list. The
    timed-out close hands the report to its caller; when the lane later gives
    up, it must not log the same remove again.
    """
    entered, release = threading.Event(), threading.Event()

    def _slow_refuse() -> Any:
        entered.set()
        release.wait(WAIT_FOR_BACKSTOP_S)
        raise psycopg.OperationalError("synthetic connect failure")

    lane = GraphLane(_Syncer(), _slow_refuse)  # type: ignore[arg-type]
    lane.remove(None, "doomed")  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="brain.vault.graph_lane"):
        lane.start()
        assert entered.wait(WAIT_FOR_BACKSTOP_S), "fixture: the lane never tried to connect"
        timed_out = lane.close(0.05) is False
        undrained = lane.undrained_removes()
        release.set()
        exited = lane.close(WAIT_FOR_BACKSTOP_S)

    assert timed_out, "fixture: close must time out while the lane is connecting"
    assert undrained == ["doomed"], (
        f"a remove in flight during a slow reconnect was not named: {undrained!r}"
    )
    assert exited, "the lane did not exit once its connect gave up"
    again = [r.getMessage() for r in caplog.records if "not applied" in r.getMessage()]
    assert again == [], f"the lane logged removes its caller had already reported: {again!r}"


def test_an_applied_remove_is_not_reported_as_undrained() -> None:
    """L16: a finished request is no longer in flight, even while the lane idles."""
    syncer = _Syncer()
    syncer.release.set()
    with _lane(syncer) as (lane, _conn):
        token = f"barrier-{uuid.uuid4()}"
        lane.remove(None, token)  # type: ignore[arg-type]
        wait_for(lambda: ("remove", token) in syncer.calls)
        cleared = _settled(lambda: lane.undrained_removes() == [])

    assert cleared, "an applied remove was still reported as undrained"
