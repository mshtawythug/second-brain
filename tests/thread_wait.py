"""A shared backstop wait for tests that poll for work done on another thread.

Importable as ``from tests.thread_wait import wait_for``. Not a ``test_*``
module, so pytest does not collect it. One definition serves the vault-watcher
and ``brain vault sync --watch`` tests (and served the wiki build-watcher
tests until R3 deleted them), and
``tests/test_thread_wait.py`` guards every importer: no call site may pass a
timeout, and no test module may define or re-export its own copy.
"""
from __future__ import annotations

import time
from collections.abc import Callable

#: A BACKSTOP against a hung thread, NOT a latency assertion. Do not lower it,
#: and do not pass a shorter ``timeout`` at a call site.
WAIT_FOR_BACKSTOP_S = 10.0

_POLL_INTERVAL_S = 0.01


def wait_for(predicate: Callable[[], object], timeout: float = WAIT_FOR_BACKSTOP_S) -> None:
    """Block until ``predicate()`` returns truthy or the deadline passes.

    ``timeout`` is a BACKSTOP against a hung thread, NOT a latency assertion.
    Every call site waits for something that WILL happen on another thread (a
    debounce timer, a watcher's worker sync, a build or refresh thread). A
    passing run returns as soon as the predicate holds and never pays the
    deadline.

    Call sites used to pass 1-3s. Under a full suite at load average 50-140
    that flaked the vault watcher's racing-edit test twice on 2026-10-15, and
    probed at load ~95 a single phase took 1.945s of its 2.0s (PR #31). The
    build-watcher tests carried the same 2.0s budgets until 2026-10-16.
    ``tests/test_thread_wait.py`` refuses a per-site ``timeout``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(
        f"predicate never became true within {timeout}s "
        "(backstop, not a latency assertion)"
    )
