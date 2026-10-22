"""The debounce-timer seam the vault watcher arms one timer per path through.

``brain.vault.watch`` debounces each path with a timer it builds through a
``TimerFactory``. Production uses :func:`daemon_timer` (a daemon
``threading.Timer``). Tests inject a manual timer whose callback they run
on their own thread, which makes the watcher's timer races reproducible
without sleeps: see ``tests/test_vault_watch_fire_race.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol


class DebounceTimer(Protocol):
    """The part of ``threading.Timer`` the watcher uses."""

    def start(self) -> None: ...

    def cancel(self) -> None: ...


#: ``(delay_seconds, callback) -> unstarted timer``.
TimerFactory = Callable[[float, Callable[[], None]], DebounceTimer]


def daemon_timer(delay: float, callback: Callable[[], None]) -> DebounceTimer:
    """Build an unstarted daemon ``threading.Timer``.

    It is a daemon so a pending debounce never holds the process open.
    """
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    return timer
