"""`WatcherProbe`: the TTL cache that bounds how often `brain ui` forks `ps`.

Every tab polls `/api/watcher`, and every window focus asks again, so without a
cache the server would fork `ps` once per request. The probe answers from its
last snapshot for ``ttl_s`` seconds. The clock and the detector are injected, so
these tests count detections against a fake clock rather than sleeping.

No PII: the detector returns fixed strings.
"""
from __future__ import annotations

import pytest

from brain.doctor_watcher import watcher_state
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

    def __init__(self, *states: str) -> None:
        self.states = list(states)
        self.calls = 0

    def __call__(self) -> str:
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
