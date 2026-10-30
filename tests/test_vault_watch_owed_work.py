"""Owed watcher work survives shutdown and reconnects (RM-26, H-2 and H-4).

Plan: ``docs/plans/2026-10-30-rm-26-owed-watcher-work.md``. Each test drives the
real watcher (``run_watcher`` or its worker loop) on the test DB, through the
PR #51 / #56 harness shapes: an injected observer, ``tests.thread_wait``
barriers, and a connection that dies at a chosen statement. No production module
is patched. The journal lives in ``tmp_path``; the session ``BRAIN_HOME`` is
never written.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from watchdog.events import FileDeletedEvent, FileMovedEvent

from brain.db import connect_raw
from brain.ingest import ingest_document
from brain.vault.db_reconnect import ReconnectingConnection
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.owed_work import JOURNAL_NAME, OwedJournal
from brain.vault.watch import WatchConfig, _Job, _WatcherState, _worker_loop, run_watcher
from tests.conftest import TEST_DATABASE_URL
from tests.test_graphrag_sync_wiring import (
    _age_document_count,
    _enabled_syncer,
    _gmail_doc,
    _seed_directory,
)
from tests.thread_wait import WAIT_FOR_BACKSTOP_S, wait_for

assert ":5434/" in TEST_DATABASE_URL, "these tests open and kill connections: test DB only"


def _write(path: Path, title: str, *, doc_id: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"title": title} if doc_id is None else {"id": doc_id, "title": title}
    path.write_text(dump_frontmatter(meta, f"{title} body.\n"))


def _row_at(conn: psycopg.Connection[Any], vault_path: str) -> str | None:
    row = conn.execute(
        "SELECT id::text FROM documents WHERE kind = 'vault' AND vault_path = %s",
        (vault_path,),
    ).fetchone()
    return None if row is None else str(row[0])


def _journal_entries(journal: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in journal.read_text().splitlines()]


class _Factory:
    """Opens real test-DB connections; refuses every call from ``refuse_from`` on."""

    def __init__(self, refuse_from: int | None = None) -> None:
        self.made: list[psycopg.Connection[Any]] = []
        self.calls = 0
        self.refuse_from = refuse_from

    def __call__(self) -> psycopg.Connection[Any]:
        self.calls += 1
        if self.refuse_from is not None and self.calls >= self.refuse_from:
            raise psycopg.OperationalError("synthetic: the server is down")
        conn = connect_raw(TEST_DATABASE_URL)
        self.made.append(conn)
        return conn

    def close_all(self) -> None:
        for conn in self.made:
            conn.close()


class _Observer:
    """A stand-in for watchdog's Observer: the test injects events."""

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


class _Watcher:
    """One ``run_watcher`` on a thread, with the owed-work journal on."""

    def __init__(
        self,
        vault: Path,
        journal: Path,
        embedder: Any,
        factory: Callable[[], psycopg.Connection[Any]],
        graph_syncer: Any = None,
    ) -> None:
        self.observer = _Observer()
        self._result: queue.Queue[Any] = queue.Queue()

        def _target() -> None:
            try:
                self._result.put(
                    run_watcher(
                        factory,
                        embedder=embedder,
                        config=WatchConfig(vault_path=vault, debounce_ms=10),
                        observer_factory=lambda: self.observer,  # type: ignore[arg-type,return-value]
                        install_signal_handlers=False,
                        graph_syncer=graph_syncer,
                        owed_journal=journal,
                    )
                )
            except Exception as exc:  # pragma: no cover - surfaced by stop()
                self._result.put(exc)

        self._thread = threading.Thread(target=_target, daemon=True)
        self._thread.start()
        assert self.observer.started.wait(WAIT_FOR_BACKSTOP_S), "the watcher never started"

    def stop(self) -> None:
        self.observer.handler._state.stop_event.set()
        self._thread.join(WAIT_FOR_BACKSTOP_S * 3)
        assert not self._thread.is_alive(), "run_watcher did not return"
        result = self._result.get_nowait()
        assert not isinstance(result, Exception), f"run_watcher raised {result!r}"


def test_a_late_delete_after_close_is_applied_at_the_next_start(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """Criterion 1 (H-2)."""
    vault, journal = tmp_path / "vault", tmp_path / "state" / JOURNAL_NAME
    _write(vault / "gone.md", "Gone", doc_id=str(uuid.uuid4()))
    factory = _Factory()
    try:
        first = _Watcher(vault, journal, fake_embedder, factory)
        assert _row_at(test_db, "gone.md") is not None, "fixture: the startup sync missed it"
        first.stop()
        (vault / "gone.md").unlink()
        first.observer.handler.on_any_event(FileDeletedEvent(str(vault / "gone.md")))

        assert _journal_entries(journal) == [{"action": "delete", "path": "gone.md"}]
        _Watcher(vault, journal, fake_embedder, factory).stop()
    finally:
        factory.close_all()

    assert _row_at(test_db, "gone.md") is None, "the late delete was not applied at the next start"


def test_a_late_move_after_close_leaves_no_row_at_its_source(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """Criterion 2 (H-2): a move owes a delete of its SOURCE.

    The destination here carries no frontmatter id, so the startup sync files
    it as a new document and, without the journal, leaves the source's row.
    """
    vault, journal = tmp_path / "vault", tmp_path / "state" / JOURNAL_NAME
    _write(vault / "src.md", "Source", doc_id=str(uuid.uuid4()))
    factory = _Factory()
    try:
        first = _Watcher(vault, journal, fake_embedder, factory)
        first.stop()
        (vault / "src.md").unlink()
        _write(vault / "dst.md", "Destination")
        first.observer.handler.on_any_event(
            FileMovedEvent(str(vault / "src.md"), str(vault / "dst.md"))
        )
        _Watcher(vault, journal, fake_embedder, factory).stop()
    finally:
        factory.close_all()

    assert _row_at(test_db, "src.md") is None, "the moved file's source row survived"
    assert _row_at(test_db, "dst.md") is not None, "the destination was not synced"


def test_a_late_rename_keeps_its_document_id(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """A late real rename (the destination keeps the frontmatter id) is not lost.

    The owed delete of its source runs after the startup sync, which has already
    filed the destination under the same document id.
    """
    vault, journal = tmp_path / "vault", tmp_path / "state" / JOURNAL_NAME
    doc_id = str(uuid.uuid4())
    _write(vault / "old.md", "Renamed", doc_id=doc_id)
    factory = _Factory()
    try:
        first = _Watcher(vault, journal, fake_embedder, factory)
        first.stop()
        (vault / "old.md").rename(vault / "new.md")
        first.observer.handler.on_any_event(
            FileMovedEvent(str(vault / "old.md"), str(vault / "new.md"))
        )
        _Watcher(vault, journal, fake_embedder, factory).stop()
    finally:
        factory.close_all()

    assert _row_at(test_db, "new.md") == doc_id, "the rename lost its document id"
    assert _row_at(test_db, "old.md") is None


class _Dead:
    closed = True
    autocommit = True

    def close(self) -> None:
        return None


def test_a_delete_unapplied_at_shutdown_is_applied_at_the_next_start(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """Criterion 3 (H-4b): applied without ``--prune``."""
    vault, journal = tmp_path / "vault", tmp_path / "state" / JOURNAL_NAME
    _write(vault / "gone.md", "Gone", doc_id=str(uuid.uuid4()))
    factory = _Factory()
    try:
        _Watcher(vault, journal, fake_embedder, factory).stop()  # syncs gone.md
        (vault / "gone.md").unlink()

        refusing = _Factory(refuse_from=1)
        state = _WatcherState()
        state.stop_event.set()
        state.owed = OwedJournal(journal, vault)
        link = ReconnectingConnection(
            refusing,
            label="worker",
            sleep=lambda _d: None,
            stopping=state.stop_event.is_set,
            conn=_Dead(),  # type: ignore[arg-type]
        )
        state.jobs.put(_Job("delete", vault / "gone.md"))
        state.jobs.put(_Job("upsert", vault / "other.md"))
        state.jobs.put(None)
        _worker_loop(link, fake_embedder, WatchConfig(vault_path=vault, debounce_ms=10), state)
        assert _journal_entries(journal) == [{"action": "delete", "path": "gone.md"}]

        _Watcher(vault, journal, fake_embedder, factory).stop()
    finally:
        factory.close_all()

    assert _row_at(test_db, "gone.md") is None, "the unapplied delete was not redone"


class _DiesAfterTheDelete:
    """A real connection whose ``DELETE FROM documents`` commits, then the reply is lost."""

    def __init__(self, inner: psycopg.Connection[Any]) -> None:
        self._inner = inner
        self.dead = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def closed(self) -> bool:
        return self.dead or self._inner.closed

    def execute(self, query: Any, params: Any = None) -> Any:
        cursor = self._inner.execute(query, params)
        if str(query).startswith("DELETE FROM documents"):
            self.dead = True
            raise psycopg.OperationalError("synthetic: the server closed the connection")
        return cursor

    def close(self) -> None:
        self._inner.close()


def test_the_rerun_of_a_committed_delete_removes_the_age_vertex(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """Criterion 4 (H-4a), on the AGE test instance."""
    _seed_directory(test_db, [("ana", "ana@example.com"), ("ben", "ben@example.com")])
    syncer = _enabled_syncer()
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc("m1", [("ana", "ana@example.com"), ("ben", "ben@example.com")]),
        source_kind="gmail",
        source_external_id="m1",
        graph_syncer=syncer,
    )
    assert result.document_id is not None
    test_db.execute(
        "UPDATE documents SET kind = 'vault', vault_path = 'gone.md' WHERE id = %s",
        (result.document_id,),
    )
    assert _age_document_count(test_db) == 1, "fixture: no AGE Document vertex"
    vault = tmp_path / "vault"
    vault.mkdir()
    factory = _Factory()
    inner = factory()
    inner.autocommit = True  # the wrapper's DELETE must COMMIT before its reply is lost
    first = _DiesAfterTheDelete(inner)
    state = _WatcherState()
    link = ReconnectingConnection(
        factory, label="worker", sleep=lambda _d: None, stopping=lambda: False,
        conn=first,  # type: ignore[arg-type]
    )
    try:
        state.jobs.put(_Job("delete", vault / "gone.md"))
        state.jobs.put(None)
        _worker_loop(
            link, fake_embedder, WatchConfig(vault_path=vault, debounce_ms=10), state, syncer
        )
    finally:
        link.close()
        factory.close_all()

    assert len(factory.made) == 2, "fixture: the delete was not re-run on a fresh connection"
    assert _row_at(test_db, "gone.md") is None
    assert _age_document_count(test_db) == 0, "the re-run left a ghost AGE Document vertex"


class _Remover:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.reconciled: list[str] = []

    def reconcile(self, conn: Any, document_id: str) -> None:
        self.reconciled.append(document_id)

    def remove(self, conn: Any, document_id: str) -> None:
        self.removed.append(document_id)


def test_graph_removes_the_lane_could_not_apply_are_replayed(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """H-4: the lane cannot reach the DB at shutdown; its remove is owed, then applied.

    Calls 1 and 2 are the startup and worker connections; the lane's own
    connect (call 3 on) is refused, so it gives up at shutdown.
    """
    vault, journal = tmp_path / "vault", tmp_path / "state" / JOURNAL_NAME
    doc_id = str(uuid.uuid4())
    _write(vault / "gone.md", "Gone", doc_id=doc_id)
    factory = _Factory(refuse_from=3)
    try:
        first = _Watcher(vault, journal, fake_embedder, factory, graph_syncer=_Remover())
        (vault / "gone.md").unlink()
        first.observer.handler.on_any_event(FileDeletedEvent(str(vault / "gone.md")))
        wait_for(lambda: _row_at(test_db, "gone.md") is None)
        first.stop()
        assert {"action": "graph_remove", "id": doc_id} in _journal_entries(journal)

        factory.refuse_from = None
        graph = _Remover()
        second = _Watcher(vault, journal, fake_embedder, factory, graph_syncer=graph)
        wait_for(lambda: doc_id in graph.removed)
        second.stop()
    finally:
        factory.close_all()


# --------------------------------------------------------------------------- #
# Round 2 (PR #95 review)
# --------------------------------------------------------------------------- #


def test_removes_a_replay_queued_survive_a_setup_failure(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """M1: the replay rotated the journal and queued a remove on the unstarted lane.

    Then the worker's connect (the second ``conn_factory`` call) fails, before
    the stop-wait whose ``finally`` journals the lane's leftovers. The remove
    must be owed again, not lost with the rotated journal.
    """
    vault, journal = tmp_path / "vault", tmp_path / "state" / JOURNAL_NAME
    vault.mkdir()
    owed_id = str(uuid.uuid4())
    OwedJournal(journal, vault).owe_graph_removes([owed_id])
    factory = _Factory(refuse_from=2)
    try:
        with pytest.raises(psycopg.OperationalError):
            run_watcher(
                factory,
                embedder=fake_embedder,
                config=WatchConfig(vault_path=vault, debounce_ms=10),
                observer_factory=_Observer,  # type: ignore[arg-type]
                install_signal_handlers=False,
                graph_syncer=_Remover(),
                owed_journal=journal,
            )
    finally:
        factory.close_all()

    assert journal.exists(), "the queued remove was lost with the rotated journal"
    assert {"action": "graph_remove", "id": owed_id} in _journal_entries(journal)


class _RemoveOnly:
    """The real graph syncer's ``remove``; ``reconcile`` is a no-op, so the count is exact."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def reconcile(self, conn: Any, document_id: str) -> None:
        return None

    def remove(self, conn: Any, document_id: str) -> None:
        self._inner.remove(conn, document_id)


def _age_vault_row(conn: psycopg.Connection[Any], embedder: Any, vault_path: str) -> Any:
    """A vault row at ``vault_path`` with an AGE ``Document`` vertex; returns the syncer."""
    _seed_directory(conn, [("ana", "ana@example.com"), ("ben", "ben@example.com")])
    syncer = _enabled_syncer()
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=_gmail_doc("m1", [("ana", "ana@example.com"), ("ben", "ben@example.com")]),
        source_kind="gmail",
        source_external_id="m1",
        graph_syncer=syncer,
    )
    assert result.document_id is not None
    conn.execute(
        "UPDATE documents SET kind = 'vault', vault_path = %s WHERE id = %s",
        (vault_path, result.document_id),
    )
    assert _age_document_count(conn) == 1, "fixture: no AGE Document vertex"
    return syncer


def _run_dying(vault: Path, embedder: Any, job: _Job, syncer: Any) -> _Factory:
    """Run ``job`` on a connection whose first ``DELETE FROM documents`` commits, then dies."""
    factory = _Factory()
    inner = factory()
    inner.autocommit = True
    state = _WatcherState()
    link = ReconnectingConnection(
        factory, label="worker", sleep=lambda _d: None, stopping=lambda: False,
        conn=_DiesAfterTheDelete(inner),  # type: ignore[arg-type]
    )
    try:
        state.jobs.put(job)
        state.jobs.put(None)
        _worker_loop(
            link, embedder, WatchConfig(vault_path=vault, debounce_ms=10), state,
            _RemoveOnly(syncer),
        )
        assert (state.errors, len(factory.made)) == (0, 2), "fixture: no clean re-run"
    finally:
        link.close()
        factory.close_all()
    return factory


def test_a_move_whose_destination_is_gone_removes_the_vertex_on_rerun(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """M2 (H-4a): the move's destination-gone branch deletes its source row."""
    syncer = _age_vault_row(test_db, fake_embedder, "src.md")
    vault = tmp_path / "vault"
    vault.mkdir()

    _run_dying(vault, fake_embedder, _Job("move", vault / "src.md", vault / "dst.md"), syncer)

    assert _row_at(test_db, "src.md") is None
    assert _age_document_count(test_db) == 0, "the re-run left a ghost AGE Document vertex"


def test_a_move_onto_another_document_removes_the_vertex_on_rerun(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """M2 (H-4a): the re-run no longer sees "another document" and takes the rename path."""
    syncer = _age_vault_row(test_db, fake_embedder, "src.md")
    vault = tmp_path / "vault"
    _write(vault / "dst.md", "Another document", doc_id=str(uuid.uuid4()))

    _run_dying(vault, fake_embedder, _Job("move", vault / "src.md", vault / "dst.md"), syncer)

    assert _row_at(test_db, "src.md") is None
    assert _row_at(test_db, "dst.md") is not None, "the destination was not synced"
    assert _age_document_count(test_db) == 0, "the re-run left a ghost AGE Document vertex"


class _BlockingEmbedder:
    """Blocks its first ``embed`` until ``release``; delegates everything else."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.dim = inner.dim
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        if not self.entered.is_set():
            self.entered.set()
            self.release.wait(WAIT_FOR_BACKSTOP_S * 3)
        return self._inner.embed(texts, input_type=input_type)

    def count_tokens(self, text: str) -> int:
        return int(self._inner.count_tokens(text))


def test_a_worker_past_its_join_owes_its_queued_deletes(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L4: the worker is still busy after its join; the delete queued behind it is owed."""
    from watchdog.events import FileCreatedEvent

    monkeypatch.setattr("brain.vault.watch._WORKER_JOIN_TIMEOUT_S", 0.05)
    vault, journal = tmp_path / "vault", tmp_path / "state" / JOURNAL_NAME
    vault.mkdir()
    embedder = _BlockingEmbedder(fake_embedder)
    factory = _Factory()
    before = set(threading.enumerate())
    worker: threading.Thread | None = None
    try:
        watcher = _Watcher(vault, journal, embedder, factory)
        worker = next(
            t for t in threading.enumerate()
            if t.name == "brain-vault-watcher-worker" and t not in before
        )
        _write(vault / "busy.md", "Busy")
        watcher.observer.handler.on_any_event(FileCreatedEvent(str(vault / "busy.md")))
        assert embedder.entered.wait(WAIT_FOR_BACKSTOP_S), "fixture: the worker never blocked"
        watcher.observer.handler.on_any_event(FileDeletedEvent(str(vault / "queued.md")))
        state = watcher.observer.handler._state
        wait_for(lambda: state.jobs.qsize() >= 1)
        watcher.stop()
        entries = _journal_entries(journal) if journal.exists() else []
    finally:
        embedder.release.set()
        if worker is not None:
            wait_for(lambda: not worker.is_alive())
        factory.close_all()

    assert {"action": "delete", "path": "queued.md"} in entries, entries
