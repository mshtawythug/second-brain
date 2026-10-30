"""``JobRegistry.submit(..., on_settled=)``: a job's cleanup runs exactly once (RM-13).

A sibling of ``tests/test_ui_jobs.py``, which is at the 800-line test-file
precedent. An uploaded file's job owns a staged file, and a runner's own ``finally``
cannot delete it when the job never runs: a queued job cancelled with a 200, or
dropped by ``stop()``. So the registry calls ``on_settled`` on EVERY terminal
transition, outside its lock, and a raising callback never kills the worker.

Pure tier, driven through ``run_next()`` on the test thread.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.ui.jobs import JobHandle, JobRegistry

pytestmark = pytest.mark.nodb


def _registry() -> JobRegistry:
    return JobRegistry(
        clock=lambda: datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
        max_queued=4,
        retain=50,
        retain_for=timedelta(hours=1),
    )


class _Recorder:
    def __init__(self, registry: JobRegistry) -> None:
        self.registry = registry
        self.calls = 0
        self.lock_free: list[bool] = []

    def __call__(self) -> None:
        self.calls += 1
        # Outside the lock: a callback that touches the registry must not deadlock.
        acquired = self.registry._lock.acquire(blocking=False)
        self.lock_free.append(acquired)
        if acquired:
            self.registry._lock.release()


def _ok(handle: JobHandle) -> Mapping[str, Any]:
    return {"ok": True}


def _boom(handle: JobHandle) -> Mapping[str, Any]:
    raise RuntimeError("synthetic failure")


def test_on_settled_runs_once_after_success() -> None:
    registry = _registry()
    hook = _Recorder(registry)
    job = registry.submit("test.kind", _ok, on_settled=hook)
    assert hook.calls == 0

    registry.run_next()

    assert registry.get(job.id).state == "succeeded"
    assert hook.calls == 1 and hook.lock_free == [True]


def test_on_settled_runs_once_after_failure() -> None:
    registry = _registry()
    hook = _Recorder(registry)
    job = registry.submit("test.kind", _boom, on_settled=hook)

    registry.run_next()

    assert registry.get(job.id).state == "failed"
    assert hook.calls == 1 and hook.lock_free == [True]


def test_on_settled_runs_once_when_a_running_job_is_cancelled() -> None:
    registry = _registry()
    hook = _Recorder(registry)

    def cancels_itself(handle: JobHandle) -> Mapping[str, Any]:
        registry.cancel(handle.job_id)
        handle.check_cancelled()
        raise AssertionError("not reached")

    job = registry.submit("test.kind", cancels_itself, on_settled=hook)
    registry.run_next()

    assert registry.get(job.id).state == "cancelled"
    assert hook.calls == 1


def test_on_settled_runs_when_a_queued_job_is_cancelled_and_never_runs() -> None:
    registry = _registry()
    hook = _Recorder(registry)
    job = registry.submit("test.kind", _ok, on_settled=hook)

    registry.cancel(job.id)
    registry.cancel(job.id)  # idempotent: still once

    assert registry.get(job.id).state == "cancelled"
    assert hook.calls == 1 and hook.lock_free == [True]
    assert registry.run_next() is False


def test_on_settled_runs_for_every_queued_job_stop_drops() -> None:
    registry = _registry()
    hooks = [_Recorder(registry) for _ in range(3)]
    for hook in hooks:
        registry.submit("test.kind", _ok, on_settled=hook)

    assert registry.stop(timeout=0.1) == []

    assert [h.calls for h in hooks] == [1, 1, 1]
    assert all(h.lock_free == [True] for h in hooks)


def test_on_settled_runs_after_cancel_too_late() -> None:
    registry = _registry()
    hook = _Recorder(registry)

    def ignores_cancel(handle: JobHandle) -> Mapping[str, Any]:
        registry.cancel(handle.job_id)
        return {"ok": True}

    job = registry.submit("test.kind", ignores_cancel, on_settled=hook)
    registry.run_next()

    assert registry.get(job.id).state == "succeeded"
    assert hook.calls == 1


def test_a_raising_callback_is_logged_by_class_and_does_not_break_the_registry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _registry()

    def bad_hook() -> None:
        raise OSError("synthetic secret path /x/y")

    first = registry.submit("test.kind", _ok, on_settled=bad_hook)
    second = registry.submit("test.kind", _ok)
    with caplog.at_level(logging.WARNING, logger="brain.ui.jobs"):
        registry.run_next()
        registry.run_next()

    assert registry.get(first.id).state == "succeeded"
    assert registry.get(second.id).state == "succeeded"
    assert "OSError" in caplog.text and "/x/y" not in caplog.text


def test_without_on_settled_nothing_changes() -> None:
    registry = _registry()
    job = registry.submit("test.kind", _ok)

    registry.run_next()

    assert registry.get(job.id).state == "succeeded"
