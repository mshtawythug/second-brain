"""Regression tests for the vault watcher's debounce ``_fire`` race (2026-10-22).

``threading.Timer.cancel()`` cannot stop a callback that has already started.
At ``a6a76fe``, a superseded timer whose ``_fire`` was already running popped
``pending_timers[path]`` BY PATH. That removed its successor's entry, so one
burst produced two jobs, and the orphaned successor was invisible to the
shutdown drain: it could fire after the sentinel, losing the edit. ``_fire``
also enqueued outside ``state.lock``, so a drain could slip in between its pop
and its put. Plan: ``docs/plans/2026-10-22-vault-watcher-fire-race.md``.

No threads and no sleeps. ``_WatcherState.timer_factory`` injects
``_ManualTimer``, whose ``fire()`` runs the callback on the test thread
UNCONDITIONALLY. That is exactly a timer thread that has already passed its
cancel check, so each race is replayed as a fixed sequence of calls.
"""

from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from watchdog.events import FileModifiedEvent

from brain.vault.watch import (
    WatchConfig,
    _drain_pending_timers,
    _Handler,
    _Job,
    _schedule_debounced,
    _WatcherState,
)


class _ManualTimer:
    """A debounce timer the test fires by hand."""

    def __init__(self, callback: Callable[[], None], start_error: Exception | None = None) -> None:
        self._callback = callback
        self._start_error = start_error
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        # Deliberately ignores ``cancelled``: this models a timer thread that
        # passed its cancel check before the cancel arrived.
        self._callback()


class _Rig:
    """A watcher state wired to manual timers, plus what it enqueued."""

    def __init__(self, vault: Path, on_event: Callable[[str, Path], None] | None = None) -> None:
        self.timers: list[_ManualTimer] = []
        self.events: list[tuple[str, Path]] = []
        #: 1-based factory call that raises ``factory_error`` (thread exhaustion).
        self.factory_fails_on: int | None = None
        self.factory_error = RuntimeError("can't start new thread")
        #: When set, every timer built raises this from ``start()``.
        self.start_error: Exception | None = None
        self._factory_calls = 0
        self.state = _WatcherState(timer_factory=self._make_timer)

        def _record(action: str, path: Path) -> None:
            self.events.append((action, path))
            if on_event is not None:
                on_event(action, path)

        self.config = WatchConfig(vault_path=vault, debounce_ms=10, on_event=_record)

    def _make_timer(self, _delay: float, callback: Callable[[], None]) -> _ManualTimer:
        self._factory_calls += 1
        if self._factory_calls == self.factory_fails_on:
            raise self.factory_error
        timer = _ManualTimer(callback, self.start_error)
        self.timers.append(timer)
        return timer

    def schedule(
        self, path: Path, action: Any = "upsert", dest: Path | None = None
    ) -> _ManualTimer:
        _schedule_debounced(self.config, self.state, action, path, dest)
        return self.timers[-1]

    def pending(self) -> dict[Path, Any]:
        """Path -> its armed timer (each entry also carries its pending job)."""
        with self.state.lock:
            return {path: timer for path, (timer, _job) in self.state.pending_timers.items()}

    def queued(self) -> list[_Job | None]:
        """Everything on the job queue, in order (consumes it)."""
        items: list[_Job | None] = []
        while True:
            try:
                items.append(self.state.jobs.get_nowait())
            except queue.Empty:
                return items

    def shut_down(self) -> None:
        """What ``run_watcher``'s ``finally`` does: drain, then the sentinel."""
        _drain_pending_timers(self.state, self.config)
        self.state.jobs.put(None)


def _race(rig: _Rig, note: Path) -> tuple[_ManualTimer, _ManualTimer]:
    """Replay the race: T1 starts firing, T2 supersedes it, T1's ``_fire`` runs."""
    t1 = rig.schedule(note)  # armed; its interval elapses and Timer.run enters _fire
    t2 = rig.schedule(note)  # same path: pops T1, cancels it (too late), arms T2
    assert t1.cancelled and t2.started, "fixture: T2 must supersede T1"
    t1.fire()  # T1's already-running _fire now takes the lock
    return t1, t2


def test_superseded_fire_leaves_successor_pending(tmp_path: Path) -> None:
    """A superseded ``_fire`` must neither steal T2's entry nor enqueue a job.

    The burst is still owed exactly one job, and T2 delivers it. This is the
    last-wins outcome a cancel that arrives in time already had.
    """
    note = tmp_path / "burst.md"
    rig = _Rig(tmp_path)

    _t1, t2 = _race(rig, note)

    assert rig.pending() == {note: t2}, (
        "the superseded timer's _fire popped its successor's pending entry"
    )
    assert rig.events == [], f"the superseded timer enqueued a job: {rig.events!r}"

    t2.fire()
    assert rig.events == [("upsert", note)]
    assert rig.queued() == [_Job(action="upsert", abs_path=note)]


def test_raced_successor_is_drained_before_sentinel(tmp_path: Path) -> None:
    """After the race, shutdown must still deliver T2's edit before the sentinel.

    At ``a6a76fe``, T2 was orphaned, and the drain could not see it. Firing it
    late, after the worker's queue was closed, put the edit behind ``None``,
    where no worker reads it.
    """
    note = tmp_path / "burst.md"
    rig = _Rig(tmp_path)
    _t1, t2 = _race(rig, note)

    rig.shut_down()
    t2.fire()  # the orphan's late fire, had the drain missed it

    jobs = rig.queued()
    assert jobs[-1] is None, f"a job landed behind the shutdown sentinel: {jobs!r}"
    assert jobs == [_Job(action="upsert", abs_path=note), None]


def test_fire_enqueues_before_a_racing_drain_closes_queue(tmp_path: Path) -> None:
    """A drain cannot run between ``_fire``'s pop and its put.

    The ``on_event`` hook runs immediately before ``_fire``'s ``jobs.put``. If
    ``state.lock`` is free at that moment, a shutdown drain on the main thread
    could take it right then: it would see nothing pending and close the
    queue before the job lands. The hook replays exactly that drain whenever
    the lock is free.
    """
    note = tmp_path / "note.md"
    drained_early: list[bool] = []
    rig: _Rig

    def _drain_if_lock_free(_action: str, _path: Path) -> None:
        if drained_early or not rig.state.lock.acquire(blocking=False):
            return
        rig.state.lock.release()
        drained_early.append(True)
        rig.shut_down()

    rig = _Rig(tmp_path, on_event=_drain_if_lock_free)
    rig.schedule(note).fire()
    if not drained_early:
        rig.shut_down()

    jobs = rig.queued()
    assert jobs[-1] is None, (
        f"a drain ran between _fire's pop and its put, so the job landed behind "
        f"the shutdown sentinel: {jobs!r}"
    )
    assert jobs == [_Job(action="upsert", abs_path=note), None]


def test_fire_that_lost_to_overflow_adds_nothing(tmp_path: Path, mocker: Any) -> None:
    """An UPSERT ``_fire`` that lost to the overflow flush is covered by the full sync.

    Overflow cancels and clears every pending timer under the same lock. It
    enqueues each flushed delete/move job itself (tests below), then one
    full-vault upsert. A timer already inside ``_fire`` finds its entry gone
    and must add nothing. For an upsert, the full sync re-reads the whole
    vault, so no event is lost.
    """
    mocker.patch("brain.vault.watch._MAX_PENDING", 1)
    rig = _Rig(tmp_path)
    t_a = rig.schedule(tmp_path / "a.md")
    rig.schedule(tmp_path / "b.md")  # overflow: flush every timer, queue a full sync
    assert t_a.cancelled and rig.pending() == {}, "fixture: overflow must flush T_a"

    t_a.fire()  # T_a's already-running _fire

    assert rig.queued() == [_Job(action="upsert", abs_path=tmp_path)], (
        "a fire that lost to the overflow flush still enqueued its own job"
    )


# ---------------------------------------------------------------------------
# The drain delivers the REAL pending job (PR #49 audit, 2026-10-22).
#
# Before this, ``pending_timers`` kept only the timer, so the shutdown drain
# enqueued an ``upsert`` for every pending path. A pending ``delete`` became a
# stale upsert, which the worker skips, and the row survived. A pending
# ``move`` upserted its vanished source, and the destination was never synced.
# The identity check made this worse for a ``delete`` whose ``_fire`` loses
# the lock to the drain: at ``a6a76fe`` that late fire still enqueued its
# delete, and now it returns, so the drain's job has to be the right one.
# ---------------------------------------------------------------------------


def test_drain_enqueues_a_pending_delete_as_delete(tmp_path: Path) -> None:
    """A ``delete`` still pending at shutdown is drained as a ``delete``."""
    gone = tmp_path / "deleted.md"
    rig = _Rig(tmp_path)
    rig.schedule(gone, "delete")

    rig.shut_down()

    assert rig.queued() == [_Job(action="delete", abs_path=gone), None], (
        "the drain did not enqueue the pending delete as a delete"
    )


def test_drain_enqueues_a_pending_move_with_its_destination(tmp_path: Path) -> None:
    """A ``move`` still pending at shutdown is drained as that ``move``, dest included."""
    src = tmp_path / "old.md"
    dst = tmp_path / "new.md"
    rig = _Rig(tmp_path)
    rig.schedule(src, "move", dst)

    rig.shut_down()

    assert rig.queued() == [_Job(action="move", abs_path=src, dest_path=dst), None], (
        "the drain did not enqueue the pending move with its destination"
    )


def test_delete_fire_that_loses_to_the_drain_is_not_lost(tmp_path: Path) -> None:
    """The auditor's replay: a ``delete`` whose ``_fire`` lost the lock to the drain.

    The losing fire returns, because the drain owns the path's event now. So
    the drain must have enqueued the delete itself, before the sentinel, and
    nothing may follow the sentinel.
    """
    gone = tmp_path / "deleted.md"
    rig = _Rig(tmp_path)
    timer = rig.schedule(gone, "delete")

    rig.shut_down()  # the drain wins the lock: cancel (too late), enqueue, sentinel
    timer.fire()  # the delete's already-running _fire now gets the lock

    jobs = rig.queued()
    assert jobs[-1] is None, f"a job landed behind the shutdown sentinel: {jobs!r}"
    assert jobs == [_Job(action="delete", abs_path=gone), None], (
        f"the delete that lost to the drain was not delivered as a delete: {jobs!r}"
    )


# ---------------------------------------------------------------------------
# The overflow flush keeps deletes and moves (PR #49 review, 2026-10-22).
#
# The full-vault job the flush enqueues covers UPSERTS: the worker runs
# ``sync_vault(prune=False)``, which re-reads every file. That sync can neither
# remove a deleted file's row nor apply a move in place: it would walk the
# destination as a new file. So the flush enqueues every flushed entry's
# non-upsert job itself, and the current call's job too, BEFORE the full sync,
# so a move updates ``vault_path`` before the sync walks the destination.
# ---------------------------------------------------------------------------


def _full_sync(vault: Path) -> _Job:
    return _Job(action="upsert", abs_path=vault)


def test_overflow_flush_enqueues_a_pending_delete_before_the_full_sync(
    tmp_path: Path, mocker: Any
) -> None:
    """A pending ``delete`` flushed by overflow is enqueued, ahead of the full sync."""
    mocker.patch("brain.vault.watch._MAX_PENDING", 1)
    gone = tmp_path / "gone.md"
    rig = _Rig(tmp_path)
    rig.schedule(gone, "delete")

    rig.schedule(tmp_path / "other.md")  # overflow: flush gone.md's entry

    assert rig.queued() == [_Job(action="delete", abs_path=gone), _full_sync(tmp_path)], (
        "the overflow flush dropped a pending delete, or queued it after the full sync"
    )


def test_delete_fire_that_loses_to_the_overflow_flush_is_not_lost(
    tmp_path: Path, mocker: Any
) -> None:
    """The reviewer's replay: a ``delete`` whose ``_fire`` lost the lock to the flush.

    At ``a6a76fe`` the late fire still enqueued its delete. With the identity
    check it returns, so the flush must already have enqueued the delete, once.
    """
    mocker.patch("brain.vault.watch._MAX_PENDING", 1)
    gone = tmp_path / "gone.md"
    rig = _Rig(tmp_path)
    timer = rig.schedule(gone, "delete")
    rig.schedule(tmp_path / "other.md")  # overflow wins the lock

    timer.fire()  # the delete's already-running _fire

    assert rig.queued() == [_Job(action="delete", abs_path=gone), _full_sync(tmp_path)], (
        "the delete that lost to the overflow flush was dropped or delivered twice"
    )


def test_overflow_flush_enqueues_a_pending_move_with_its_destination(
    tmp_path: Path, mocker: Any
) -> None:
    """A pending ``move`` flushed by overflow keeps its dest, ahead of the full sync."""
    mocker.patch("brain.vault.watch._MAX_PENDING", 1)
    src = tmp_path / "old.md"
    dst = tmp_path / "new.md"
    rig = _Rig(tmp_path)
    rig.schedule(src, "move", dst)

    rig.schedule(tmp_path / "other.md")  # overflow

    assert rig.queued() == [
        _Job(action="move", abs_path=src, dest_path=dst),
        _full_sync(tmp_path),
    ], "the overflow flush dropped a pending move, or queued it after the full sync"


def test_overflowing_call_that_is_a_delete_is_enqueued(tmp_path: Path, mocker: Any) -> None:
    """The call that trips the overflow is never armed, so its own delete must be queued."""
    mocker.patch("brain.vault.watch._MAX_PENDING", 1)
    gone = tmp_path / "gone.md"
    rig = _Rig(tmp_path)
    rig.schedule(tmp_path / "a.md")  # an upsert: the full sync covers it
    timers_before = len(rig.timers)

    _schedule_debounced(rig.config, rig.state, "delete", gone)  # overflows

    assert rig.queued() == [_Job(action="delete", abs_path=gone), _full_sync(tmp_path)], (
        "the overflowing call's own delete was dropped"
    )
    assert len(rig.timers) == timers_before, "the overflowing call built a debounce timer"


def test_overflowing_call_whose_timer_cannot_start_still_flushes(
    tmp_path: Path, mocker: Any
) -> None:
    """The overflow path must not depend on a timer: it builds and starts none.

    Were it to build one, a ``start()`` failure under thread exhaustion would
    take the direct-delivery fallback and return BEFORE the flush: no full
    sync would be queued, and the flushed delete would stay pending.
    """
    mocker.patch("brain.vault.watch._MAX_PENDING", 1)
    gone = tmp_path / "gone.md"
    rig = _Rig(tmp_path)
    rig.schedule(gone, "delete")
    rig.start_error = RuntimeError("can't start new thread")

    rig.schedule(tmp_path / "other.md")  # overflows

    assert rig.queued() == [_Job(action="delete", abs_path=gone), _full_sync(tmp_path)], (
        "the overflow flush was skipped when a timer could not start"
    )
    assert rig.pending() == {}, "a flushed entry was left pending"


# ---------------------------------------------------------------------------
# A scheduling failure loses nothing and kills nothing (PR #49 review of 8341ea1).
#
# ``_schedule_debounced`` used to pop and cancel the existing entry BEFORE it
# built the new timer. A raising factory (``RuntimeError: can't start new
# thread`` under thread exhaustion) therefore dropped the existing entry's job,
# and a racing ``_fire`` returned on the identity check. The exception also
# escaped ``_Handler.on_any_event`` into watchdog's observer thread, which
# wrapped only classification in its try/except.
# ---------------------------------------------------------------------------


def test_raising_timer_factory_leaves_the_existing_entry_pending(tmp_path: Path) -> None:
    """A factory that raises on the 2nd call must not cost P its pending delete."""
    note = tmp_path / "p.md"
    rig = _Rig(tmp_path)
    rig.factory_fails_on = 2
    original = rig.schedule(note, "delete")

    with pytest.raises(RuntimeError, match="can't start new thread"):
        rig.schedule(note)  # the factory raises

    assert rig.pending() == {note: original}, (
        "a raising timer factory dropped the existing pending entry"
    )
    rig.shut_down()
    assert rig.queued() == [_Job(action="delete", abs_path=note), None]


def _schedule_expecting_no_raise(rig: _Rig, path: Path, action: Any) -> None:
    # The assertion sits outside the ``except`` so a failure reports it, not a
    # chained traceback of the original exception.
    raised: RuntimeError | None = None
    try:
        rig.schedule(path, action)
    except RuntimeError as exc:
        raised = exc
    assert raised is None, "a failing start() raised instead of delivering the event"


def test_start_failure_delivers_a_delete_that_supersedes_a_pending_upsert(
    tmp_path: Path,
) -> None:
    """``start()`` raising "can't start new thread" delivers the NEW event now.

    Constructing a ``threading.Timer`` creates no OS thread; ``start()`` does.
    Without a timer there is no debounce, so the newest event wins at once,
    exactly as debounce would have let it: the pending upsert is cancelled and
    the delete is enqueued directly, once, with nothing left pending.
    """
    note = tmp_path / "p.md"
    rig = _Rig(tmp_path)
    pending_upsert = rig.schedule(note)
    rig.start_error = RuntimeError("can't start new thread")

    _schedule_expecting_no_raise(rig, note, "delete")

    assert rig.queued() == [_Job(action="delete", abs_path=note)], (
        "the delete was not delivered exactly once when its timer could not start"
    )
    assert rig.pending() == {}, "an entry was left pending after a start failure"
    assert pending_upsert.cancelled, "the superseded upsert's timer was left armed"


def test_start_failure_on_a_fresh_path_delivers_the_event_now(tmp_path: Path) -> None:
    """With nothing pending, a ``start()`` failure still delivers the event, once."""
    note = tmp_path / "p.md"
    rig = _Rig(tmp_path)
    rig.start_error = RuntimeError("can't start new thread")

    _schedule_expecting_no_raise(rig, note, "delete")

    assert rig.queued() == [_Job(action="delete", abs_path=note)], (
        "the event was not delivered exactly once when its timer could not start"
    )
    assert rig.pending() == {}, "an entry was left pending after a start failure"


def test_handler_survives_a_scheduling_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A scheduling exception must not escape into watchdog's observer thread.

    An escape can kill that thread, which silently stops every later event.
    The handler logs and keeps going, like its classification guard does. The
    trigger is a raising timer factory, which ``_schedule_debounced`` lets
    propagate (only a failing ``start()`` falls back to direct delivery).
    """
    rig = _Rig(tmp_path)
    rig.factory_fails_on = 1
    handler = _Handler(config=rig.config, state=rig.state)
    first = tmp_path / "a.md"
    later = tmp_path / "b.md"

    with caplog.at_level(logging.ERROR, logger="brain.vault.watch"):
        escaped: RuntimeError | None = None
        try:
            handler.on_any_event(FileModifiedEvent(str(first)))
        except RuntimeError as exc:
            escaped = exc
        handler.on_any_event(FileModifiedEvent(str(later)))

    assert escaped is None, "a scheduling failure escaped into the observer thread"

    assert set(rig.pending()) == {later}, "a later event was not scheduled"
    assert any("schedule failed" in r.getMessage() for r in caplog.records), (
        "the scheduling failure was not logged"
    )


# ---------------------------------------------------------------------------
# (A) No event after shutdown. Plan:
# docs/plans/2026-10-22-vault-watcher-shutdown-mixed-actions.md.
#
# An observer thread that outlives ``observer.join(5.0)`` could arm a timer
# after the drain, and its job landed behind the sentinel, where no worker
# reads it. The drain now closes the watcher. A closed watcher arms nothing
# and enqueues nothing; the next startup ``sync_vault`` reconciles the path.
# ---------------------------------------------------------------------------


def test_event_after_shutdown_arms_no_timer_and_enqueues_nothing(tmp_path: Path) -> None:
    """After the drain, a new event builds no timer and lands nothing behind ``None``."""
    rig = _Rig(tmp_path)
    rig.shut_down()
    timers_before = len(rig.timers)

    _schedule_debounced(rig.config, rig.state, "delete", tmp_path / "late.md")

    assert len(rig.timers) == timers_before, "a closed watcher armed a debounce timer"
    assert rig.pending() == {}, "a closed watcher registered a pending entry"
    assert rig.queued() == [None], "a closed watcher enqueued behind the sentinel"


def test_events_after_shutdown_are_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dropped post-shutdown events are logged once, not once per event."""
    rig = _Rig(tmp_path)
    rig.shut_down()

    with caplog.at_level(logging.WARNING, logger="brain.vault.watch"):
        _schedule_debounced(rig.config, rig.state, "upsert", tmp_path / "a.md")
        _schedule_debounced(rig.config, rig.state, "upsert", tmp_path / "b.md")

    drops = [r for r in caplog.records if "after shutdown" in r.getMessage()]
    assert len(drops) == 1, f"expected one drop log, got {len(drops)}"


def test_timer_fired_after_shutdown_enqueues_nothing(tmp_path: Path) -> None:
    """A timer armed before the drain that fires after it adds nothing.

    The drain clears ``pending_timers`` and a closed watcher never refills
    it, so every surviving ``_fire`` fails its identity check.
    """
    note = tmp_path / "p.md"
    rig = _Rig(tmp_path)
    timer = rig.schedule(note)
    rig.shut_down()

    timer.fire()
    _schedule_debounced(rig.config, rig.state, "upsert", note)  # refused: closed
    for survivor in rig.timers:  # every timer ever built fires late
        survivor.fire()

    assert rig.queued() == [_Job(action="upsert", abs_path=note), None], (
        "a timer fired after shutdown enqueued behind the sentinel"
    )


# ---------------------------------------------------------------------------
# (B) A pending MOVE is never silently replaced by a different job.
#
# Debounce is keyed by path and the last event wins. For a move (keyed on its
# src) that lost the in-place rename: a move from P, then P recreated within
# debounce_ms, became an upsert of P. A pending move superseded by a DIFFERENT
# job is now enqueued first, under the lock. Every other pair still coalesces;
# see the action-pair table in the plan.
# ---------------------------------------------------------------------------


def test_move_superseded_by_upsert_is_delivered_first(tmp_path: Path) -> None:
    src, dst = tmp_path / "p.md", tmp_path / "d.md"
    rig = _Rig(tmp_path)
    rig.schedule(src, "move", dst)

    upsert = rig.schedule(src)  # P recreated within the debounce window

    assert rig.queued() == [_Job(action="move", abs_path=src, dest_path=dst)], (
        "the pending move was dropped when an upsert of its src superseded it"
    )
    assert rig.pending() == {src: upsert}
    upsert.fire()
    assert rig.queued() == [_Job(action="upsert", abs_path=src)]


def test_move_superseded_by_delete_is_delivered_first(tmp_path: Path) -> None:
    src, dst = tmp_path / "p.md", tmp_path / "d.md"
    rig = _Rig(tmp_path)
    rig.schedule(src, "move", dst)

    newest = rig.schedule(src, "delete")

    assert rig.queued() == [_Job(action="move", abs_path=src, dest_path=dst)], (
        "the pending move was dropped when a delete of its src superseded it"
    )
    assert rig.pending() == {src: newest}
    newest.fire()
    assert rig.queued() == [_Job(action="delete", abs_path=src)], (
        "the successor did not deliver the NEW job (the delete)"
    )


def test_move_superseded_by_a_move_elsewhere_is_delivered_first(tmp_path: Path) -> None:
    src, first, second = tmp_path / "p.md", tmp_path / "d1.md", tmp_path / "d2.md"
    rig = _Rig(tmp_path)
    rig.schedule(src, "move", first)

    newest = rig.schedule(src, "move", second)

    assert rig.queued() == [_Job(action="move", abs_path=src, dest_path=first)], (
        "the first move was dropped when a move of the same src to another dest superseded it"
    )
    newest.fire()
    assert rig.queued() == [_Job(action="move", abs_path=src, dest_path=second)], (
        "the successor did not deliver the NEW job (the move to the second dest)"
    )


def test_duplicate_move_still_coalesces(tmp_path: Path) -> None:
    """Two reports of the same rename stay ONE job: same-action bursts coalesce."""
    src, dst = tmp_path / "p.md", tmp_path / "d.md"
    rig = _Rig(tmp_path)
    rig.schedule(src, "move", dst)

    again = rig.schedule(src, "move", dst)

    assert rig.queued() == [], "a duplicate move report was delivered twice"
    again.fire()
    assert rig.queued() == [_Job(action="move", abs_path=src, dest_path=dst)]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("upsert", "upsert"),
        ("upsert", "delete"),
        ("upsert", "move"),
        ("delete", "delete"),
        ("delete", "upsert"),
        ("delete", "move"),
    ],
)
def test_non_move_pending_jobs_still_coalesce(tmp_path: Path, first: str, second: str) -> None:
    """A pending upsert or delete is superseded silently: only the newest job is delivered."""
    path, dst = tmp_path / "p.md", tmp_path / "d.md"
    rig = _Rig(tmp_path)
    rig.schedule(path, first, dst if first == "move" else None)

    newest = rig.schedule(path, second, dst if second == "move" else None)

    assert rig.queued() == [], f"a pending {first} was delivered when a {second} superseded it"
    newest.fire()
    assert rig.queued() == [
        _Job(action=second, abs_path=path, dest_path=dst if second == "move" else None)
    ]


def test_atomic_save_delete_then_create_collapses_to_one_upsert(tmp_path: Path) -> None:
    """An editor's atomic save (delete + create of P) must stay ONE upsert.

    Delivering the delete first would DELETE the row, cascading away its
    incoming backlinks, before re-inserting it.
    """
    note = tmp_path / "p.md"
    rig = _Rig(tmp_path)
    rig.schedule(note, "delete")

    created = rig.schedule(note)

    assert rig.queued() == [], "the atomic save's delete was delivered"
    created.fire()
    assert rig.queued() == [_Job(action="upsert", abs_path=note)]


def test_move_superseded_when_the_timer_cannot_start_is_delivered_first(
    tmp_path: Path,
) -> None:
    """The start() fallback delivers the pending move, THEN the new job."""
    src, dst = tmp_path / "p.md", tmp_path / "d.md"
    rig = _Rig(tmp_path)
    rig.schedule(src, "move", dst)
    rig.start_error = RuntimeError("can't start new thread")

    _schedule_expecting_no_raise(rig, src, "upsert")

    assert rig.queued() == [
        _Job(action="move", abs_path=src, dest_path=dst),
        _Job(action="upsert", abs_path=src),
    ], "the fallback dropped the pending move, or delivered it out of order"
    assert rig.pending() == {}
