"""`WatcherProbe`: the TTL cache that bounds how often `brain ui` forks `ps`.

Every tab polls `/api/watcher`, and every window focus asks again, so without a
cache the server would fork `ps` once per request. The probe answers from its
last snapshot for ``ttl_s`` seconds. The clock and the detector are injected, so
these tests count detections against a fake clock rather than sleeping.

No PII: the detector returns fixed strings.
"""
from __future__ import annotations

import threading

import pytest

from brain.doctor_watcher import WatcherState, watcher_state
from brain.ui.watcher_probe import DEFAULT_TTL_S, WatcherProbe

#: Opens no database connection.
pytestmark = pytest.mark.nodb


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Detector:
    """Answers each call with the next state from ``states``, and counts calls."""

    def __init__(self, *states: WatcherState) -> None:
        self.states = list(states)
        self.calls = 0

    def __call__(self) -> WatcherState:
        self.calls += 1
        return self.states[min(self.calls, len(self.states)) - 1]


def test_the_first_call_detects() -> None:
    detect = _Detector("not_running")
    probe = WatcherProbe(detect=detect, clock=_Clock(), ttl_s=15.0)

    assert probe.state() == "not_running"
    assert detect.calls == 1


def test_a_call_inside_the_ttl_reuses_the_snapshot() -> None:
    clock = _Clock()
    detect = _Detector("not_running", "running")
    probe = WatcherProbe(detect=detect, clock=clock, ttl_s=15.0)

    probe.state()
    clock.now += 14.9

    assert probe.state() == "not_running"
    assert detect.calls == 1


def test_a_call_at_the_ttl_detects_again() -> None:
    clock = _Clock()
    detect = _Detector("not_running", "running")
    probe = WatcherProbe(detect=detect, clock=clock, ttl_s=15.0)

    probe.state()
    clock.now += 15.0

    assert probe.state() == "running"
    assert detect.calls == 2


def test_the_default_detector_is_the_doctors_and_the_ttl_is_bounded() -> None:
    """Reuse, not a second rule: the probe asks ``doctor_watcher.watcher_state``."""
    probe = WatcherProbe()

    assert probe.detect is watcher_state
    assert 0 < DEFAULT_TTL_S <= 60


class _CountingLock:
    """A real lock that signals ``second_attempt`` before the 2nd acquire blocks.

    The seam that makes the concurrency test deterministic without sleeping:
    "the second thread has reached the lock" is observable as an attempt,
    whereas "it is blocked inside ``Lock.acquire``" is not observable at all.
    """

    def __init__(self, second_attempt: threading.Event) -> None:
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self._attempts = 0
        self._second_attempt = second_attempt

    def __enter__(self) -> None:
        with self._guard:
            self._attempts += 1
            if self._attempts >= 2:
                self._second_attempt.set()
        self._lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def test_concurrent_requests_at_an_expired_ttl_share_one_detection() -> None:
    """Spec §3.1 bound #1: two tabs asking at once fork ``ps`` ONCE, not twice.

    The first thread is held inside the detector; the second starts only after
    that. The detector is released once the second thread has PROGRESSED, which
    is one of two observable events: it reached the lock (correct code — it
    will wait there), or it entered the detector itself (no serialisation).
    Waiting for either, rather than for the lock alone, is what makes the
    failure land on ``calls == 1`` — the claim — instead of on the harness.
    """
    entered = threading.Event()
    progressed = threading.Event()
    release = threading.Event()
    calls = 0

    def detect() -> WatcherState:
        nonlocal calls
        calls += 1
        entered.set()
        if calls >= 2:
            progressed.set()
        assert release.wait(timeout=10), "the test never released the detector"
        return "not_running"

    probe = WatcherProbe(
        detect=detect, clock=_Clock(), ttl_s=15.0, lock=_CountingLock(progressed)
    )
    results: list[WatcherState] = []
    first = threading.Thread(target=lambda: results.append(probe.state()))
    second = threading.Thread(target=lambda: results.append(probe.state()))

    first.start()
    assert entered.wait(timeout=10)
    second.start()
    assert progressed.wait(timeout=10), "the second request never got going"
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert calls == 1
    assert results == ["not_running", "not_running"]


def test_a_raising_detector_caches_nothing_and_releases_the_lock() -> None:
    """RULING: an exception from ``detect`` propagates and is NOT cached.

    The real detector cannot raise for an environmental reason —
    ``snapshot_processes`` already turns a missing, slow or failing ``ps`` into
    ``[]``, which ``watcher_state`` reports as ``"unavailable"``, and THAT is
    cached like any answer. So an exception here is a bug, and caching a made-up
    answer for it would hide the bug behind a plausible status line. It reaches
    the route as a logged 500, the page keeps its last answer (or says
    "unknown" if it has none), and the retry rate is bounded by the page's own
    cadence. What must hold is that the lock is released, or every later
    request would hang.
    """
    detect = _Raising()
    probe = WatcherProbe(detect=detect, clock=_Clock(), ttl_s=15.0)

    with pytest.raises(_DetectorBug):
        probe.state()
    with pytest.raises(_DetectorBug):
        probe.state()

    assert detect.calls == 2


class _DetectorBug(RuntimeError):
    """A synthetic defect in a detector."""


class _Raising:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> WatcherState:
        self.calls += 1
        raise _DetectorBug("synthetic")
