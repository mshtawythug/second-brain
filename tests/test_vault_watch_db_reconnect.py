"""The vault watcher's worker survives a Postgres restart (WR1-WR4).

Plan: ``docs/plans/2026-10-22-vault-watcher-db-reconnect.md``. Before the fix,
the worker kept using its dead connection after the server dropped it, so every
later job failed until the watcher was restarted. A restart is simulated with
``pg_terminate_backend`` on the worker's own backend, in the TEST database
only. No production module is patched; ``monkeypatch`` only lengthens the
backoff for the shutdown test, so a wake that did not happen shows as a hang.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from watchdog.events import FileCreatedEvent

from brain.db import connect_raw
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.watch import WatchConfig, _Job, _WatcherState, _worker_loop, run_watcher
from tests.conftest import TEST_DATABASE_URL
from tests.thread_wait import WAIT_FOR_BACKSTOP_S, blocked_in_wait, wait_for

if TYPE_CHECKING:
    from brain.vault.db_reconnect import ReconnectingConnection

assert ":5434/" in TEST_DATABASE_URL, "these tests terminate backends: test DB (5434) only"


def _write(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter({"id": str(uuid.uuid4()), "title": title}, f"{title}\n"))


def _titles(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute("SELECT title FROM documents WHERE kind = 'vault'").fetchall()
    return {r[0] for r in rows}


def _terminate(killer: psycopg.Connection[Any], victim: psycopg.Connection[Any]) -> None:
    """Terminate ``victim``'s backend and wait until the server has dropped it."""
    pid = victim.info.backend_pid
    killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
    wait_for(
        lambda: (
            killer.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone()
            == (0,)
        )
    )


class _Factory:
    """Opens real test-DB connections and records them; can be told to refuse."""

    def __init__(self) -> None:
        self.made: list[psycopg.Connection[Any]] = []
        self.calls = 0
        self.refuse = False

    def __call__(self) -> psycopg.Connection[Any]:
        self.calls += 1
        if self.refuse:
            raise psycopg.OperationalError("synthetic: the server is down")
        conn = connect_raw(TEST_DATABASE_URL)
        self.made.append(conn)
        return conn

    def close_all(self) -> None:
        for conn in self.made:
            conn.close()


def _drain(
    link: ReconnectingConnection, embedder: Any, vault: Path, state: _WatcherState, *jobs: _Job
) -> None:
    for job in jobs:
        state.jobs.put(job)
    state.jobs.put(None)
    _worker_loop(link, embedder, WatchConfig(vault_path=vault, debounce_ms=10), state)


def test_worker_syncs_on_a_fresh_connection_after_its_backend_is_terminated(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """WR1: after the server drops the worker's connection, the next jobs still sync."""
    from brain.vault.db_reconnect import ReconnectingConnection

    vault = tmp_path / "vault"
    for name in ("a", "b", "c"):
        _write(vault / f"{name}.md", f"Title {name}")
    factory = _Factory()
    first = factory()
    first.autocommit = True
    state = _WatcherState()
    link = ReconnectingConnection(
        factory, label="worker", sleep=lambda _d: None, stopping=lambda: False, conn=first
    )
    try:
        _drain(link, fake_embedder, vault, state, _Job("upsert", vault / "a.md"))
        _terminate(test_db, first)
        _drain(
            link,
            fake_embedder,
            vault,
            state,
            _Job("upsert", vault / "b.md"),
            _Job("upsert", vault / "c.md"),
        )
    finally:
        link.close()
        factory.close_all()

    assert {"Title b", "Title c"} <= _titles(test_db), (
        "the jobs after the backend was terminated were not synced on a fresh connection"
    )
    assert (state.processed, state.errors) == (3, 0), (
        f"processed/errors {(state.processed, state.errors)}, expected (3, 0)"
    )


class _Dead:
    closed = True
    autocommit = True

    def close(self) -> None:
        return None


def test_worker_reports_unapplied_jobs_when_it_cannot_reconnect_at_shutdown(
    fake_embedder: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """WR2: shutdown with the DB down: every queued job is reported, nothing sleeps."""
    from brain.vault.db_reconnect import ReconnectingConnection

    vault = tmp_path / "vault"
    vault.mkdir()
    factory = _Factory()
    factory.refuse = True
    state = _WatcherState()
    state.stop_event.set()
    sleeps: list[float] = []
    link = ReconnectingConnection(
        factory,
        label="worker",
        sleep=sleeps.append,
        stopping=state.stop_event.is_set,
        conn=_Dead(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.WARNING, logger="brain.vault.watch"):
        _drain(
            link,
            fake_embedder,
            vault,
            state,
            _Job("upsert", vault / "a.md"),
            _Job("delete", vault / "b.md"),
            _Job("upsert", vault / "c.md"),
        )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "3 job(s) not applied (1 delete(s))" in logged, (
        f"the jobs the worker could not apply were not reported: {logged!r}"
    )
    assert state.jobs.empty(), "the worker returned before reading its queue to the sentinel"
    assert sleeps == [], f"the worker slept during shutdown: {sleeps}"


#: ``run_watcher``'s worker thread name.
_WORKER_THREAD = "brain-vault-watcher-worker"


class _Observer:
    """A minimal stand-in for watchdog's Observer: the test injects events."""

    def __init__(self) -> None:
        self.handler: Any = None
        self.started = threading.Event()

    def schedule(self, handler: Any, path: str, recursive: bool = False) -> None:
        self.handler = handler

    def start(self) -> None:
        self.started.set()

    def stop(self) -> None:
        return None

    def join(self, timeout: float | None = None) -> None:
        return None


def _start(
    vault: Path, embedder: Any, factory: Callable[[], psycopg.Connection[Any]]
) -> tuple[threading.Thread, _Observer, queue.Queue[Any]]:
    observer = _Observer()
    result: queue.Queue[Any] = queue.Queue()

    def _target() -> None:
        try:
            result.put(
                run_watcher(
                    factory,
                    embedder=embedder,
                    config=WatchConfig(vault_path=vault, debounce_ms=10),
                    observer_factory=lambda: observer,  # type: ignore[arg-type,return-value]
                    install_signal_handlers=False,
                )
            )
        except Exception as exc:  # pragma: no cover - surfaced by the test
            result.put(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    assert observer.started.wait(WAIT_FOR_BACKSTOP_S), "fixture: the watcher never started"
    return thread, observer, result


#: Half of ``run_watcher``'s 10s worker join. A worker that slept out its backoff
#: still lets ``run_watcher`` return, but only after that join times out, so a
#: shutdown bounded by this proves the backoff was woken.
_PROMPT_SHUTDOWN_S = 5.0


def _stop(thread: threading.Thread, observer: _Observer, timeout: float) -> None:
    observer.handler._state.stop_event.set()
    thread.join(timeout)


def test_run_watcher_survives_its_worker_backend_being_terminated(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """WR3: end to end; at shutdown the worker's CURRENT connection is closed."""
    vault = tmp_path / "vault"
    vault.mkdir()
    factory = _Factory()
    thread, observer, _result = _start(vault, fake_embedder, factory)
    try:
        worker_conn = factory.made[1]  # [0] is the startup sync's, closed by now
        _terminate(test_db, worker_conn)
        _write(vault / "after.md", "After restart")
        observer.handler.on_any_event(FileCreatedEvent(str(vault / "after.md")))
        try:
            wait_for(lambda: "After restart" in _titles(test_db))
            synced = True
        except AssertionError:
            synced = False
    finally:
        _stop(thread, observer, WAIT_FOR_BACKSTOP_S * 3)
        current = factory.made[-1]
        current_closed = current.closed
        factory.close_all()

    assert synced, "a file created after the worker's backend was terminated was not synced"
    assert not thread.is_alive(), "run_watcher did not return"
    assert len(factory.made) == 3 and current_closed, (
        "shutdown did not close the worker's CURRENT (reconnected) connection"
    )


def test_shutdown_wakes_a_worker_in_reconnect_backoff(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WR4: a worker sleeping out a 600s backoff must not hold up shutdown."""
    monkeypatch.setattr("brain.vault.db_reconnect.BACKOFF_INITIAL_S", 600.0)
    monkeypatch.setattr("brain.vault.db_reconnect.BACKOFF_MAX_S", 600.0)
    vault = tmp_path / "vault"
    vault.mkdir()
    factory = _Factory()
    before = set(threading.enumerate())
    thread, observer, _result = _start(vault, fake_embedder, factory)
    worker = next(t for t in threading.enumerate() if t.name == _WORKER_THREAD and t not in before)
    with caplog.at_level(logging.WARNING, logger="brain.vault.watch"):
        try:
            _terminate(test_db, factory.made[1])
            factory.refuse = True
            _write(vault / "lost.md", "Lost")
            observer.handler.on_any_event(FileCreatedEvent(str(vault / "lost.md")))
            # INSIDE the backoff sleep, not just past the failed reconnect: a stop
            # that lands before the worker's stopping() check never exercises the
            # sleep (the same race mutation G3 found in L14).
            try:
                wait_for(lambda: blocked_in_wait(worker, "_connect"))
                retried = True
            except AssertionError:
                retried = False
        finally:
            _stop(thread, observer, _PROMPT_SHUTDOWN_S)
            factory.close_all()

    assert retried, "the worker never entered its reconnect backoff"
    assert not thread.is_alive(), "shutdown waited out the worker's reconnect backoff"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "1 job(s) not applied" in logged, (
        f"the job the worker could not apply was not reported: {logged!r}"
    )
