"""A manual debounce timer for ``brain.wiki.build_watcher`` tests.

Passed as ``timer_factory=`` to ``run_watcher`` or ``_Handler``, it records
every timer the debouncer makes and fires one only when the test says so, so
a debounce test never races the wall clock. Not a ``test_*`` module, so
pytest does not collect it.
"""
from __future__ import annotations

from collections.abc import Callable


class ManualTimer:
    """One debounce timer: started, cancelled, or fired by the test."""

    def __init__(self, interval: float, fire: Callable[[], None]) -> None:
        self.interval = interval
        self._fire = fire
        self.started = False
        self.cancelled = False
        self.fired = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        """Run the callback as ``threading.Timer`` would when its interval ends.
        A cancelled or unstarted timer never fires, and none fires twice, as
        with the real one."""
        assert self.started and not self.cancelled and not self.fired, (
            "this timer would not fire"
        )
        self.fired = True
        self._fire()


class ManualClock:
    """A ``timer_factory`` that keeps every timer it makes, in order."""

    def __init__(self) -> None:
        self.timers: list[ManualTimer] = []

    def __call__(self, interval: float, fire: Callable[[], None]) -> ManualTimer:
        timer = ManualTimer(interval, fire)
        self.timers.append(timer)
        return timer

    def live(self) -> list[ManualTimer]:
        """Timers started, not cancelled and not yet fired: the ones that
        would still fire."""
        return [timer for timer in self.timers
                if timer.started and not timer.cancelled and not timer.fired]
