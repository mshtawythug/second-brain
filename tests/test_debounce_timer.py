"""Tests for ``brain.vault.debounce_timer``, the vault watcher's timer seam."""

from __future__ import annotations

import threading

from brain.vault.debounce_timer import daemon_timer


def test_daemon_timer_builds_an_unstarted_daemon_threading_timer() -> None:
    """Production's factory returns a real, unstarted, DAEMON ``threading.Timer``.

    A daemon, so a pending debounce never holds the process open at exit. And
    unstarted, because ``_schedule_debounced`` registers the timer before it
    starts it.
    """
    calls: list[str] = []

    timer = daemon_timer(60, lambda: calls.append("fired"))

    assert isinstance(timer, threading.Timer)
    assert timer.daemon is True, "a pending debounce timer would hold the process open"
    assert not timer.is_alive(), "the factory must not start the timer"
    assert calls == []
