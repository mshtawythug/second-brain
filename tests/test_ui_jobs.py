"""``brain.ui.jobs.JobRegistry``: the kind-agnostic job subsystem (Ingest spec §4, §9.1).

Pure tier. No database, no server. Every test drives the registry through
``run_next()`` on the test thread, so there are no sleeps and no races. The one
exception is the ``stop()`` timeout case, which starts the worker and waits on a
``threading.Event``, never on a sleep.
"""
from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.errors import BrainError
from brain.ui.jobs import (
    JobCancelled,
    JobError,
    JobHandle,
    JobRegistry,
    JobUnknown,
    NotCancellable,
    QueueFull,
    ShuttingDown,
)

pytestmark = pytest.mark.nodb

_T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def _registry(
    clock: _Clock | None = None,
    *,
    max_queued: int = 4,
    retain: int = 50,
    retain_for: timedelta = timedelta(hours=1),
    retain_for_by_kind: Mapping[str, timedelta] | None = None,
    max_events: int = 200,
) -> JobRegistry:
    return JobRegistry(
        clock=clock or _Clock(),
        max_queued=max_queued,
        retain=retain,
        retain_for=retain_for,
        retain_for_by_kind=retain_for_by_kind,
        max_events=max_events,
    )


def _returns(value: Mapping[str, Any]) -> Any:
    def runner(handle: JobHandle) -> Mapping[str, Any]:
        return value

    return runner


# ---------------------------------------------------------------- lifecycle --


def test_a_submitted_job_is_queued_and_cancellable() -> None:
    reg = _registry()

    snap = reg.submit("test.kind", _returns({"ok": 1}))

    assert snap.state == "queued"
    assert snap.cancellable is True
    assert snap.kind == "test.kind"
    assert snap.created_at == _T0
    assert snap.started_at is None and snap.finished_at is None
    assert [e.phase for e in snap.events] == ["queued"]


def test_run_next_runs_one_job_to_success() -> None:
    clock = _Clock()
    reg = _registry(clock)
    job = reg.submit("k", _returns({"answer": 42}))

    assert reg.run_next() is True

    snap = reg.get(job.id)
    assert snap.state == "succeeded"
    assert dict(snap.result or {}) == {"answer": 42}
    assert snap.error is None
    assert snap.started_at == _T0 and snap.finished_at == _T0


def test_run_next_without_work_returns_false() -> None:
    assert _registry().run_next() is False


def test_run_next_is_fifo() -> None:
    reg = _registry()
    order: list[str] = []

    def runner_for(name: str) -> Any:
        def runner(handle: JobHandle) -> Mapping[str, Any]:
            order.append(name)
            return {}

        return runner

    for name in ("a", "b", "c"):
        reg.submit("k", runner_for(name))
    while reg.run_next():
        pass

    assert order == ["a", "b", "c"]


def test_the_kind_is_a_free_string() -> None:
    """RM-18a registers ``"ask"`` without changing jobs.py."""
    reg = _registry()
    job = reg.submit("ask", _returns({}))
    reg.run_next()
    assert reg.get(job.id).kind == "ask"


def test_a_fifth_queued_job_is_refused() -> None:
    reg = _registry()
    for _ in range(4):
        reg.submit("k", _returns({}))

    with pytest.raises(QueueFull):
        reg.submit("k", _returns({}))


def test_a_running_job_does_not_count_against_the_queue() -> None:
    """One running plus four queued is the full house; the next submit is refused."""
    reg = _registry()
    held: list[Any] = []
    refused: list[BaseException] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        for _ in range(4):
            held.append(reg.submit("k", _returns({})))
        try:
            reg.submit("k", _returns({}))
        except QueueFull as exc:
            refused.append(exc)
        return {}

    reg.submit("k", runner)
    reg.run_next()

    assert len(held) == 4
    assert len(refused) == 1


def test_get_unknown_raises() -> None:
    with pytest.raises(JobUnknown):
        _registry().get("0" * 32)


def test_progress_and_note_append_events() -> None:
    reg = _registry()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        handle.progress("embedding", done=0, total=3)
        handle.progress("embedding", done=3, total=3)
        handle.note("secret_findings")
        return {}

    job = reg.submit("k", runner)
    reg.run_next()

    events = reg.get(job.id).events
    assert [(e.seq, e.phase, e.done, e.total, e.code) for e in events] == [
        (1, "queued", None, None, None),
        (2, "embedding", 0, 3, None),
        (3, "embedding", 3, 3, None),
        (4, "embedding", None, None, "secret_findings"),
    ]


def test_after_slices_the_event_log() -> None:
    reg = _registry()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        for i in range(5):
            handle.progress("embedding", done=i, total=5)
        return {}

    job = reg.submit("k", runner)
    reg.run_next()

    assert [e.seq for e in reg.get(job.id, after=4).events] == [5, 6]
    assert reg.get(job.id, after=6).events == ()


# ----------------------------------------------------------------- failures --


def test_a_raising_runner_fails_with_the_mapped_error() -> None:
    reg = _registry()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        raise ValueError("document text that must never surface")

    job = reg.submit(
        "k", runner, on_error=lambda exc: JobError("bad_thing", "fixed text")
    )
    reg.run_next()

    snap = reg.get(job.id)
    assert snap.state == "failed"
    assert snap.error == JobError("bad_thing", "fixed text")
    assert snap.result is None


def test_without_a_mapper_the_error_names_the_class_only() -> None:
    reg = _registry()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        raise ValueError("document text that must never surface")

    job = reg.submit("k", runner)
    reg.run_next()

    error = reg.get(job.id).error
    assert error is not None
    assert error.code == "internal_error"
    assert "ValueError" in error.message
    assert "document text" not in error.message


def test_a_raising_mapper_falls_back_instead_of_killing_the_worker() -> None:
    reg = _registry()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        raise ValueError("boom")

    def bad_mapper(exc: Exception) -> JobError:
        raise KeyError("mapper bug")

    job = reg.submit("k", runner, on_error=bad_mapper)
    assert reg.run_next() is True

    error = reg.get(job.id).error
    assert error is not None and error.code == "internal_error"


def test_a_failure_is_logged_by_class_only(caplog: pytest.LogCaptureFixture) -> None:
    reg = _registry()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        raise ValueError("document text that must never surface")

    job = reg.submit("ingest.paste", runner)
    with caplog.at_level("WARNING", logger="brain.ui.jobs"):
        reg.run_next()

    assert job.id in caplog.text
    assert "ValueError" in caplog.text
    assert "document text" not in caplog.text


def test_job_cancelled_is_a_brain_error_not_an_embed_error() -> None:
    from brain.errors import EmbedError

    assert issubclass(JobCancelled, BrainError)
    assert not issubclass(JobCancelled, EmbedError)


# ------------------------------------------------------------------- cancel --


def test_cancel_while_queued_removes_the_job_and_it_never_runs() -> None:
    reg = _registry()
    ran: list[bool] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        ran.append(True)
        return {}

    job = reg.submit("k", runner)

    snap = reg.cancel(job.id)

    assert snap.state == "cancelled"
    assert snap.cancellable is False
    assert reg.run_next() is False
    assert ran == []


def test_cancel_while_running_is_honoured_at_the_next_check() -> None:
    reg = _registry()
    reached: list[str] = []
    seen: list[Any] = []
    job_id: list[str] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        seen.append(reg.cancel(job_id[0]))
        handle.check_cancelled()
        reached.append("after check")
        return {}

    job_id.append(reg.submit("k", runner).id)
    reg.run_next()

    assert seen[0].state == "running"
    assert seen[0].cancel_requested is True
    assert reached == []
    assert reg.get(job_id[0]).state == "cancelled"


def test_enter_commit_makes_cancel_not_cancellable() -> None:
    reg = _registry()
    errors: list[BaseException] = []
    job_id: list[str] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        handle.enter_commit()
        handle.enter_commit()  # idempotent
        try:
            reg.cancel(job_id[0])
        except NotCancellable as exc:
            errors.append(exc)
        return {}

    job_id.append(reg.submit("k", runner).id)
    reg.run_next()

    assert len(errors) == 1
    assert reg.get(job_id[0]).state == "succeeded"


def test_enter_commit_after_a_requested_cancel_raises() -> None:
    """The atomic check-and-close: a cancel landing in the last batch is honoured."""
    reg = _registry()
    reached: list[str] = []
    job_id: list[str] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        reg.cancel(job_id[0])
        handle.enter_commit()
        reached.append("wrote")
        return {}

    job_id.append(reg.submit("k", runner).id)
    reg.run_next()

    assert reached == []
    assert reg.get(job_id[0]).state == "cancelled"


def test_an_unobserved_cancel_ends_succeeded_with_cancel_too_late() -> None:
    reg = _registry()
    job_id: list[str] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        handle.progress("finishing")
        reg.cancel(job_id[0])
        return {"written": True}

    job_id.append(reg.submit("k", runner).id)
    reg.run_next()

    snap = reg.get(job_id[0])
    assert snap.state == "succeeded"
    assert [e.code for e in snap.events].count("cancel_too_late") == 1
    assert dict(snap.result or {}) == {"written": True}


def test_cancel_on_a_terminal_job_returns_it_unchanged() -> None:
    reg = _registry()
    job = reg.submit("k", _returns({}))
    reg.run_next()
    before = reg.get(job.id)

    assert reg.cancel(job.id) == before


def test_cancel_is_idempotent_while_running() -> None:
    reg = _registry()
    snaps: list[Any] = []
    job_id: list[str] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        snaps.append(reg.cancel(job_id[0]))
        snaps.append(reg.cancel(job_id[0]))
        handle.check_cancelled()
        return {}

    job_id.append(reg.submit("k", runner).id)
    reg.run_next()

    assert [s.cancel_requested for s in snaps] == [True, True]


def test_cancel_unknown_raises() -> None:
    with pytest.raises(JobUnknown):
        _registry().cancel("f" * 32)


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled", "cancelled_queued"])
def test_cancellable_is_false_in_every_terminal_snapshot(outcome: str) -> None:
    """Including a runner that never called enter_commit."""
    reg = _registry()
    job_id: list[str] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        if outcome == "failed":
            raise RuntimeError("x")
        if outcome == "cancelled":
            raise JobCancelled("stop")
        return {}

    job_id.append(reg.submit("k", runner).id)
    if outcome == "cancelled_queued":
        reg.cancel(job_id[0])
    else:
        reg.run_next()

    snap = reg.get(job_id[0])
    assert snap.state in {"succeeded", "failed", "cancelled"}
    assert snap.cancellable is False


# ---------------------------------------------------------------- snapshots --


def test_snapshots_are_frozen_copies() -> None:
    reg = _registry()
    progressed = threading.Event()
    old: list[Any] = []
    job_id: list[str] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        old.append(reg.get(job_id[0]))
        handle.progress("embedding", done=1, total=1)
        progressed.set()
        return {"k": "v"}

    job_id.append(reg.submit("k", runner).id)
    reg.run_next()

    snapshot = old[0]
    assert progressed.is_set()
    assert [e.phase for e in snapshot.events] == ["queued"]
    assert isinstance(snapshot.events, tuple)
    with pytest.raises(AttributeError):
        snapshot.state = "failed"  # type: ignore[misc]
    result = reg.get(job_id[0]).result
    assert result is not None
    with pytest.raises(TypeError):
        result["k"] = "mutated"  # type: ignore[index]


# --------------------------------------------------------------- event cap --


def test_the_event_log_is_capped_and_keeps_what_it_must() -> None:
    reg = _registry(max_events=20)

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        handle.progress("extracting")
        for i in range(30):
            handle.progress("embedding", done=i, total=60)
            if i == 10:
                handle.note("secret_findings")
        handle.progress("embedding", done=60, total=60)
        return {}

    job = reg.submit("k", runner)
    reg.run_next()

    events = reg.get(job.id).events
    seqs = [e.seq for e in events]
    assert len(events) == 20
    assert seqs == sorted(set(seqs))  # strictly increasing, never reused
    phases_first = {e.phase: e for e in reversed(events)}
    assert phases_first["queued"].seq == 1
    assert phases_first["extracting"].seq == 2
    assert phases_first["embedding"].seq == 3  # the first event of its phase
    assert any(e.code == "secret_findings" for e in events)
    assert events[-1].done == 60  # the newest always survives


def test_the_latest_progress_event_survives_a_notable_newest_event() -> None:
    reg = _registry(max_events=5)

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        for i in range(6):
            handle.progress("embedding", done=i, total=9)
        handle.note("late")
        return {}

    job = reg.submit("k", runner)
    reg.run_next()

    events = reg.get(job.id).events
    assert len(events) == 5
    assert events[-1].code == "late"
    assert events[-2].done == 5


def test_after_across_a_gap_returns_every_surviving_later_event() -> None:
    reg = _registry(max_events=6)

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        for i in range(10):
            handle.progress("embedding", done=i, total=10)
        return {}

    job = reg.submit("k", runner)
    reg.run_next()

    full = reg.get(job.id).events
    gap_after = 3  # seq 3 was dropped from the middle
    assert 3 not in [e.seq for e in full]
    assert reg.get(job.id, after=gap_after).events == tuple(
        e for e in full if e.seq > gap_after
    )


def test_the_default_cap_is_two_hundred() -> None:
    reg = JobRegistry(
        clock=_Clock(), max_queued=4, retain=50, retain_for=timedelta(hours=1)
    )

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        for i in range(500):
            handle.progress("embedding", done=i, total=500)
        return {}

    job = reg.submit("k", runner)
    reg.run_next()
    assert len(reg.get(job.id).events) == 200


# --------------------------------------------------------------- retention --


def test_terminal_jobs_are_evicted_by_age_on_list() -> None:
    clock = _Clock()
    reg = _registry(clock)
    job = reg.submit("k", _returns({}))
    reg.run_next()

    clock.advance(minutes=59)
    assert [s.id for s in reg.list_recent()] == [job.id]
    clock.advance(minutes=2)
    assert reg.list_recent() == ()
    with pytest.raises(JobUnknown):
        reg.get(job.id)


def test_terminal_jobs_are_evicted_by_count_oldest_first_on_submit() -> None:
    clock = _Clock()
    reg = _registry(clock, retain=2)
    ids = []
    for _ in range(3):
        ids.append(reg.submit("k", _returns({})).id)
        reg.run_next()
        clock.advance(seconds=1)

    reg.submit("k", _returns({}))

    with pytest.raises(JobUnknown):
        reg.get(ids[0])
    assert reg.get(ids[2]).state == "succeeded"


def test_retention_can_be_overridden_per_kind() -> None:
    clock = _Clock()
    reg = _registry(clock, retain_for_by_kind={"ask": timedelta(minutes=10)})
    ask = reg.submit("ask", _returns({}))
    reg.run_next()
    ingest = reg.submit("ingest.paste", _returns({}))
    reg.run_next()

    clock.advance(minutes=11)
    remaining = {s.id for s in reg.list_recent()}

    assert ask.id not in remaining
    assert ingest.id in remaining


def test_live_jobs_are_never_evicted() -> None:
    clock = _Clock()
    reg = _registry(clock, retain=0)
    job = reg.submit("k", _returns({}))
    clock.advance(hours=5)

    assert [s.id for s in reg.list_recent()] == [job.id]


def test_list_recent_is_newest_first() -> None:
    clock = _Clock()
    reg = _registry(clock)
    first = reg.submit("k", _returns({}))
    clock.advance(seconds=1)
    second = reg.submit("k", _returns({}))

    assert [s.id for s in reg.list_recent()] == [second.id, first.id]


# ------------------------------------------------------------ the worker --


def test_stop_returns_the_running_job_id_after_the_timeout() -> None:
    reg = _registry()
    started = threading.Event()
    release = threading.Event()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        started.set()
        release.wait(timeout=30)
        return {}

    job = reg.submit("k", runner)
    reg.start()
    assert started.wait(timeout=30)

    try:
        assert reg.stop(timeout=0.05) == [job.id]
    finally:
        release.set()
    assert reg.stop(timeout=30) == []


def test_stop_cancels_queued_jobs_and_refuses_new_submits() -> None:
    reg = _registry()
    queued = reg.submit("k", _returns({}))

    assert reg.stop(timeout=1.0) == []

    assert reg.get(queued.id).state == "cancelled"
    with pytest.raises(ShuttingDown):
        reg.submit("k", _returns({}))


def test_stop_requests_cancel_on_the_running_job() -> None:
    reg = _registry()
    started = threading.Event()
    release = threading.Event()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        started.set()
        release.wait(timeout=30)
        handle.check_cancelled()
        return {}

    job = reg.submit("k", runner)
    reg.start()
    assert started.wait(timeout=30)
    assert reg.stop(timeout=0.01) == [job.id]
    release.set()

    assert reg.stop(timeout=30) == []
    assert reg.get(job.id).state == "cancelled"


def test_the_worker_runs_submitted_jobs() -> None:
    reg = _registry()
    done = threading.Event()

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        done.set()
        return {}

    reg.start()
    try:
        job = reg.submit("k", runner)
        assert done.wait(timeout=30)
    finally:
        assert reg.stop(timeout=30) == []
    assert reg.get(job.id).state == "succeeded"


def test_start_twice_is_a_no_op() -> None:
    reg = _registry()
    reg.start()
    reg.start()
    assert reg.stop(timeout=30) == []


def test_the_handle_exposes_its_job_id() -> None:
    reg = _registry()
    seen: list[str] = []

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        seen.append(handle.job_id)
        return {}

    job = reg.submit("k", runner)
    reg.run_next()
    assert seen == [job.id]


# ------------------------------------------- what a retained job holds on to --


class _HeavyRunner:
    """A runner object standing in for a paste closure that holds a whole body."""

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.body = "x" * 1024

    def __call__(self, handle: JobHandle) -> Mapping[str, Any]:
        if self.behaviour == "fail":
            raise RuntimeError("boom")
        if self.behaviour == "cancel":
            raise JobCancelled("stop")
        return {}


@pytest.mark.parametrize("path", ["succeed", "fail", "cancel", "cancel_queued", "stop_queued"])
def test_a_finished_job_releases_its_runner(path: str) -> None:
    """A retained job must not keep a paste body alive for the retention window."""
    import gc
    import weakref

    reg = _registry()
    runner = _HeavyRunner("succeed" if path.endswith("queued") else path)
    ref = weakref.ref(runner)
    job = reg.submit("k", runner)
    del runner

    if path == "cancel_queued":
        reg.cancel(job.id)
    elif path == "stop_queued":
        reg.stop(timeout=1.0)
    else:
        reg.run_next()
    gc.collect()

    assert reg.get(job.id).state in {"succeeded", "failed", "cancelled"}
    assert ref() is None


def test_the_result_is_deep_frozen_and_not_shared() -> None:
    reg = _registry()
    original: dict[str, Any] = {"findings": [{"kind": "aws"}], "nested": {"k": [1, 2]}}

    job = reg.submit("k", lambda handle: original)
    reg.run_next()
    original["findings"].append({"kind": "late"})
    original["nested"]["k"].append(3)

    result = reg.get(job.id).result
    assert result is not None
    assert result["findings"] == ({"kind": "aws"},)
    assert result["nested"]["k"] == (1, 2)
    with pytest.raises(TypeError):
        result["findings"][0]["kind"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        result["nested"]["new"] = 1  # type: ignore[index]
