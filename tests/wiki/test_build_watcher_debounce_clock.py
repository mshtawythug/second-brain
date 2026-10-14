"""The debounce clock seam of ``brain.wiki.build_watcher``: ``timer_factory``.

``test_debounce_coalesces_events`` (tests/wiki/test_build_watcher.py) once
slept 5ms between events against a real 50ms debounce timer. Under load a
sleep overran the window, the burst split into two builds, and the test
failed 6 times in 30 runs at a load average around 55. The timer is now
injected. Here the race that test lost is reproduced ON PURPOSE and
deterministically, and the production default is pinned.

New module rather than more lines in test_build_watcher.py, which is past
800. ``mocker.patch`` on ``build_and_swap`` is pytest-mock with automatic
cleanup, not monkey-patching (CLAUDE.md).
"""
from __future__ import annotations

import inspect
import threading
from pathlib import Path

from pytest_mock import MockerFixture
from watchdog.events import FileModifiedEvent

from brain.wiki.build_swap import BuildResult
from brain.wiki.build_watcher import _daemon_timer, _Handler, _WatcherState, run_watcher
from tests.wiki.manual_timer import ManualClock

_DEBOUNCE_S = 0.05


def _result() -> BuildResult:
    return BuildResult(build_dir=Path("/tmp/builds/x"), build_id="x", elapsed_seconds=0.01,
                       pruned=[], method="output-flag")


def _handler(vault: Path, clock: ManualClock) -> _Handler:
    """A handler on its own state, with the manual clock and a no-op refresh,
    driven directly: no observer and no watcher thread."""
    return _Handler(state=_WatcherState(), vault=vault, quartz_dir=None,
                    debounce_seconds=_DEBOUNCE_S, keep=3, on_build=None,
                    refresh_runner=lambda: None, timer_factory=clock)


def test_two_events_straddling_the_debounce_window_build_twice(
    tmp_path: Path, mocker: MockerFixture,
) -> None:
    """The race the old test lost under load, reproduced deterministically:
    an event, the quiet period ending, then a second event. Those are two
    batches, so two builds.

    That is CORRECT debouncing. The flaky test's bug was only that it
    could not control which side of the window its events landed on."""
    patched = mocker.patch("brain.wiki.build_watcher.build_and_swap", return_value=_result())
    note = tmp_path / "note.md"
    note.write_text("# hi")
    clock = ManualClock()
    handler = _handler(tmp_path, clock)

    handler.on_any_event(FileModifiedEvent(str(note)))
    (first,) = clock.live()
    first.fire()
    handler.on_any_event(FileModifiedEvent(str(note)))
    (second,) = clock.live()
    second.fire()

    assert second is not first
    assert patched.call_count == 2, f"expected 2 builds, got {patched.call_count}"


def test_events_inside_one_window_build_once(tmp_path: Path, mocker: MockerFixture) -> None:
    """The other side of the same line, on the handler alone: events that all
    land before the window ends cancel each earlier timer, and one build runs."""
    patched = mocker.patch("brain.wiki.build_watcher.build_and_swap", return_value=_result())
    note = tmp_path / "note.md"
    note.write_text("# hi")
    clock = ManualClock()
    handler = _handler(tmp_path, clock)

    for _ in range(3):
        handler.on_any_event(FileModifiedEvent(str(note)))
    (live,) = clock.live()
    live.fire()

    assert [timer.cancelled for timer in clock.timers] == [True, True, False]
    assert patched.call_count == 1


def test_the_production_timer_is_a_daemon_threading_timer() -> None:
    """Without a ``timer_factory`` the debouncer gets ``_daemon_timer``: a
    ``threading.Timer`` set to the interval it is given, a daemon (so a pending
    debounce never keeps the process alive on shutdown), and one that fires its
    callback when started.

    The interval is the debounce itself. A factory that built
    ``threading.Timer(0.0, fire)`` would turn off production debouncing while
    every manual-clock test stayed green, because those never build a real
    timer. It is read off the timer, not measured by waiting. So is "not
    started": a thread's ``ident`` is None until ``start()``, whereas checking
    the callback could not tell a started 0.25s timer from an unstarted one
    without waiting past the interval."""
    fired = threading.Event()
    timer = _daemon_timer(0.25, fired.set)

    assert isinstance(timer, threading.Timer)
    assert timer.interval == 0.25
    assert timer.daemon
    assert timer.ident is None, "the factory must not start the timer"
    timer.start()
    assert fired.wait(timeout=5.0), "the started timer never fired"


def test_both_entry_points_default_to_the_production_timer() -> None:
    """``run_watcher`` and ``_Handler`` default ``timer_factory`` to the daemon
    timer, so production behaves as it did before the seam existed."""
    for entry in (run_watcher, _Handler):
        default = inspect.signature(entry).parameters["timer_factory"].default
        assert default is _daemon_timer, f"{entry.__name__} defaults to {default!r}"
