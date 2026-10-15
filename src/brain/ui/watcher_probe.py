"""A TTL-cached answer to "is the vault-sync watcher running?" for `GET /api/watcher`.

Detection is NOT defined here. It is :func:`brain.doctor_watcher.watcher_state`,
the same process scan ``brain doctor`` uses, so there is one rule for what a
watcher looks like. This module only bounds its cost: every open tab polls, and
every window focus asks again, while each detection forks ``ps``. The probe
answers from its last snapshot for ``ttl_s`` seconds, and a lock makes
concurrent requests share one snapshot instead of each forking their own.

Spec: ``docs/specs/2026-10-15-ui-watcher-status-design.md`` §3.1.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager

from ..doctor_watcher import WatcherState, watcher_state

#: How long one ``ps`` snapshot answers for. Short beside the page's 60 s poll,
#: so a watcher that was just started shows up on the next poll.
DEFAULT_TTL_S = 15.0


class WatcherProbe:
    """Answer :meth:`state` from a cached detection, refreshed after ``ttl_s``.

    :param detect: the detector; :func:`~brain.doctor_watcher.watcher_state` by
        default. Injected so tests count calls instead of forking ``ps``.
    :param clock: a monotonic clock in seconds. Injected for the same reason.
    :param ttl_s: how long a detection stays fresh.
    :param lock: serialises detection, so requests that arrive together at an
        expired TTL share ONE ``ps`` fork. A fresh ``threading.Lock`` by
        default; injected so a test can observe that a second request reached
        it, which is what makes the concurrency test deterministic.

    A ``detect`` that RAISES caches nothing and the exception propagates (the
    route answers a logged 500). The real detector cannot raise for an
    environmental reason, because ``snapshot_processes`` already turns a
    missing or failing ``ps`` into ``"unavailable"``, which is cached like any
    answer. An exception is therefore a bug, and caching an invented answer
    would hide it behind a plausible status line. The ``with`` block releases
    the lock either way.
    """

    def __init__(
        self,
        *,
        detect: Callable[[], WatcherState] = watcher_state,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = DEFAULT_TTL_S,
        lock: AbstractContextManager[object] | None = None,
    ) -> None:
        self.detect = detect
        self._clock = clock
        self._ttl_s = ttl_s
        self._lock: AbstractContextManager[object] = (
            lock if lock is not None else threading.Lock()
        )
        self._cached: tuple[float, WatcherState] | None = None

    def state(self) -> WatcherState:
        """The cached state while it is fresh, otherwise a new detection.

        Blocking (it may run ``ps``), so async callers go through a thread pool.
        """
        with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._cached[0] < self._ttl_s:
                return self._cached[1]
            fresh = self.detect()
            self._cached = (now, fresh)
            return fresh
