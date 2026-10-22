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

import queue
from collections.abc import Callable
from pathlib import Path
from typing import Any

from brain.vault.watch import (
    WatchConfig,
    _drain_pending_timers,
    _Job,
    _schedule_debounced,
    _WatcherState,
)


class _ManualTimer:
    """A debounce timer the test fires by hand."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self.started = False
        self.cancelled = False

    def start(self) -> None:
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
        self.state = _WatcherState(timer_factory=self._make_timer)

        def _record(action: str, path: Path) -> None:
            self.events.append((action, path))
            if on_event is not None:
                on_event(action, path)

        self.config = WatchConfig(vault_path=vault, debounce_ms=10, on_event=_record)

    def _make_timer(self, _delay: float, callback: Callable[[], None]) -> _ManualTimer:
        timer = _ManualTimer(callback)
        self.timers.append(timer)
        return timer

    def schedule(self, path: Path) -> _ManualTimer:
        _schedule_debounced(self.config, self.state, "upsert", path)
        return self.timers[-1]

    def pending(self) -> dict[Path, Any]:
        with self.state.lock:
            return dict(self.state.pending_timers)

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
    """A ``_fire`` that lost to the overflow flush is covered by the full sync.

    Overflow cancels and clears every pending timer and enqueues one
    full-vault upsert under the same lock. A timer already inside ``_fire``
    finds its entry gone and must add nothing: the full sync re-reads the
    whole vault, so no event is lost.
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
