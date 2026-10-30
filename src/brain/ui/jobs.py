"""`JobRegistry` — the in-process, kind-agnostic job subsystem behind long UI tasks.

Ingest spec §4 (D1–D5). Long work never runs in a request handler: handlers here
are ``async def`` calling synchronous psycopg ON the event loop, so an ingest in a
handler would freeze every other request, the job's own polls included. Instead
one dedicated daemon worker thread runs one job at a time from a bounded FIFO
queue, and clients poll an append-only event log.

**Starlette-free and psycopg-free** on purpose, so the whole state machine is
testable without a server or a database. **Kind-agnostic**: ``kind`` is a free
string, and a job's failure mapping arrives with it (``submit(..., on_error=)``),
so the Agent tab (RM-18a) registers ``"ask"`` without editing this module. So does
its cleanup (``submit(..., on_settled=)``, RM-13): called exactly once, outside the
lock, on EVERY terminal transition, including a queued job that is cancelled or
dropped by :meth:`JobRegistry.stop` and so never runs. A runner's own ``finally``
cannot cover those, and an uploaded file's staged copy would outlive its job.

Invariants:

- **One lock, immutable snapshots.** Every mutation of a job record happens under
  ``self._lock``; every read returns a fresh frozen :class:`JobSnapshot`, so a poll
  never sees a half-updated record and a later event never appears in an old one.
- **``run_next`` is the test seam.** Tests never start the thread; they call
  ``run_next()`` on the test thread. Production calls :meth:`JobRegistry.start`
  once, from the app lifespan.
- **Cancellation is cooperative** (D3). :meth:`JobHandle.enter_commit` is an atomic
  check-and-close: a cancel accepted (202) before it is always honoured, and every
  cancel after it is refused (409). An accepted cancel the runner never observed
  ends ``succeeded`` with one ``cancel_too_late`` event, so no cancel is silently
  dropped.
- **Jobs are in memory only** (D4). Nothing survives a restart, by design.
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, Protocol

from ..errors import BrainError, describe_failure

logger = logging.getLogger(__name__)

JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]

#: Terminal states. ``cancellable`` is false in every one of them.
TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

#: Per-job event-log cap (Ingest spec §4.2).
DEFAULT_MAX_EVENTS = 200

#: The code of the event that reports a cancel accepted too late to honour.
CANCEL_TOO_LATE = "cancel_too_late"


class JobCancelled(BrainError):
    """Raised inside a runner when a requested cancel is honoured.

    A :class:`~brain.errors.BrainError`, deliberately NOT an
    :class:`~brain.errors.EmbedError`: it is raised from inside an embedder's
    ``embed()``, and no handler in the ingest pipeline that maps embed failures
    may swallow it.
    """


class QueueFull(BrainError):
    """``submit`` refused: the queue already holds ``max_queued`` jobs (429)."""


class JobUnknown(BrainError):
    """No such job: never submitted, evicted, or lost to a restart (404)."""


class NotCancellable(BrainError):
    """The job is past :meth:`JobHandle.enter_commit`; writing has started (409)."""


class ShuttingDown(BrainError):
    """``submit`` refused: :meth:`JobRegistry.stop` has begun (503)."""


@dataclass(frozen=True)
class JobEvent:
    """One entry in a job's append-only progress log."""

    seq: int  # 1-based, strictly increasing per job, never reused
    at: datetime
    phase: str
    done: int | None
    total: int | None
    code: str | None  # set on a *notable* event, e.g. "secret_findings"


@dataclass(frozen=True)
class JobError:
    """A failed job's error: a stable ``code`` and a FIXED message.

    The message is chosen per code by the job's mapper and never carries the
    exception's text, which can quote document content.
    """

    code: str
    message: str


@dataclass(frozen=True)
class JobSnapshot:
    """What a poll returns: an immutable copy, never the live record."""

    id: str
    kind: str
    state: JobState
    cancellable: bool
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    events: tuple[JobEvent, ...]
    result: Mapping[str, Any] | None
    error: JobError | None


class JobHandle(Protocol):
    """What a runner sees of its own job."""

    @property
    def job_id(self) -> str: ...

    def progress(
        self, phase: str, *, done: int | None = None, total: int | None = None
    ) -> None: ...

    def note(self, code: str) -> None: ...

    def check_cancelled(self) -> None: ...

    def enter_commit(self) -> None: ...


Runner = Callable[[JobHandle], Mapping[str, Any]]
ErrorMapper = Callable[[Exception], JobError]
SettledHook = Callable[[], None]


def freeze(value: Any) -> Any:
    """A deep, immutable copy of a JSON-shaped value.

    Mappings become read-only ``MappingProxyType`` views over fresh dicts, and
    lists and tuples become tuples, all the way down, so no snapshot shares a
    mutable object with the runner that built it or with another snapshot.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def generic_failure(exc: Exception) -> JobError:
    """The fallback mapping: the class name only, never the message."""
    return JobError("internal_error", f"Internal error ({type(exc).__name__}).")


@dataclass
class _Job:
    """The live, lock-guarded record behind a job. Never leaves this module."""

    id: str
    kind: str
    # Dropped on every terminal transition (``_finish_locked``): a paste runner's
    # closure holds the whole body, and a retained job must not keep it alive for
    # the retention window.
    runner: Runner | None
    on_error: ErrorMapper
    created_at: datetime
    # Popped by ``_finish_locked``, so it can only ever run once.
    on_settled: SettledHook | None = None
    state: JobState = "queued"
    cancellable: bool = True
    cancel_requested: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    events: list[JobEvent] = field(default_factory=list)
    first_of_phase: set[int] = field(default_factory=set)
    phases_seen: set[str] = field(default_factory=set)
    next_seq: int = 1
    result: Mapping[str, Any] | None = None
    error: JobError | None = None


class _Handle:
    """The :class:`JobHandle` bound to one job; every method takes the registry lock."""

    def __init__(self, registry: JobRegistry, job: _Job) -> None:
        self._registry = registry
        self._job = job

    @property
    def job_id(self) -> str:
        """The job's id, for a runner's own log lines."""
        return self._job.id

    def progress(
        self, phase: str, *, done: int | None = None, total: int | None = None
    ) -> None:
        with self._registry._lock:
            self._registry._append_locked(self._job, phase, done, total, None)

    def note(self, code: str) -> None:
        with self._registry._lock:
            self._registry._append_locked(
                self._job, self._registry._phase_locked(self._job), None, None, code
            )

    def check_cancelled(self) -> None:
        with self._registry._lock:
            if self._job.cancel_requested:
                raise JobCancelled("cancel requested")

    def enter_commit(self) -> None:
        """Atomic check-and-close: raise if a cancel was requested, else close."""
        with self._registry._lock:
            if self._job.cancel_requested:
                raise JobCancelled("cancel requested")
            self._job.cancellable = False


class JobRegistry:
    """One worker, a bounded FIFO queue, bounded retention. See the module docstring."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        max_queued: int,
        retain: int,
        retain_for: timedelta,
        retain_for_by_kind: Mapping[str, timedelta] | None = None,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        self._clock = clock
        self._max_queued = max_queued
        self._retain = retain
        self._retain_for = retain_for
        self._retain_for_by_kind = dict(retain_for_by_kind or {})
        self._max_events = max_events
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._jobs: dict[str, _Job] = {}
        self._queue: deque[_Job] = deque()
        self._stopping = False
        self._thread: threading.Thread | None = None
        # Hooks of jobs that settled under the lock, run by _run_settled() after it.
        self._settled: list[tuple[_Job, SettledHook]] = []

    # ------------------------------------------------------------ public --

    def submit(
        self,
        kind: str,
        runner: Runner,
        *,
        on_error: ErrorMapper | None = None,
        on_settled: SettledHook | None = None,
    ) -> JobSnapshot:
        """Queue ``runner``. Raises :class:`QueueFull` or :class:`ShuttingDown`.

        ``on_settled`` runs once when the job reaches any terminal state; it is not
        called when ``submit`` itself raises.
        """
        with self._lock:
            if self._stopping:
                raise ShuttingDown("the server is shutting down")
            self._evict_locked()
            if len(self._queue) >= self._max_queued:
                raise QueueFull(f"at most {self._max_queued} jobs may wait")
            job = _Job(
                id=uuid.uuid4().hex,
                kind=kind,
                runner=runner,
                on_error=on_error or generic_failure,
                created_at=self._clock(),
                on_settled=on_settled,
            )
            self._append_locked(job, "queued", None, None, None)
            self._jobs[job.id] = job
            self._queue.append(job)
            self._wake.notify_all()
            return self._snapshot_locked(job)

    def get(self, job_id: str, *, after: int = 0) -> JobSnapshot:
        """The job's snapshot, with only the events whose ``seq > after``.

        Evicts first, as every read does: an unlisted job (an ask, RM-18a) must not
        outlive its retention because nobody listed or submitted.
        """
        with self._lock:
            self._evict_locked()
            return self._snapshot_locked(self._require_locked(job_id), after=after)

    def list_recent(self) -> tuple[JobSnapshot, ...]:
        """Every retained job, newest first."""
        with self._lock:
            self._evict_locked()
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return tuple(self._snapshot_locked(j) for j in jobs)

    def cancel(self, job_id: str) -> JobSnapshot:
        """Cancel per the Ingest spec §4.4 table. Raises :class:`NotCancellable`."""
        with self._lock:
            self._evict_locked()
            job = self._require_locked(job_id)
            if job.state == "queued":
                self._queue.remove(job)
                self._finish_locked(job, "cancelled")
            elif job.state == "running":
                if not job.cancellable:
                    raise NotCancellable("writing has started; the job will finish")
                job.cancel_requested = True
            snapshot = self._snapshot_locked(job)
        self._run_settled()
        return snapshot

    def retention(self, kind: str) -> timedelta:
        """How long a finished job of ``kind`` stays readable (the per-kind override)."""
        return self._retain_for_by_kind.get(kind, self._retain_for)

    def run_next(self) -> bool:
        """Run ONE queued job on the calling thread. False when none is queued."""
        with self._lock:
            if not self._queue:
                return False
            job = self._queue.popleft()
            job.state = "running"
            job.started_at = self._clock()
            runner = job.runner
        assert runner is not None  # only a terminal job has released its runner
        self._execute(job, runner)
        return True

    def start(self) -> None:
        """Spawn the daemon worker thread. A second call is a no-op."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._work, name="brain-ui-jobs", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout: float) -> list[str]:
        """Refuse submits, cancel the queue, ask the running job to stop, then join.

        Returns the ids of jobs still running when ``timeout`` expires. The worker
        is a daemon, so a wedged call cannot hang shutdown; a transaction in flight
        rolls back when the process dies, which is what keeps a document atomic.
        """
        with self._lock:
            self._stopping = True
            while self._queue:
                self._finish_locked(self._queue.popleft(), "cancelled")
            for job in self._jobs.values():
                if job.state == "running" and job.cancellable:
                    job.cancel_requested = True
            self._wake.notify_all()
            thread = self._thread
        self._run_settled()
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            return [j.id for j in self._jobs.values() if j.state == "running"]

    # ---------------------------------------------------------- internals --

    def _work(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._stopping:
                    self._wake.wait()
                if self._stopping:
                    return
            self.run_next()

    def _execute(self, job: _Job, runner: Runner) -> None:
        handle = _Handle(self, job)
        try:
            result = runner(handle)
        except JobCancelled:
            with self._lock:
                self._finish_locked(job, "cancelled")
            self._run_settled()
            return
        except Exception as exc:
            logger.warning("ui %s job %s failed: %s", job.kind, job.id, describe_failure(exc))
            self._fail(job, self._map_error(job, exc))
            return
        with self._lock:
            if job.cancel_requested:
                self._append_locked(
                    job, self._phase_locked(job), None, None, CANCEL_TOO_LATE
                )
            job.result = freeze(result)
            self._finish_locked(job, "succeeded")
        self._run_settled()

    def _fail(self, job: _Job, error: JobError) -> None:
        # Outside the `except` block on purpose: the log-privacy guard's taint scan
        # (tests/log_privacy_rules.py) flags an attribute write through `job` there,
        # and treats `job`, so the class-only failure line above, as exception-derived.
        with self._lock:
            job.error = error
            self._finish_locked(job, "failed")
        self._run_settled()

    @staticmethod
    def _map_error(job: _Job, exc: Exception) -> JobError:
        try:
            return job.on_error(exc)
        except Exception as mapper_exc:  # a mapper bug must not kill the worker
            logger.warning(
                "ui %s job %s: error mapper failed: %s",
                job.kind, job.id, describe_failure(mapper_exc),
            )
            return generic_failure(exc)

    def _finish_locked(self, job: _Job, state: JobState) -> None:
        job.state = state
        job.cancellable = False
        job.finished_at = self._clock()
        job.runner = None
        if job.on_settled is not None:
            self._settled.append((job, job.on_settled))
            job.on_settled = None

    def _run_settled(self) -> None:
        """Run the hooks of jobs that settled, OUTSIDE the lock.

        A hook (deleting a staged file) can take time and may call back into the
        registry, so it never runs under the lock. A raising hook is logged by
        class and never reaches the worker loop.
        """
        with self._lock:
            pending, self._settled = self._settled, []
        for job, hook in pending:
            try:
                hook()
            except Exception as exc:
                logger.warning(
                    "ui %s job %s: cleanup failed: %s", job.kind, job.id, describe_failure(exc)
                )

    def _require_locked(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobUnknown("no such job")
        return job

    @staticmethod
    def _phase_locked(job: _Job) -> str:
        return job.events[-1].phase if job.events else "queued"

    def _append_locked(
        self,
        job: _Job,
        phase: str,
        done: int | None,
        total: int | None,
        code: str | None,
    ) -> None:
        seq = job.next_seq
        job.next_seq += 1
        if phase not in job.phases_seen:
            job.phases_seen.add(phase)
            job.first_of_phase.add(seq)
        job.events.append(JobEvent(seq, self._clock(), phase, done, total, code))
        if len(job.events) > self._max_events:
            self._drop_one_locked(job)

    @staticmethod
    def _drop_one_locked(job: _Job) -> None:
        """Remove the oldest progress event that is safe to lose (§4.2).

        Never dropped: a notable event (one with a ``code``), the first event of
        each phase, and the latest progress event. Progress is cumulative, so one
        dropped from the middle loses nothing a later event does not restate.
        Should every event be protected, the log exceeds the cap rather than lose
        a protected one; no ingest path comes near that.
        """
        latest_progress = next(
            (e.seq for e in reversed(job.events) if e.code is None), None
        )
        for index, event in enumerate(job.events):
            if (
                event.code is None
                and event.seq not in job.first_of_phase
                and event.seq != latest_progress
            ):
                del job.events[index]
                return

    def _evict_locked(self) -> None:
        now = self._clock()
        terminal = [
            j for j in self._jobs.values()
            if j.state in TERMINAL_STATES and j.finished_at is not None
        ]
        for job in terminal:
            ttl = self.retention(job.kind)
            assert job.finished_at is not None
            if job.finished_at + ttl <= now:
                del self._jobs[job.id]
        survivors = sorted(
            (j for j in terminal if j.id in self._jobs),
            key=lambda j: j.finished_at or now,
        )
        for job in survivors[: max(0, len(survivors) - self._retain)]:
            del self._jobs[job.id]

    def _snapshot_locked(self, job: _Job, *, after: int = 0) -> JobSnapshot:
        return JobSnapshot(
            id=job.id,
            kind=job.kind,
            state=job.state,
            cancellable=job.cancellable,
            cancel_requested=job.cancel_requested,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            events=tuple(e for e in job.events if e.seq > after),
            result=job.result,
            error=job.error,
        )
