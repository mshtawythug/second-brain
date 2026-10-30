"""A job past its retention is gone from EVERY read, not only the list (RM-18a, D3).

Eviction used to run only in ``submit()`` and ``list_recent()``, so a finished job
stayed readable by id for as long as nobody submitted or listed. Ask jobs are never
listed (A10), so ``GET /api/jobs/{id}`` would have served a 10-minute result
indefinitely. Split from ``tests/test_ui_jobs.py``, which is at the 800-line
test-file precedent.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.ui.jobs import JobHandle, JobRegistry, JobUnknown


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _registry(clock: _Clock) -> JobRegistry:
    return JobRegistry(
        clock=clock,
        max_queued=4,
        retain=50,
        retain_for=timedelta(hours=1),
        retain_for_by_kind={"ask": timedelta(minutes=10)},
    )


def _returns(value: Mapping[str, Any]) -> Any:
    def runner(handle: JobHandle) -> Mapping[str, Any]:
        return value

    return runner


def test_get_evicts_a_job_past_its_retention_without_a_list_or_submit() -> None:
    clock = _Clock()
    reg = _registry(clock)
    ask = reg.submit("ask", _returns({"answer": "a"}))
    ingest = reg.submit("ingest.paste", _returns({}))
    reg.run_next()
    reg.run_next()

    clock.now += timedelta(minutes=9)
    assert reg.get(ask.id).state == "succeeded"
    clock.now += timedelta(minutes=2)

    with pytest.raises(JobUnknown):
        reg.get(ask.id)
    assert reg.get(ingest.id).state == "succeeded"  # its own, longer retention


def test_cancel_evicts_a_job_past_its_retention() -> None:
    clock = _Clock()
    reg = _registry(clock)
    ask = reg.submit("ask", _returns({}))
    reg.run_next()

    clock.now += timedelta(minutes=11)

    with pytest.raises(JobUnknown):
        reg.cancel(ask.id)
