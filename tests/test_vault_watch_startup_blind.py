"""The vault watcher is live while a slow startup graph reconcile runs (2026-10-22).

Production, 2026-10-22: after a restart, ``run_watcher`` sat 80+ minutes in
its startup ``sync_vault`` step 7 (graph reconcile; Ollama concept extraction
at ~8-10 min per new Krisp doc). The observer and the worker start only after
the startup sync returns, so vault edits went unwatched for hours. Plan:
``docs/plans/2026-10-22-vault-watcher-startup-blind.md``.

Each test injects a slow graph syncer (a structural ``GraphSyncTarget``) whose
first ``reconcile`` blocks until the test releases it, and a minimal fake
observer. Both are DI seams; no production module is patched. Ordering is
asserted only after a FIFO barrier through the lane (a ``remove`` is never
deduped), never after a guess at timing.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent

from brain.vault.frontmatter import dump_frontmatter
from brain.vault.watch import WatchConfig, run_watcher
from tests.thread_wait import WAIT_FOR_BACKSTOP_S

#: The lane's thread name (``brain.vault.graph_lane``); every graph sync must run on it.
_LANE_THREAD = "brain-vault-watcher-graph"


def _eventually(predicate: Callable[[], object]) -> bool:
    """Poll ``predicate`` until it holds or the shared backstop passes."""
    deadline = time.monotonic() + WAIT_FOR_BACKSTOP_S
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _Observer:
    """A minimal stand-in for watchdog's Observer: tests inject events."""

    instances: list[_Observer] = []

    def __init__(self) -> None:
        self.handler: Any = None
        self.started = False
        _Observer.instances.append(self)

    def schedule(self, handler: Any, path: str, recursive: bool = False) -> None:
        self.handler = handler

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        return None

    def join(self, timeout: float | None = None) -> None:
        return None

    def inject(self, event: Any) -> None:
        self.handler.on_any_event(event)


class _SlowGraphSyncer:
    """A ``GraphSyncTarget`` whose FIRST reconcile blocks until ``release`` is set.

    Each reconcile records the document's title as read from the DB when the
    reconcile STARTS, which is what a real reconcile works from, plus the
    thread it ran on. Removes are recorded too; tests use one as a barrier.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.seen: list[tuple[str, str | None]] = []
        self.threads: list[str] = []
        self.removed: list[str] = []
        self._first = True
        self._lock = threading.Lock()

    def reconcile(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        row = conn.execute("SELECT title FROM documents WHERE id = %s", (document_id,)).fetchone()
        with self._lock:
            self.seen.append((document_id, row[0] if row else None))
            self.threads.append(threading.current_thread().name)
            first, self._first = self._first, False
        if first:
            self.entered.set()
            self.release.wait(WAIT_FOR_BACKSTOP_S * 3)

    def remove(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        with self._lock:
            self.removed.append(document_id)


def _conn_factory() -> Callable[[], psycopg.Connection[Any]]:
    from brain.db import connect_raw
    from tests.conftest import TEST_DATABASE_URL

    return lambda: connect_raw(TEST_DATABASE_URL)


def _write(path: Path, fields: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _start(vault: Path, embedder: Any, syncer: _SlowGraphSyncer) -> threading.Thread:
    _Observer.instances.clear()
    result: queue.Queue[Any] = queue.Queue()

    def _target() -> None:
        try:
            result.put(
                run_watcher(
                    _conn_factory(),
                    embedder=embedder,
                    config=WatchConfig(vault_path=vault, debounce_ms=10),
                    observer_factory=_Observer,
                    install_signal_handlers=False,
                    graph_syncer=syncer,  # type: ignore[arg-type]
                )
            )
        except Exception as exc:  # pragma: no cover - surfaced by the test
            result.put(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread


def _observer_live() -> bool:
    return bool(_Observer.instances and _Observer.instances[0].started)


def _state() -> Any:
    return _Observer.instances[0].handler._state


def _drain_lane(syncer: _SlowGraphSyncer) -> bool:
    """FIFO barrier: True once every earlier lane request has been processed."""
    token = f"barrier-{uuid.uuid4()}"
    _state().graph_lane.remove(None, token)
    return _eventually(lambda: token in syncer.removed)


def _stop(syncer: _SlowGraphSyncer, thread: threading.Thread) -> None:
    syncer.release.set()
    if _eventually(_observer_live):
        _state().stop_event.set()
    thread.join(timeout=WAIT_FOR_BACKSTOP_S * 3)


def _title(conn: psycopg.Connection, doc_id: str) -> str | None:
    row = conn.execute("SELECT title FROM documents WHERE id = %s", (doc_id,)).fetchone()
    return row[0] if row else None


def _lane_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == _LANE_THREAD and t.is_alive()}


def test_watcher_is_live_while_the_startup_graph_reconcile_runs(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """W1: the observer starts, and a live edit is synced, while the reconcile blocks.

    After shutdown, the lane this watcher started is no longer running, and it
    closed cleanly: no "still finishing" warning for an idle lane.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "old.md", {"id": str(uuid.uuid4()), "title": "Old"}, "old\n")
    lanes_before = _lane_threads()
    syncer = _SlowGraphSyncer()
    thread = _start(vault, fake_embedder, syncer)
    try:
        assert syncer.entered.wait(WAIT_FOR_BACKSTOP_S), "the startup graph reconcile never ran"
        assert _eventually(_observer_live), (
            "the observer did not start while the startup graph reconcile was running"
        )

        new_id = str(uuid.uuid4())
        _write(vault / "new.md", {"id": new_id, "title": "New"}, "new\n")
        _Observer.instances[0].inject(FileCreatedEvent(str(vault / "new.md")))
        assert _eventually(lambda: _title(test_db, new_id) == "New"), (
            "a live edit was not synced while the startup graph reconcile was running"
        )
        assert not syncer.release.is_set(), "fixture: the reconcile must still be blocked"
        syncer.release.set()
        # Park the lane (idle, waiting for work) so the shutdown below must WAKE
        # it: a close that does not notify then times out deterministically.
        assert _drain_lane(syncer), "the graph lane never drained"
    finally:
        with caplog.at_level(logging.WARNING, logger="brain.vault.watch"):
            _stop(syncer, thread)

    still_finishing = [r for r in caplog.records if "graph lane still finishing" in r.getMessage()]
    assert not still_finishing, "run_watcher's close of an idle graph lane timed out"
    ours = _lane_threads() - lanes_before
    assert _eventually(lambda: not any(t.is_alive() for t in ours)), (
        "run_watcher returned with its graph lane still running (never closed)"
    )


def _edit_during_startup_reconcile(
    test_db: psycopg.Connection, embedder: Any, tmp_path: Path, x_first: bool
) -> tuple[list[str | None], set[str]]:
    """Two docs, X and Y. One startup reconcile blocks; X is edited meanwhile.

    ``x_first`` makes X's own reconcile the blocked (in-flight) one; otherwise
    Y's is, and X's is still pending when X is edited. Returns X's reconcile
    titles in order, and the set of threads ANY reconcile ran on, both read
    only after a lane barrier, so every request has been processed.
    """
    low, high = "00000000-0000-4000-8000-000000000001", "ffffffff-ffff-4fff-bfff-ffffffffffff"
    x_id, y_id = (low, high) if x_first else (high, low)
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "x.md", {"id": x_id, "title": "X old"}, "x\n")
    _write(vault / "y.md", {"id": y_id, "title": "Y"}, "y\n")
    syncer = _SlowGraphSyncer()
    thread = _start(vault, embedder, syncer)
    try:
        assert syncer.entered.wait(WAIT_FOR_BACKSTOP_S), "the startup graph reconcile never ran"
        assert _eventually(_observer_live), (
            "the observer did not start while the startup graph reconcile was running"
        )
        _write(vault / "x.md", {"id": x_id, "title": "X new"}, "x edited\n")
        _Observer.instances[0].inject(FileModifiedEvent(str(vault / "x.md")))
        # processed advances only after sync_one_file returns, i.e. after the
        # worker has submitted X's graph request to the lane.
        assert _eventually(lambda: _state().processed >= 1), "the worker never synced the edit"
        assert _title(test_db, x_id) == "X new", "fixture: the edit must be committed"
        syncer.release.set()
        assert _drain_lane(syncer), "the graph lane never drained"
        return [t for d, t in syncer.seen if d == x_id], set(syncer.threads)
    finally:
        _stop(syncer, thread)


def test_edit_while_its_reconcile_is_in_flight_is_reconciled_again(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """W2a: X's reconcile is IN FLIGHT when X is edited: X is reconciled again, after.

    Every reconcile runs on the lane thread: the worker never runs graph syncs.
    """
    x_titles, threads = _edit_during_startup_reconcile(
        test_db, fake_embedder, tmp_path, x_first=True
    )

    assert x_titles == ["X old", "X new"], (
        f"X was not reconciled again after its in-flight reconcile: {x_titles!r}"
    )
    assert threads == {_LANE_THREAD}, (
        f"a graph reconcile ran off the lane thread: {sorted(threads)!r}"
    )


def test_edit_while_its_reconcile_is_pending_is_reconciled_with_the_edit(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """W2b: X's reconcile is still PENDING when X is edited: it runs once, on the edit."""
    x_titles, threads = _edit_during_startup_reconcile(
        test_db, fake_embedder, tmp_path, x_first=False
    )

    assert x_titles == ["X new"], (
        f"X's pending reconcile was not deduped onto the edited content: {x_titles!r}"
    )
    assert threads == {_LANE_THREAD}, (
        f"a graph reconcile ran off the lane thread: {sorted(threads)!r}"
    )


def test_a_failed_setup_leaks_no_lane_thread(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """W3: if run_watcher fails before observing, it has not started a lane thread."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "n.md", {"id": str(uuid.uuid4()), "title": "N"}, "n\n")
    lanes_before = _lane_threads()
    syncer = _SlowGraphSyncer()

    def _broken_observer() -> Any:
        raise RuntimeError("synthetic observer failure")

    raised: Exception | None = None
    try:
        run_watcher(
            _conn_factory(),
            embedder=fake_embedder,
            config=WatchConfig(vault_path=vault, debounce_ms=10),
            observer_factory=_broken_observer,
            install_signal_handlers=False,
            graph_syncer=syncer,  # type: ignore[arg-type]
        )
    except RuntimeError as exc:
        raised = exc
    leaked = _lane_threads() - lanes_before
    syncer.release.set()

    assert raised is not None, "fixture: the observer factory must fail"
    assert not leaked, "run_watcher failed during setup and leaked a graph lane thread"


def test_undrained_removes_at_shutdown_are_logged(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    mocker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """W4: a remove still queued behind an in-flight reconcile at shutdown is logged.

    It names the document and the repair command, instead of vanishing.
    """
    mocker.patch("brain.vault.watch._LANE_CLOSE_TIMEOUT_S", 0.05)
    vault = tmp_path / "vault"
    vault.mkdir()
    doomed_id = str(uuid.uuid4())
    _write(vault / "doomed.md", {"id": doomed_id, "title": "Doomed"}, "d\n")
    syncer = _SlowGraphSyncer()
    thread = _start(vault, fake_embedder, syncer)
    try:
        assert syncer.entered.wait(WAIT_FOR_BACKSTOP_S), "the startup graph reconcile never ran"
        assert _eventually(_observer_live), "the observer never started"
        (vault / "doomed.md").unlink()
        _Observer.instances[0].inject(FileDeletedEvent(str(vault / "doomed.md")))
        assert _eventually(lambda: _state().processed >= 1), "the worker never handled the delete"

        with caplog.at_level(logging.WARNING, logger="brain.vault.watch"):
            _state().stop_event.set()  # the reconcile is STILL blocked: close times out
            thread.join(timeout=WAIT_FOR_BACKSTOP_S * 3)
    finally:
        syncer.release.set()

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(doomed_id in m and "brain graphrag build --force" in m for m in warnings), (
        f"an undrained graph remove was not logged with its id and the repair: {warnings!r}"
    )
    assert any("still finishing a request or reconnecting" in m for m in warnings), (
        f"the lane warning does not cover a lane that is reconnecting: {warnings!r}"
    )


class _ObserverThatCannotStart(_Observer):
    def start(self) -> None:
        raise RuntimeError("synthetic observer start failure")


def test_an_observer_that_cannot_start_leaks_no_lane_thread(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """W3b: the lane starts LAST, so even ``observer.start()`` failing leaks no lane."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "n.md", {"id": str(uuid.uuid4()), "title": "N"}, "n\n")
    lanes_before = _lane_threads()
    syncer = _SlowGraphSyncer()

    raised: Exception | None = None
    try:
        run_watcher(
            _conn_factory(),
            embedder=fake_embedder,
            config=WatchConfig(vault_path=vault, debounce_ms=10),
            observer_factory=_ObserverThatCannotStart,
            install_signal_handlers=False,
            graph_syncer=syncer,  # type: ignore[arg-type]
        )
    except RuntimeError as exc:
        raised = exc
    leaked = _lane_threads() - lanes_before
    syncer.release.set()

    assert raised is not None, "fixture: observer.start() must fail"
    assert not leaked, "observer.start() failed and run_watcher leaked a graph lane thread"
