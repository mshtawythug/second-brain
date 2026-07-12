"""Unit tests for the vault watcher.

These tests exercise ``brain.vault.watch.run_watcher`` against a real
test DB connection but with a fake watchdog ``Observer`` so we never
actually rely on the OS filesystem-events subsystem during a test run.
The fake observer exposes ``inject(event)`` so tests can script the
exact sequence of events they want to verify.

Conventions:

- ``WatchConfig(debounce_ms=10)`` keeps tests sub-second; we still wait
  on explicit synchronization points (queue.join() / threading.Events)
  rather than ``time.sleep`` to avoid flakiness.
- ``install_signal_handlers=False`` is set everywhere — the test itself
  drives shutdown via ``state.stop_event.set()`` so pytest's signal
  handling stays untouched.
- Each test starts the watcher in a background thread and asserts on
  observations from a thread-safe collector.
"""
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from brain.vault.derived_links.fence import (
    FENCE_END_MARKER,
    FENCE_START_MARKER,
    strip_fence,
)
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import sync_vault
from brain.vault.watch import (
    WatchConfig,
    _classify_event,
    _filter_path,
    _handle_delete,
    _Job,
    _WatcherState,
    _worker_loop,
    run_watcher,
)

# ---------------------------------------------------------------------------
# Test doubles for watchdog Observer.
# ---------------------------------------------------------------------------


class _FakeObserver:
    """In-process stand-in for ``watchdog.observers.Observer``.

    Captures the handler passed to ``schedule`` so tests can later
    inject events directly via ``inject(event)``. Mirrors the
    real Observer's ``start/stop/join`` surface but does no real work.
    """

    instances: list["_FakeObserver"] = []

    def __init__(self) -> None:
        self.scheduled: list[tuple[Any, str, bool]] = []
        self.handler: Any = None
        self.started = False
        self.stopped = False
        _FakeObserver.instances.append(self)

    def schedule(self, handler: Any, path: str, recursive: bool = False) -> None:
        self.handler = handler
        self.scheduled.append((handler, path, recursive))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        return None

    # Test-only helper: route an event through the registered handler.
    def inject(self, event: Any) -> None:
        assert self.handler is not None, "schedule() must be called first"
        self.handler.on_any_event(event)


@pytest.fixture(autouse=True)
def _reset_fake_observers() -> None:
    """Clear the per-test FakeObserver registry."""
    _FakeObserver.instances.clear()


# ---------------------------------------------------------------------------
# Helpers to drive the watcher in a background thread.
# ---------------------------------------------------------------------------


def _write(path: Path, fields: dict[str, Any], body: str) -> None:
    """Write a vault note with the given frontmatter + body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _start_watcher(
    *,
    conn_factory: Any,
    embedder: Any,
    config: WatchConfig,
) -> tuple[threading.Thread, queue.Queue[Any]]:
    """Run ``run_watcher`` in a background thread.

    Returns the thread and a queue that captures (return_value, exception)
    when the thread exits, so the test can join + assert.
    """
    result_q: queue.Queue[Any] = queue.Queue()

    def _target() -> None:
        try:
            report = run_watcher(
                conn_factory,
                embedder=embedder,
                config=config,
                observer_factory=_FakeObserver,
                install_signal_handlers=False,
            )
            result_q.put(("ok", report))
        except Exception as exc:  # pragma: no cover — surfaced to test
            result_q.put(("err", exc))

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, result_q


def _wait_for_observer(timeout: float = 1.0) -> _FakeObserver:
    """Block until the watcher has registered its FakeObserver."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _FakeObserver.instances and _FakeObserver.instances[0].started:
            return _FakeObserver.instances[0]
        time.sleep(0.01)
    raise AssertionError("watcher did not start observer in time")


def _wait_for(predicate: Any, timeout: float = 2.0) -> None:
    """Block until ``predicate()`` returns truthy or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("predicate never became true")


# ---------------------------------------------------------------------------
# _classify_event / _filter_path — pure function tests.
# ---------------------------------------------------------------------------


def test_classify_skips_directory_events(tmp_path: Path) -> None:
    """Directory events have no per-file action."""
    from watchdog.events import DirCreatedEvent

    event = DirCreatedEvent(str(tmp_path / "subdir"))
    assert _classify_event(event, tmp_path) is None


def test_classify_skips_non_md(tmp_path: Path) -> None:
    """Non-Markdown files (editor temp files, .DS_Store) are filtered."""
    event = FileModifiedEvent(str(tmp_path / "x.swp"))
    assert _classify_event(event, tmp_path) is None


def test_classify_skips_templates(tmp_path: Path) -> None:
    event = FileModifiedEvent(str(tmp_path / "_templates" / "daily.md"))
    assert _classify_event(event, tmp_path) is None


def test_classify_skips_attachments(tmp_path: Path) -> None:
    event = FileModifiedEvent(str(tmp_path / "_attachments" / "x.md"))
    assert _classify_event(event, tmp_path) is None


def test_classify_skips_hidden_dirs(tmp_path: Path) -> None:
    """``.git/``, ``.obsidian/`` etc. should never produce events."""
    event = FileModifiedEvent(str(tmp_path / ".git" / "HEAD.md"))
    assert _classify_event(event, tmp_path) is None


def test_classify_created_yields_upsert(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hi")
    event = FileCreatedEvent(str(target))
    classified = _classify_event(event, tmp_path)
    assert classified is not None
    [(action, path, dest)] = classified
    assert action == "upsert"
    assert path.name == "note.md"
    assert dest is None


def test_classify_deleted_yields_delete(tmp_path: Path) -> None:
    event = FileDeletedEvent(str(tmp_path / "gone.md"))
    classified = _classify_event(event, tmp_path)
    assert classified is not None
    [(action, _path, dest)] = classified
    assert action == "delete"
    assert dest is None


def test_classify_moved_within_vault_yields_single_move(tmp_path: Path) -> None:
    """A within-vault rename is threaded as a single ``move`` (src + dst) so
    the worker can UPDATE vault_path in place and preserve incoming links."""
    src = tmp_path / "old.md"
    dst = tmp_path / "new.md"
    event = FileMovedEvent(str(src), str(dst))
    classified = _classify_event(event, tmp_path)
    assert classified is not None
    assert len(classified) == 1
    (action, path, dest) = classified[0]
    assert action == "move"
    assert path.name == "old.md"
    assert dest is not None and dest.name == "new.md"


def test_classify_moved_outside_vault_skips_dst(tmp_path: Path) -> None:
    """If a file is moved out of the vault, the destination is skipped
    but the source still becomes a delete event (no ``move`` threading)."""
    src = tmp_path / "old.md"
    dst = tmp_path / "_attachments" / "new.md"
    event = FileMovedEvent(str(src), str(dst))
    classified = _classify_event(event, tmp_path)
    assert classified is not None
    actions = [a for a, *_ in classified]
    assert actions == ["delete"]


def test_filter_path_handles_bytes(tmp_path: Path) -> None:
    """Inotify can surface ``bytes`` paths — we must accept them."""
    target = tmp_path / "note.md"
    target.write_text("hi")
    encoded = os.fsencode(str(target))
    event = FileCreatedEvent(encoded)
    classified = _classify_event(event, tmp_path)
    assert classified is not None
    [(_, path, _dest)] = classified
    assert path.name == "note.md"


def test_filter_path_outside_vault(tmp_path: Path) -> None:
    """A path outside the vault returns None."""
    other = tmp_path.parent / "other.md"
    assert _filter_path(other, tmp_path) is None


# ---------------------------------------------------------------------------
# Integration tests: drive the watcher with a real DB + fake observer.
# ---------------------------------------------------------------------------


def _conn_factory(test_db: psycopg.Connection) -> Any:
    """Build a conn_factory that returns a *new* connection on each call.

    The watcher needs its own connection separate from ``test_db`` (which
    pytest will close at the end of the test). We import the test DB
    URL inside the helper to keep the import near the use-site.
    """
    from brain.db import connect_raw
    from tests.conftest import TEST_DATABASE_URL

    def _make() -> psycopg.Connection:
        return connect_raw(TEST_DATABASE_URL)

    return _make


def test_single_create_event_triggers_one_sync(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A single create event after debounce results in exactly one sync."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "n.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Hello"}, "world\n")

    events: list[tuple[str, Path]] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10,
        on_event=lambda action, path: events.append((action, path)),
    )

    # Empty out the file first so the startup sync sees nothing — we
    # want to exclusively measure the post-startup event path.
    note.unlink()

    thread, result_q = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    # Recreate the file then inject a created event for it.
    _write(note, {"id": note_id, "title": "Hello"}, "world\n")
    observer.inject(FileCreatedEvent(str(note)))

    # Wait for the debounced job to enqueue + the worker to consume it.
    _wait_for(lambda: len(events) == 1)
    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 1
    )

    # Shut down the watcher.
    from brain.vault.watch import _WatcherState  # noqa: PLC0415

    # Reach into the running watcher: signal stop via the same Event the
    # signal handler would set. We do this by accessing the FakeObserver's
    # handler, which holds a back-reference to state.
    assert observer.handler is not None
    state: _WatcherState = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    kind, payload = result_q.get_nowait()
    assert kind == "ok", payload
    assert events == [("upsert", note)]


def test_burst_of_modifies_collapses_to_one_sync(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """5 rapid events on the same path should produce one job."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "burst.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Burst"}, "v1\n")

    events: list[tuple[str, Path]] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=50,
        on_event=lambda action, path: events.append((action, path)),
    )

    thread, _result_q = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    # Inject 5 modifies in rapid succession — each one resets the timer,
    # so only the LAST should actually fire.
    for _ in range(5):
        observer.inject(FileModifiedEvent(str(note)))

    _wait_for(lambda: len(events) >= 1, timeout=2.0)
    # Allow a little slack for any spurious extras to arrive.
    time.sleep(0.1)
    assert len(events) == 1, f"expected 1 collapsed event, got {events!r}"

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_multi_path_bursts_yield_one_sync_per_path(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Two paths each getting bursts produce exactly two enqueued jobs."""
    vault = tmp_path / "vault"
    vault.mkdir()
    a = vault / "a.md"
    b = vault / "b.md"
    _write(a, {"id": str(uuid.uuid4()), "title": "A"}, "x\n")
    _write(b, {"id": str(uuid.uuid4()), "title": "B"}, "y\n")

    events: list[tuple[str, Path]] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=50,
        on_event=lambda action, path: events.append((action, path)),
    )

    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    for _ in range(3):
        observer.inject(FileModifiedEvent(str(a)))
    for _ in range(3):
        observer.inject(FileModifiedEvent(str(b)))

    _wait_for(lambda: len(events) >= 2, timeout=2.0)
    time.sleep(0.1)
    assert len(events) == 2
    paths = sorted(p.name for _, p in events)
    assert paths == ["a.md", "b.md"]

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_watcher_ignores_template_events(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Events under ``_templates/`` never enqueue jobs."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "_templates").mkdir()
    template = vault / "_templates" / "daily.md"
    template.write_text("# {{date}}")

    events: list[Any] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10,
        on_event=lambda a, p: events.append((a, p)),
    )

    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    observer.inject(FileModifiedEvent(str(template)))
    time.sleep(0.1)
    assert events == []

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_watcher_ignores_non_md_files(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Non-``.md`` files never enqueue jobs."""
    vault = tmp_path / "vault"
    vault.mkdir()
    swap = vault / "note.md.swp"
    swap.write_text("vim swap")

    events: list[Any] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10,
        on_event=lambda a, p: events.append((a, p)),
    )

    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    observer.inject(FileModifiedEvent(str(swap)))
    time.sleep(0.1)
    assert events == []

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_delete_event_removes_row(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A delete event causes the worker to remove the document row."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "doomed.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Doomed"}, "x\n")

    events: list[Any] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10,
        on_event=lambda a, p: events.append((a, p)),
    )

    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    # Confirm startup sync inserted the row.
    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 1
    )

    note.unlink()
    observer.inject(FileDeletedEvent(str(note)))

    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 0,
        timeout=2.0,
    )
    assert events == [("delete", note)]

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_stale_delete_event_preserves_existing_file(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A spurious 'deleted' fsevent for a still-existing file must NOT remove the row.

    Repro path: ``rewrite_tags`` (called by ``brain tag``) uses
    ``_atomic.atomic_write_text`` which does ``os.replace(tmp, path)``.
    On macOS APFS, fsevents can surface this rename as a ``deleted`` event
    on the original path without a corresponding ``created``/``modified``
    follow-up — especially under timing pressure when watchdog's
    deduplication coalesces events. The on-disk file is fully intact, but
    the previous worker would blindly call ``DELETE FROM documents`` based
    on the action label alone.

    Regression: live test on 2026-05-01 confirmed ``brain tag <vault-doc>
    +foo`` could leave the file written but the DB row gone, causing the
    next ``brain tag <id> -foo`` to fail with ``document not found``.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "still-here.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Still Here"}, "x\n")

    events: list[Any] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10,
        on_event=lambda a, p: events.append((a, p)),
    )

    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    # Confirm startup sync inserted the row.
    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 1
    )

    # The file is still on disk — DO NOT unlink. This is the difference
    # vs. ``test_delete_event_removes_row``: a real deletion follows
    # ``note.unlink()``; a stale deletion does not.
    assert note.exists(), "test setup must leave the file in place"

    observer.inject(FileDeletedEvent(str(note)))

    # Wait for the worker to drain the event queue. The on_event hook
    # fires when a job is enqueued (post-debounce); poll for it before
    # asserting on DB state so we don't race the worker.
    _wait_for(lambda: ("delete", note) in events, timeout=2.0)

    # Give the worker a brief moment to actually process the dequeued
    # job. We can't sync on the row's deletion because it shouldn't
    # happen, so a short bounded wait is the right shape here.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        time.sleep(0.02)

    # The fix: stale delete events must NOT remove the row.
    count = test_db.execute(
        "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
    ).fetchone()[0]
    assert count == 1, (
        f"stale delete event removed the row despite the file still existing "
        f"(count={count}); watcher should re-stat the path before DELETE"
    )

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_stale_upsert_event_for_vanished_file(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A spurious 'created' fsevent for a vanished file must be a no-op.

    Symmetric to the delete-side fix in ``ecef181``. Without this guard
    the worker calls ``sync_one_file`` which raises ``FileNotFoundError``,
    which the outer ``try/except Exception`` catches and counts as
    ``state.errors``. That's noisy + cluttering: we processed an event for
    a path that doesn't exist on disk; nothing is wrong, just stale.

    Trigger scenarios in the wild: editor atomic-save creating + removing a
    temp file inside the debounce window, ``brain note new`` immediately
    followed by ``brain rm``, or any tool that creates + removes within the
    same debounce interval and races the worker.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    # Path that has NEVER existed on disk — the whole point.
    ghost = vault / "phantom.md"
    assert not ghost.exists(), "test setup invariant: file must not exist"

    events: list[Any] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10,
        on_event=lambda a, p: events.append((a, p)),
    )

    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    # Snapshot processed counter BEFORE we inject — startup sync may have
    # incremented it for the (empty) initial scan, and we want a delta.
    state = observer.handler._state
    processed_before = state.processed

    # Inject a "created" event for a path that doesn't exist. The previous
    # behavior here was: enqueue → worker calls sync_one_file → FileNotFoundError
    # → except → state.errors += 1. The fix: re-stat before sync, treat
    # nonexistence as a no-op.
    observer.inject(FileCreatedEvent(str(ghost)))

    # Wait for the worker to dequeue the job. The on_event hook fires when a
    # job is enqueued (post-debounce); we poll for that, then bound-wait for
    # the worker to finish acting on it. We can't sync on a DB row appearing
    # (it shouldn't) or on processed advancing (it shouldn't either, per the
    # impl decision to ``continue``), so the bounded wait is the right shape.
    _wait_for(lambda: ("upsert", ghost) in events, timeout=2.0)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        time.sleep(0.02)

    # The fix: stale upsert for a vanished file must NOT raise + be caught.
    assert state.errors == 0, (
        f"stale upsert event inflated state.errors (={state.errors}); "
        f"watcher should re-stat the path before sync_one_file"
    )

    # And it must NOT have written anything to the DB.
    count = test_db.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert count == 0, (
        f"stale upsert event produced a spurious DB row (count={count}); "
        f"watcher should skip when the file does not exist"
    )

    # And per impl decision: state.processed must NOT advance for a stale
    # event. A vanished file isn't meaningful work, neither processed nor
    # errored — it's a phantom event that should leave the counters alone.
    assert state.processed == processed_before, (
        f"state.processed advanced for a stale upsert "
        f"({processed_before} → {state.processed}); the worker should "
        f"``continue`` without counting a vanished file as work"
    )

    state.stop_event.set()
    thread.join(timeout=5.0)


def test_graceful_shutdown_drains_pending(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Pending debounce timers must flush BEFORE shutdown returns.

    The watcher cancels any in-flight timers on stop but immediately
    enqueues an upsert for each pending path, so the user's last edit
    is never lost.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "drain.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Drain"}, "v1\n")

    events: list[Any] = []
    # Long debounce so we KNOW the timer is still pending when we stop.
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10_000,
        on_event=lambda a, p: events.append((a, p)),
    )

    thread, result_q = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    # Update the file body, fire one event — debounce won't fire for 10s.
    _write(note, {"id": note_id, "title": "Drain"}, "v2-updated\n")
    observer.inject(FileModifiedEvent(str(note)))

    _wait_for(
        lambda: len(observer.handler._state.pending_timers) == 1,
        timeout=1.0,
    )

    # Stop now — the drain logic should still flush the pending edit.
    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    # Drain produced exactly one event, and the DB row has the new body.
    assert events == [("upsert", note)]
    row = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    assert "v2-updated" in row[0]

    kind, _ = result_q.get_nowait()
    assert kind == "ok"


def test_signal_handlers_are_restored_on_exit(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """With ``install_signal_handlers=True``, the previous SIGINT handler
    must be restored after the watcher exits.

    We do NOT verify actual signal delivery here — pytest's main thread
    owns Python's signal infrastructure and we cannot safely raise
    SIGINT from a test thread. Instead we drive shutdown via the
    ``stop_event`` (the same path the production handler uses) and
    assert that ``signal.getsignal(SIGINT)`` afterwards is something
    other than the watcher's private closure — i.e. the prior handler
    was reinstalled.
    """
    import signal

    vault = tmp_path / "vault"
    vault.mkdir()

    config = WatchConfig(vault_path=vault, debounce_ms=10)

    result_q: queue.Queue[Any] = queue.Queue()

    def _target() -> None:
        try:
            run_watcher(
                _conn_factory(test_db),
                embedder=fake_embedder,
                config=config,
                observer_factory=_FakeObserver,
                install_signal_handlers=True,
            )
            result_q.put("ok")
        except Exception as exc:  # pragma: no cover
            result_q.put(("err", exc))

    # signal.signal() must run on the main thread, so we install handlers
    # FROM the watcher thread but trigger them by raising a signal in our
    # own process. Python's signal delivery routes to the main thread
    # regardless — but we drive shutdown via the threading.Event so we
    # can verify the handler installation path without OS-level signals.
    thread = threading.Thread(target=_target, daemon=True)
    thread.start()

    observer = _wait_for_observer(timeout=2.0)
    state = observer.handler._state

    # The handler was installed (we don't directly send a signal — the
    # main thread under pytest holds Python's signal infra). Drive
    # shutdown the same way the handler would.
    state.stop_event.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    # Verify previous handlers were restored. After the watcher exits,
    # the SIGINT handler must NOT be our internal closure anymore — the
    # default (or whatever pytest had installed) should be back.
    current = signal.getsignal(signal.SIGINT)
    # Just confirm it's not our private closure (which has the name
    # "_signal_handler"); pytest's handler is fine.
    name = getattr(current, "__name__", "")
    assert name != "_signal_handler"


def test_overflow_triggers_full_sync(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """When the debounce buffer overflows, a full sync is queued."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "over.md"
    _write(note, {"id": str(uuid.uuid4()), "title": "Over"}, "x\n")

    # Patch the cap down to a tiny number so we can hit overflow easily.
    mocker.patch("brain.vault.watch._MAX_PENDING", 2)

    events: list[Any] = []
    # Long debounce so timers don't fire on their own — we want to
    # observe overflow in the buffer, not normal flushing.
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10_000,
        on_event=lambda a, p: events.append((a, p)),
    )

    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    # Fire 3 modifies on 3 different paths → overflow at the third.
    for i in range(3):
        path = vault / f"f{i}.md"
        path.write_text(f"# {i}")
        observer.inject(FileModifiedEvent(str(path)))

    _wait_for(
        lambda: any(p == vault for _, p in events),
        timeout=2.0,
    )
    overflow = [(a, p) for a, p in events if p == vault]
    assert overflow == [("upsert", vault)]

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_worker_swallows_per_file_errors(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A single bad file must not kill the worker — subsequent events
    still flow through."""
    vault = tmp_path / "vault"
    vault.mkdir()
    bad = vault / "bad.md"
    bad.write_text("---\nfoo: [unclosed\n---\nbody\n")
    good = vault / "good.md"
    good_id = str(uuid.uuid4())

    events: list[Any] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10,
        on_event=lambda a, p: events.append((a, p)),
    )

    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    observer.inject(FileModifiedEvent(str(bad)))
    _wait_for(lambda: len(events) >= 1, timeout=2.0)
    # Now write a healthy file and verify the worker is still alive.
    _write(good, {"id": good_id, "title": "Good"}, "y\n")
    observer.inject(FileCreatedEvent(str(good)))

    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (good_id,)
        ).fetchone()[0]
        == 1,
        timeout=2.0,
    )

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_on_any_event_swallows_classify_errors(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A misbehaving event must not kill the observer thread.

    We hand the handler an object that makes ``event_type`` raise. The
    handler's try/except in ``on_any_event`` should log + continue.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    config = WatchConfig(vault_path=vault, debounce_ms=10)

    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    # Build a sentinel event whose ``event_type`` access throws.
    class _Boom:
        is_directory = False

        @property
        def event_type(self) -> str:
            raise RuntimeError("boom")

        src_path = "/tmp/whatever.md"

    observer.handler.on_any_event(_Boom())  # must not raise
    # The observer thread is still alive — schedule a real event and
    # confirm it processes.
    note = vault / "after.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "After"}, "x\n")
    from watchdog.events import FileCreatedEvent  # noqa: PLC0415

    observer.inject(FileCreatedEvent(str(note)))
    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 1,
        timeout=2.0,
    )

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_filter_path_with_vanished_file_uses_lexical_fallback(
    tmp_path: Path,
) -> None:
    """``_filter_path`` must work for delete events whose target is gone.

    Resolve raises FileNotFoundError on a vanished path; the lexical
    fallback (``relative_to`` without ``resolve()``) must still produce
    the correct relative path.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    gone = vault / "gone.md"  # never created
    out = _filter_path(gone, vault)
    assert out == gone


def test_filter_path_handles_resolve_oserror(
    tmp_path: Path, mocker: Any
) -> None:
    """When ``Path.resolve()`` raises OSError, the lexical fallback runs.

    macOS / Linux can surface OSError from ``resolve()`` on certain
    filesystems (broken symlinks, EACCES on parent dir, etc). We patch
    ``Path.resolve`` directly to ensure the fallback path is exercised
    deterministically rather than depending on filesystem quirks.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "x.md"

    real_resolve = Path.resolve

    def _flaky_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == target:
            raise OSError("simulated resolve failure")
        return real_resolve(self, *args, **kwargs)

    mocker.patch.object(Path, "resolve", _flaky_resolve)
    out = _filter_path(target, vault)
    # Lexical fallback succeeds — same result as the resolve path would
    # have produced.
    assert out == target


def test_filter_path_outside_vault_oserror_returns_none(
    tmp_path: Path, mocker: Any
) -> None:
    """OSError + outside-vault path → both paths return None."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "elsewhere.md"

    def _flaky_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        raise OSError("simulated resolve failure")

    mocker.patch.object(Path, "resolve", _flaky_resolve)
    assert _filter_path(outside, vault) is None


def test_filter_path_empty_relative_returns_none(tmp_path: Path) -> None:
    """An empty relative path is rejected.

    Reaching the empty-parts branch in production requires the absolute
    path to resolve identical to the vault path itself — which never
    happens for files. We assert the contract directly.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    out = _filter_path(vault, vault)
    # vault is itself the vault root → empty parts → None.
    assert out is None


def test_on_event_hook_exceptions_are_swallowed(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A misbehaving on_event hook must not crash the worker."""
    vault = tmp_path / "vault"
    vault.mkdir()

    def _bad_hook(_action: str, _path: Path) -> None:
        raise RuntimeError("boom")

    note = vault / "n.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "N"}, "x\n")

    config = WatchConfig(vault_path=vault, debounce_ms=10, on_event=_bad_hook)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    from watchdog.events import FileModifiedEvent  # noqa: PLC0415

    observer.inject(FileModifiedEvent(str(note)))
    # Worker still picks up the job even though the hook raised.
    _wait_for(
        lambda: observer.handler._state.processed >= 1,
        timeout=2.0,
    )

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_worker_logs_and_continues_on_sync_exception(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """If ``sync_one_file`` raises, the worker logs and stays alive.

    Patches ``sync_one_file`` to raise once, then succeed. After the
    failure the worker should still process the next job.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "n.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "N"}, "x\n")

    calls: list[Path] = []
    real = __import__(
        "brain.vault.watch", fromlist=["sync_one_file"]
    ).sync_one_file

    def _flaky(*args: Any, **kwargs: Any) -> Any:
        path = kwargs.get("file_path")
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("simulated failure")
        return real(*args, **kwargs)

    mocker.patch("brain.vault.watch.sync_one_file", _flaky)

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    from watchdog.events import FileModifiedEvent  # noqa: PLC0415

    # First event triggers the simulated failure; the worker stays alive.
    observer.inject(FileModifiedEvent(str(note)))
    _wait_for(lambda: observer.handler._state.errors >= 1, timeout=2.0)

    # Second event must succeed — proves the worker didn't die.
    observer.inject(FileModifiedEvent(str(note)))
    _wait_for(lambda: observer.handler._state.processed >= 1, timeout=2.0)

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_overflow_full_sync_is_dispatched_to_worker(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """The overflow-recovery branch in ``_worker_loop`` runs ``sync_vault``.

    Drives an overflow + waits for the worker to dispatch the synthetic
    full-sync job; covers the ``job.abs_path == config.vault_path`` arm.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "a.md", {"id": str(uuid.uuid4()), "title": "A"}, "x\n")

    mocker.patch("brain.vault.watch._MAX_PENDING", 1)

    sync_called: list[Path] = []
    real = __import__("brain.vault.watch", fromlist=["sync_vault"]).sync_vault

    def _spy(*args: Any, **kwargs: Any) -> Any:
        sync_called.append(kwargs.get("vault_path"))
        return real(*args, **kwargs)

    mocker.patch("brain.vault.watch.sync_vault", _spy)

    config = WatchConfig(vault_path=vault, debounce_ms=10_000)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    # Each modified path adds a pending timer; second one overflows.
    from watchdog.events import FileModifiedEvent  # noqa: PLC0415

    for i in range(3):
        path = vault / f"f{i}.md"
        path.write_text("# x")
        observer.inject(FileModifiedEvent(str(path)))

    _wait_for(lambda: len(sync_called) >= 2, timeout=2.0)
    # First call was the startup sync; second is the overflow recovery.

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_run_watcher_install_signal_handlers_real_path(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """Cover the ``install_signal_handlers=True`` branch end-to-end.

    Patches ``signal.signal`` so the test runner's own handlers stay
    intact, then drives shutdown by invoking the captured handler directly.
    The handler's body (``logger.info`` + ``state.stop_event.set()``) is
    the line we want covered.
    """
    import signal  # noqa: PLC0415

    vault = tmp_path / "vault"
    vault.mkdir()

    captured_handlers: list[Any] = []

    def _capture(_sig: int, handler: Any) -> Any:
        captured_handlers.append(handler)
        return signal.SIG_DFL

    mocker.patch("brain.vault.watch.signal.signal", _capture)

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, result_q = _start_watcher_with_signal_handlers(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    _wait_for_observer()  # ensures the watcher has fully started up
    # Two install calls (SIGINT, SIGTERM), each capturing the closure.
    _wait_for(lambda: len(captured_handlers) >= 2, timeout=2.0)

    # Invoking the handler MUST trigger the stop_event — that's the
    # only thing the handler does (besides the log line).
    handler = captured_handlers[0]
    handler(signal.SIGINT, None)

    thread.join(timeout=5.0)
    assert not thread.is_alive()
    kind, _ = result_q.get_nowait()
    assert kind == "ok"


def _start_watcher_with_signal_handlers(
    *, conn_factory: Any, embedder: Any, config: WatchConfig
) -> tuple[threading.Thread, queue.Queue[Any]]:
    """Same as ``_start_watcher`` but with ``install_signal_handlers=True``."""
    result_q: queue.Queue[Any] = queue.Queue()

    def _target() -> None:
        try:
            report = run_watcher(
                conn_factory,
                embedder=embedder,
                config=config,
                observer_factory=_FakeObserver,
                install_signal_handlers=True,
            )
            result_q.put(("ok", report))
        except Exception as exc:  # pragma: no cover
            result_q.put(("err", exc))

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, result_q


def test_enqueue_signature_has_no_bypass_filter(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Phase 5 carryover: ``_enqueue`` must not accept ``bypass_filter``.

    The parameter was dead — its body started with ``del bypass_filter``
    so passing it changed nothing. We removed it in Phase 6; this test
    pins the new signature so a future refactor doesn't accidentally
    re-introduce it.
    """
    import inspect

    from brain.vault.watch import _enqueue

    params = inspect.signature(_enqueue).parameters
    assert "bypass_filter" not in params
    # The expected surviving keyword-only param is ``config``.
    assert "config" in params


def test_worker_join_timeout_skips_close_when_alive(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """Phase 5 carryover: don't close the worker connection when the
    worker thread is still alive past the join timeout.

    The race: ``worker_thread.join(timeout=10)`` returns after 10s; if
    the worker is mid-statement, calling ``worker_conn.close()`` from
    the main thread races against the in-flight statement and the
    worker thread will raise from inside ``sync_one_file`` while we're
    trying to return cleanly.

    Fix verified here: when ``is_alive()`` is True after the join, we
    skip the close + log a warning. The connection becomes the daemon
    thread's responsibility; Postgres reaps it on idle timeout.

    Strategy: replace ``brain.vault.watch.threading.Thread`` with a
    factory that returns a real Thread for everything except the
    watcher's worker thread (matched by name kwarg). For the worker
    thread we return a fake whose ``start`` no-ops, ``join`` no-ops,
    and ``is_alive`` returns True — so the watcher's shutdown path
    runs the join-timeout branch deterministically without an actual
    background worker.
    """
    vault = tmp_path / "vault"
    vault.mkdir()

    real_thread_cls = threading.Thread
    fake_workers: list[_FakeAliveThread] = []

    def _thread_factory(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("name") == "brain-vault-watcher-worker":
            fake = _FakeAliveThread()
            fake_workers.append(fake)
            return fake
        return real_thread_cls(*args, **kwargs)

    mocker.patch("brain.vault.watch.threading.Thread", _thread_factory)

    # Spy on the worker connection's ``close`` so we can verify it
    # wasn't invoked when the thread is still "alive".
    closed: list[bool] = []
    real_connect = __import__("brain.db", fromlist=["connect_raw"]).connect_raw
    from tests.conftest import TEST_DATABASE_URL

    call_n = {"n": 0}

    def _conn_factory() -> Any:
        call_n["n"] += 1
        inner = real_connect(TEST_DATABASE_URL)
        if call_n["n"] == 1:
            # Startup connection — closed inside ``run_watcher``'s
            # finally block before the worker even starts; we don't
            # want that to count.
            return inner
        return _ConnProxy(inner, closed)

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, result_q = _start_watcher(
        conn_factory=_conn_factory,
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    assert len(fake_workers) == 1, "expected one fake worker thread to be created"

    state.stop_event.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    # The connection's close() must NOT have been invoked — that's the
    # point of the fix. The fake worker reports ``is_alive()`` True so
    # we leak the connection rather than race against an in-flight
    # statement.
    assert closed == [], (
        "worker connection was closed despite the worker thread still "
        "being alive — the join-timeout race fix regressed"
    )
    kind, _ = result_q.get_nowait()
    assert kind == "ok"


class _FakeAliveThread:
    """Stand-in for the watcher's worker thread.

    ``start()`` is a no-op (we don't actually want a worker draining
    the queue — this test is about the shutdown path), ``join()`` is a
    no-op (so the watcher's shutdown doesn't block waiting for a
    nonexistent worker), and ``is_alive()`` returns True forever (the
    behavior the join-timeout race fix branches on).
    """

    def start(self) -> None:
        return None

    def join(self, timeout: float | None = None) -> None:
        return None

    def is_alive(self) -> bool:
        return True


class _ConnProxy:
    """Lightweight wrapper that records close() calls without delegating.

    We don't want the real connection actually closed (the test fixture
    cleans it up), but we DO want to know whether the watcher tried to
    close it from the main thread. Recording into an outer list keeps
    the test simple.
    """

    def __init__(self, inner: Any, closed: list[bool]) -> None:
        self._inner = inner
        self._closed_log = closed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        self._closed_log.append(True)
        self._inner.close()


def test_run_watcher_handles_already_closed_connection(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """Cover the ``except psycopg.Error`` branch on shutdown.

    The watcher closes its worker connection at the end. If the close
    raises (e.g. server already dropped the connection), the watcher
    should swallow the error rather than crash on shutdown.
    """
    vault = tmp_path / "vault"
    vault.mkdir()

    config = WatchConfig(vault_path=vault, debounce_ms=10)

    # Patch the watcher's psycopg.connect path so we can return a
    # proxy whose .close() raises. Patching at the watcher's import
    # site keeps test isolation tight.
    real_connect = __import__("brain.db", fromlist=["connect_raw"]).connect_raw

    class _RaisingClose:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def close(self) -> None:
            raise psycopg.OperationalError("simulated already-closed")

    counter = {"n": 0}

    conftest = __import__("tests.conftest", fromlist=["TEST_DATABASE_URL"])
    test_url = conftest.TEST_DATABASE_URL

    def _conn() -> Any:
        counter["n"] += 1
        inner = real_connect(test_url)
        if counter["n"] == 1:
            return inner  # startup conn — closed by run_watcher's try/finally
        return _RaisingClose(inner)

    thread, result_q = _start_watcher(
        conn_factory=_conn,
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)
    kind, _ = result_q.get_nowait()
    assert kind == "ok"


def test_handle_delete_outside_vault_is_a_noop(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A delete event for a path outside the vault is dropped quietly.

    This is reached by ``_filter_path`` when both ``resolve()`` and the
    lexical fallback agree the path isn't inside the vault. The watcher
    must not raise in that case.
    """
    from brain.vault.watch import _handle_delete  # noqa: PLC0415

    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "elsewhere" / "x.md"
    # Should not raise even though the path is outside the vault.
    _handle_delete(test_db, outside, vault)


def test_startup_sync_runs_before_observer_starts(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Files already present when the watcher starts get synced on
    startup, BEFORE any events fire."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "preexisting.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Pre"}, "x\n")

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, result_q = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    # By the time the observer is running, the startup sync has finished.
    row = test_db.execute(
        "SELECT title FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row == ("Pre",)

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)
    kind, report = result_q.get_nowait()
    assert kind == "ok"
    assert report.created == 1


# ---------------------------------------------------------------------------
# Fence-only debounce tests (Phase D, Task D.5).
#
# The metadata-aware linker rewrites a fenced ``BRAIN_DERIVED_*`` section in
# every affected ``_ingested/`` body whenever derived edges are rebuilt, even
# when the rewrite is byte-identical (decision Q4=(b)). Each rewrite fires a
# filesystem event the watcher would normally re-sync — and that re-sync
# would re-rebuild the fence, looping forever. The worker dedups via a
# strip_fence cache; these tests pin the contract.
# ---------------------------------------------------------------------------


def _fenced_body(prose: str, bullets: list[str]) -> str:
    """Build a body with a ``BRAIN_DERIVED_*`` fence appended.

    Mirrors the shape :func:`brain.vault.derived_links.fence.render_fenced_section`
    emits, without going through the renderer (we don't need a real DB
    edge — the test only cares that the fence markers are present and
    nestable in :func:`strip_fence`).
    """
    lines = [
        prose,
        "",
        FENCE_START_MARKER,
        "## Related (auto-generated, do not edit)",
        *bullets,
        FENCE_END_MARKER,
    ]
    return "\n".join(lines) + "\n"


def _make_sync_spy(mocker: Any) -> list[Path]:
    """Patch ``sync_one_file`` with a no-op spy and return the call log.

    Returns the list each call appends ``file_path`` into. We don't
    delegate to the real ``sync_one_file`` because these tests only care
    whether the debounce gate decided to call it — not what it does to
    the DB.
    """
    calls: list[Path] = []

    def _spy(*_args: Any, **kwargs: Any) -> None:
        calls.append(kwargs["file_path"])

    mocker.patch("brain.vault.watch.sync_one_file", _spy)
    return calls


def test_fence_only_write_skips_resync(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """A write that only changes the fence content must NOT trigger sync.

    This is the central watch-loop guard: the linker's fence rewriter
    bumps the file's mtime even on a byte-identical re-render
    (decision Q4=(b)). Without this skip the watcher would re-enter
    sync, which would re-enter the linker, which would rewrite the
    fence, which would fire another event, ad infinitum.
    """
    spy_calls = _make_sync_spy(mocker)
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "fenced.md"
    note_id = str(uuid.uuid4())
    body_v1 = _fenced_body("real body\n", ["- [[a|A]] *(R1)*"])
    _write(note, {"id": note_id, "title": "Fenced"}, body_v1)

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    # Seed the cache as if a successful sync had just observed body_v1 —
    # this is exactly the post-condition ``_refresh_body_cache`` would
    # leave behind, and lets us isolate the "fence-only" path without a
    # full real sync warmup.
    state.body_cache[note] = strip_fence(note.read_text())

    # Now overwrite the file with a DIFFERENT fence but the SAME body.
    body_v2 = _fenced_body("real body\n", ["- [[a|A]] *(R1)*", "- [[b|B]] *(R3)*"])
    _write(note, {"id": note_id, "title": "Fenced"}, body_v2)
    assert strip_fence(note.read_text()) == state.body_cache[note], (
        "fixture invariant: only the fence region differs"
    )
    observer.inject(FileModifiedEvent(str(note)))

    _wait_for(
        lambda: state.skipped_fence_only >= 1,
        timeout=2.0,
    )
    # ``sync_one_file`` was NOT called for this event.
    assert spy_calls == [], (
        f"fence-only write triggered sync; this would loop forever "
        f"(call log: {spy_calls!r})"
    )

    state.stop_event.set()
    thread.join(timeout=5.0)


def test_real_body_change_with_fence_triggers_resync(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """Body changed AND fence present → sync must still run.

    Cache hit comparison is on ``strip_fence`` output, so a different
    real body produces a cache miss → fall through to the normal sync
    path.
    """
    spy_calls = _make_sync_spy(mocker)
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "fenced_changed.md"
    note_id = str(uuid.uuid4())
    body_v1 = _fenced_body("first body\n", ["- [[a|A]] *(R1)*"])
    _write(note, {"id": note_id, "title": "Changed"}, body_v1)

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    state.body_cache[note] = strip_fence(note.read_text())

    # Genuine body change (still has a fence — but the body BEFORE the
    # fence is different, so strip_fence is different).
    body_v2 = _fenced_body("second body, new content\n", ["- [[a|A]] *(R1)*"])
    _write(note, {"id": note_id, "title": "Changed"}, body_v2)
    observer.inject(FileModifiedEvent(str(note)))

    _wait_for(lambda: len(spy_calls) >= 1, timeout=2.0)
    assert spy_calls == [note]
    assert state.skipped_fence_only == 0

    state.stop_event.set()
    thread.join(timeout=5.0)


def test_real_body_change_without_fence_triggers_resync(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """Body changed AND no fence ever existed → sync must run (regression).

    This is the pre-Phase-D contract: a vault note without any fence
    section — typical for user-authored vault-tier files, which we
    don't touch — must still re-sync on edit. The fence dedup must not
    accidentally suppress those.
    """
    spy_calls = _make_sync_spy(mocker)
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "plain.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Plain"}, "v1\n")

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    state.body_cache[note] = strip_fence(note.read_text())

    _write(note, {"id": note_id, "title": "Plain"}, "v2 — completely different\n")
    observer.inject(FileModifiedEvent(str(note)))

    _wait_for(lambda: len(spy_calls) >= 1, timeout=2.0)
    assert spy_calls == [note]
    assert state.skipped_fence_only == 0

    state.stop_event.set()
    thread.join(timeout=5.0)


def test_cold_start_first_sight_triggers_resync(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """First event for a path with no cache entry must run sync.

    Spec: "First sight of a file (no cache entry) → run sync, then
    populate cache." Without this, a watcher restart followed by a
    fence-only rewrite would silently drop the user's first edit since
    the previous session.
    """
    spy_calls = _make_sync_spy(mocker)
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "fresh.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Fresh"}, "hello\n")

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    # Explicitly assert no cache entry exists (cold start).
    assert note not in state.body_cache

    observer.inject(FileModifiedEvent(str(note)))

    _wait_for(lambda: len(spy_calls) >= 1, timeout=2.0)
    assert spy_calls == [note]
    # Cache was populated post-sync (the spy is a no-op so the file
    # contents are unchanged).
    _wait_for(lambda: note in state.body_cache, timeout=1.0)
    assert state.body_cache[note] == strip_fence(note.read_text())

    state.stop_event.set()
    thread.join(timeout=5.0)


def test_delete_invalidates_cache_recreate_resyncs(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """Delete → re-create with same content → re-sync (not a fence-only skip).

    The delete handler must drop the cache entry, otherwise re-creating
    the file with byte-identical contents would look like a fence-only
    no-op and silently skip — even though the row has been removed from
    the DB and genuinely needs an upsert.
    """
    spy_calls = _make_sync_spy(mocker)
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "rebirth.md"
    note_id = str(uuid.uuid4())
    body = _fenced_body("payload\n", ["- [[a|A]] *(R1)*"])
    _write(note, {"id": note_id, "title": "Rebirth"}, body)

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    # Pre-populate cache as if a sync had recently observed the body.
    state.body_cache[note] = strip_fence(note.read_text())

    note.unlink()
    observer.inject(FileDeletedEvent(str(note)))
    _wait_for(lambda: note not in state.body_cache, timeout=2.0)

    # Re-create with byte-identical content — a fence-only check against
    # the (now-evicted) cache would skip and we'd lose the row. Instead
    # the worker should sync because there's no cache entry anymore.
    _write(note, {"id": note_id, "title": "Rebirth"}, body)
    observer.inject(FileCreatedEvent(str(note)))

    _wait_for(lambda: len(spy_calls) >= 1, timeout=2.0)
    assert spy_calls == [note]
    assert state.skipped_fence_only == 0

    state.stop_event.set()
    thread.join(timeout=5.0)


def test_cache_refreshed_after_real_sync(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """A successful sync_one_file populates the cache from the post-sync state.

    Pins the contract that the cache reflects the file's current
    fence-stripped content AFTER ``sync_one_file`` returns — so the
    follow-up filesystem event triggered by sync's own write is
    recognized as fence-only and skipped.
    """
    spy_calls = _make_sync_spy(mocker)
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "refresh.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Refresh"}, "before\n")

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    assert note not in state.body_cache  # cold start

    # First event: cold start → sync runs and cache is populated.
    observer.inject(FileModifiedEvent(str(note)))
    _wait_for(lambda: note in state.body_cache, timeout=2.0)
    assert spy_calls == [note]

    # Second event: same content, no cache miss → fence-only skip.
    observer.inject(FileModifiedEvent(str(note)))
    _wait_for(lambda: state.skipped_fence_only >= 1, timeout=2.0)
    assert len(spy_calls) == 1, (
        f"second identical event must not call sync_one_file; got {spy_calls!r}"
    )

    state.stop_event.set()
    thread.join(timeout=5.0)


def test_overflow_full_sync_clears_body_cache(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """Overflow recovery (full ``sync_vault``) flushes the body cache.

    After a full sync any cached strip_fence values may be stale (the
    full sync may have rewritten any number of fences); clearing forces
    the next per-file event to take the cold-start path so we never
    silently dedup against a stale baseline.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _write(vault / "a.md", {"id": str(uuid.uuid4()), "title": "A"}, "x\n")

    mocker.patch("brain.vault.watch._MAX_PENDING", 1)

    config = WatchConfig(vault_path=vault, debounce_ms=10_000)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    # Seed a stale entry — overflow recovery must flush it.
    bogus = vault / "stale.md"
    state.body_cache[bogus] = "stale-marker"

    # Trip overflow: more pending paths than _MAX_PENDING.
    for i in range(3):
        path = vault / f"f{i}.md"
        path.write_text("# x")
        observer.inject(FileModifiedEvent(str(path)))

    _wait_for(
        lambda: bogus not in state.body_cache,
        timeout=3.0,
    )

    state.stop_event.set()
    thread.join(timeout=5.0)


def test_vanished_file_with_cached_body_is_noop(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path, mocker: Any
) -> None:
    """A vanished file with a stale body-cache entry must be a no-op.

    Companion to ``test_stale_upsert_event_for_vanished_file`` (which covers
    the cold-cache case). The upsert-side existence guard runs BEFORE
    ``_is_fence_only_write``, so it intercepts the vanished file
    regardless of cache state — sync is never attempted, no error is
    counted, and the spy never fires.

    Historical context: an earlier version of this test asserted the
    OPPOSITE — that ``sync_one_file`` would be called on the vanished file
    and the resulting ``FileNotFoundError`` would surface through the
    error path. That was the symptom of the bug fixed in this change;
    the test has been inverted to lock in the fix.
    """
    spy_calls = _make_sync_spy(mocker)
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "vanished.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Vanished"}, "x\n")

    events: list[Any] = []
    config = WatchConfig(
        vault_path=vault,
        debounce_ms=10,
        on_event=lambda a, p: events.append((a, p)),
    )
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state
    state.body_cache[note] = strip_fence(note.read_text())
    processed_before = state.processed

    # Make the file vanish AFTER cache seeding but BEFORE the worker acts.
    note.unlink()
    observer.inject(FileModifiedEvent(str(note)))

    _wait_for(lambda: ("upsert", note) in events, timeout=2.0)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        time.sleep(0.02)

    # Existence-guard short-circuits before _is_fence_only_write or sync.
    assert spy_calls == [], (
        f"vanished file triggered sync; existence guard should have "
        f"short-circuited (call log: {spy_calls!r})"
    )
    assert state.skipped_fence_only == 0
    assert state.errors == 0
    assert state.processed == processed_before

    state.stop_event.set()
    thread.join(timeout=5.0)


def test_handle_delete_logs_error_on_multi_row_delete(
    test_db: psycopg.Connection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive: if vault_path uniqueness is ever broken, ``_handle_delete``
    logs a loud error so the user notices.

    Migration ``003_vault_model.sql`` declares a unique partial index on
    ``documents.vault_path WHERE vault_path IS NOT NULL``, so the DELETE
    in ``_handle_delete`` matches at most one row in normal operation.
    But schema drift (manual ``ALTER``, dropped index, migration
    regression) could let two rows share a ``vault_path``. This test
    synthesizes that broken state by dropping the unique index for the
    duration of the test, inserting two duplicate rows, and asserting:

    1. The DELETE itself proceeds — both rows are gone (current
       behavior preserved; we don't raise + leave half a delete behind).
    2. An ``ERROR``-level log record is emitted so the operator sees
       the invariant break.

    Setup is destructive to the test schema, but ``test_db`` rebuilds a
    fresh schema for every test (see ``conftest.py:test_db``), so the
    dropped index does not leak.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    relative = "shared/duplicate.md"
    abs_path = vault / relative

    # Synthesize the broken state: drop the unique partial index so two
    # rows can share a vault_path. The fresh test schema makes this safe;
    # the next test gets the index back.
    test_db.execute("DROP INDEX documents_vault_path_idx")

    # Insert two vault-tier rows pointing at the same vault_path.
    # content_hash has its own UNIQUE constraint, so each row needs a
    # distinct hash; vault_path is what we're deliberately duplicating.
    id_a = str(uuid.uuid4())
    id_b = str(uuid.uuid4())
    for doc_id, hash_suffix in ((id_a, "a"), (id_b, "b")):
        test_db.execute(
            """
            INSERT INTO documents
              (id, title, content, content_hash, content_type,
               kind, vault_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                doc_id,
                f"Duplicate {hash_suffix}",
                f"body {hash_suffix}\n",
                f"hash-{hash_suffix}",
                "markdown",
                "vault",
                relative,
            ),
        )

    # Sanity: confirm the broken state actually exists before the call.
    pre_count = test_db.execute(
        "SELECT count(*) FROM documents WHERE vault_path = %s",
        (relative,),
    ).fetchone()[0]
    assert pre_count == 2, "test setup must produce two duplicate rows"

    # Capture ERROR-level logs from the watcher logger.
    with caplog.at_level("ERROR", logger="brain.vault.watch"):
        _handle_delete(test_db, abs_path, vault)

    # Both rows must be gone — the SQL still ran, we just observed the
    # invariant break.
    post_count = test_db.execute(
        "SELECT count(*) FROM documents WHERE vault_path = %s",
        (relative,),
    ).fetchone()[0]
    assert post_count == 0, (
        f"_handle_delete should have removed both duplicate rows "
        f"(post_count={post_count})"
    )

    # And the loud-error log fired with the expected diagnostic shape.
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(
        "schema uniqueness invariant broken" in r.getMessage()
        and "expected 0 or 1" in r.getMessage()
        for r in error_records
    ), (
        f"expected an ERROR log mentioning the broken uniqueness invariant, "
        f"got records: {[r.getMessage() for r in error_records]!r}"
    )


# ---------------------------------------------------------------------------
# Watcher rename/move preserves incoming backlinks (Task 2.1).
#
# The old classifier decomposed a within-vault move into two independent
# jobs — ``delete(src)`` + ``upsert(dst)``. The delete ran
# ``DELETE FROM documents WHERE vault_path=src`` whose ON DELETE CASCADE
# wiped every ``links`` row pointing AT the moved doc (its incoming
# backlinks); the follow-up upsert restored only the doc's OUTGOING links.
# The fix threads a single ``move`` action that UPDATEs the row's
# ``vault_path`` in place (preserving the document id → incoming links
# survive) then upserts the destination to refresh content/mirror.
# ---------------------------------------------------------------------------


def _run_events_through_worker(
    conn: psycopg.Connection,
    embedder: Any,
    config: WatchConfig,
    *events: Any,
) -> _WatcherState:
    """Classify each event, enqueue the resulting job(s) in order, then drain
    the worker synchronously.

    Deterministic by construction: no debounce timers and no thread race
    between the two jobs the *legacy* classifier emitted for a move
    (``delete(src)`` then ``upsert(dst)``). Shape-tolerant so the same test
    exercises both the pre-fix 2-tuple classifier (behavioral RED) and the
    post-fix 3-tuple ``move`` classifier (GREEN).
    """
    state = _WatcherState()
    for event in events:
        classified = _classify_event(event, config.vault_path)
        if classified is None:
            continue
        for item in classified:
            action = item[0]
            path = item[1]
            dest = item[2] if len(item) > 2 else None
            job = _Job(action=action, abs_path=path)
            if dest is not None:
                job.dest_path = dest  # field only exists post-fix
            state.jobs.put(job)
    state.jobs.put(None)  # sentinel closes the worker loop
    _worker_loop(conn, embedder, config, state)
    return state


def test_move_within_vault_preserves_incoming_backlinks(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A within-vault rename must NOT destroy backlinks pointing at the doc.

    Regression: renaming ``b.md`` → ``b2.md`` used to DELETE the ``b`` row
    (cascading away the ``a → b`` backlink) and re-insert it, leaving the
    incoming link orphaned until the next full ``brain vault sync``.

    Drives ``classify → enqueue → worker`` synchronously so the pre-fix
    ``delete(src) + upsert(dst)`` ordering is deterministic (the delete runs
    first and cascades the backlink away) rather than racing on debounce
    timers.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    # a → b (incoming backlink for b) and b → a (outgoing link for b).
    _write(vault / "a.md", {"id": a_id, "title": "A"}, "see [[B]]\n")
    _write(vault / "b.md", {"id": b_id, "title": "B"}, "see [[A]]\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    # Sanity: startup sync resolved a → b (the backlink we must protect).
    assert (
        test_db.execute(
            "SELECT count(*) FROM links "
            "WHERE src_document_id = %s AND dst_document_id = %s",
            (a_id, b_id),
        ).fetchone()[0]
        == 1
    )

    # Rename on disk, then drive the corresponding move event through the
    # classifier + worker in order.
    src = vault / "b.md"
    dst = vault / "b2.md"
    src.rename(dst)
    state = _run_events_through_worker(
        test_db, fake_embedder, WatchConfig(vault_path=vault, debounce_ms=10),
        FileMovedEvent(str(src), str(dst)),
    )
    assert state.errors == 0, "worker recorded an error handling the move"

    # The moved doc's vault_path is updated in place...
    assert test_db.execute(
        "SELECT vault_path FROM documents WHERE id = %s", (b_id,)
    ).fetchone() == ("b2.md",)

    # ...and crucially the INCOMING backlink a → b still exists, pointing at
    # the SAME document id (UPDATE-in-place, not delete + re-insert).
    incoming = test_db.execute(
        "SELECT src_document_id::text, dst_document_id::text FROM links "
        "WHERE dst_document_id = %s",
        (b_id,),
    ).fetchall()
    assert incoming == [(a_id, b_id)], (
        f"incoming backlink to the moved doc was destroyed: {incoming!r}"
    )

    # The document id is unchanged (exactly one row still carries it).
    assert (
        test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (b_id,)
        ).fetchone()[0]
        == 1
    )

    # The moved doc's OUTGOING link b → a was re-materialized by the upsert
    # on the destination — proving the normal per-file resolution pipeline
    # (materialize + unresolved retry) ran for the new path.
    outgoing = test_db.execute(
        "SELECT dst_document_id::text FROM links WHERE src_document_id = %s",
        (b_id,),
    ).fetchall()
    assert outgoing == [(a_id,)], f"outgoing link not re-materialized: {outgoing!r}"

    # Derived rows are a vault-tier-inert concern here (no gmail/krisp docs);
    # the move must not spuriously create any.
    assert (
        test_db.execute("SELECT count(*) FROM derived_links").fetchone()[0] == 0
    )


def test_move_within_vault_preserves_backlinks_end_to_end(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """Integration: the full ``run_watcher`` machinery (observer → debounce →
    worker) also preserves incoming backlinks across a rename.

    Post-fix a within-vault move is a single ``move`` job, so this is
    deterministic (no delete/upsert ordering race).
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a_id, "title": "A"}, "see [[B]]\n")
    _write(vault / "b.md", {"id": b_id, "title": "B"}, "see [[A]]\n")

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()
    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM links "
            "WHERE src_document_id = %s AND dst_document_id = %s",
            (a_id, b_id),
        ).fetchone()[0]
        == 1,
        timeout=2.0,
    )

    src = vault / "b.md"
    dst = vault / "b2.md"
    src.rename(dst)
    observer.inject(FileMovedEvent(str(src), str(dst)))

    _wait_for(
        lambda: test_db.execute(
            "SELECT vault_path FROM documents WHERE id = %s", (b_id,)
        ).fetchone()
        == ("b2.md",),
        timeout=2.0,
    )
    incoming = test_db.execute(
        "SELECT src_document_id::text, dst_document_id::text FROM links "
        "WHERE dst_document_id = %s",
        (b_id,),
    ).fetchall()
    assert incoming == [(a_id, b_id)], (
        f"incoming backlink to the moved doc was destroyed: {incoming!r}"
    )

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_move_out_of_vault_still_deletes(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A move whose destination lands OUTSIDE the vault still removes the row.

    The destination is filtered out by ``_filter_path`` (outside the vault),
    so the classifier emits a plain ``delete`` for the source — today's
    behavior, preserved.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    note_id = str(uuid.uuid4())
    _write(vault / "leaving.md", {"id": note_id, "title": "Leaving"}, "x\n")

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 1,
        timeout=2.0,
    )

    src = vault / "leaving.md"
    dst = outside / "leaving.md"
    src.rename(dst)
    observer.inject(FileMovedEvent(str(src), str(dst)))

    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 0,
        timeout=2.0,
    )

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


def test_handle_move_dest_vanished_falls_back_to_delete(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """If the move destination has vanished by the time the worker runs, the
    source row is removed (plain delete) rather than pointed at a missing file.

    Reproduces a move-then-delete inside the debounce window. Driving
    ``_handle_move`` directly keeps the edge case deterministic.
    """
    from brain.vault.sync import sync_one_file  # noqa: PLC0415
    from brain.vault.watch import _handle_move, _WatcherState  # noqa: PLC0415

    vault = tmp_path / "vault"
    vault.mkdir()
    note_id = str(uuid.uuid4())
    _write(vault / "gone.md", {"id": note_id, "title": "Gone"}, "x\n")
    sync_one_file(
        test_db, embedder=fake_embedder, vault_path=vault, file_path=vault / "gone.md"
    )
    assert (
        test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 1
    )

    src = vault / "gone.md"
    dst = vault / "gone2.md"  # never created on disk — the vanished dest
    src.unlink()  # source also gone (a move away then delete)
    config = WatchConfig(vault_path=vault, debounce_ms=10)
    state = _WatcherState()

    _handle_move(
        test_db,
        embedder=fake_embedder,
        config=config,
        state=state,
        src_abs=src,
        dst_abs=dst,
    )

    # Row removed; no crash; vault_path never points at the missing dest.
    assert (
        test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 0
    )


# ---------------------------------------------------------------------------
# Task 3.1: a new top-level directory created AFTER the watcher starts must be
# scheduled for watching, and any files already inside it must be picked up.
#
# The root is watched non-recursively and per-subdir recursive watches are
# enumerated only at startup, so a brand-new top-level dir (and every file
# under it) was invisible until the watcher restarted.
# ---------------------------------------------------------------------------


def test_new_top_level_dir_created_after_start_is_watched_and_synced(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A top-level dir created post-startup is watched + its notes synced.

    Repro: with the root watched ``recursive=False`` and per-subdir recursive
    watches only enumerated at startup, a directory created afterwards is
    never scheduled — its ``.md`` files stay invisible until restart. The fix
    schedules a recursive watch on the new dir (so future edits inside it are
    seen) and enqueues the notes that already exist in it (which predate the
    watch and would otherwise never generate a create event).
    """
    from watchdog.events import DirCreatedEvent  # noqa: PLC0415

    vault = tmp_path / "vault"
    vault.mkdir()
    # A pre-existing top-level dir so startup schedules at least one subtree.
    (vault / "notes").mkdir()

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=fake_embedder,
        config=config,
    )
    observer = _wait_for_observer()

    # A brand-new top-level directory appears with a note already inside it —
    # the note predates any watch on the new directory.
    newdir = vault / "fresh-project"
    newdir.mkdir()
    note = newdir / "kickoff.md"
    note_id = str(uuid.uuid4())
    _write(note, {"id": note_id, "title": "Kickoff"}, "hello\n")

    # The OS surfaces the new directory via the root's non-recursive watch.
    observer.inject(DirCreatedEvent(str(newdir)))

    # (a) A recursive watch is now scheduled on the new directory so future
    #     edits inside it are seen.
    _wait_for(
        lambda: any(
            Path(p) == newdir and recursive
            for _h, p, recursive in observer.scheduled
        ),
        timeout=2.0,
    )
    # (b) The note that already existed inside the new dir is synced without a
    #     separate file event.
    _wait_for(
        lambda: test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0]
        == 1,
        timeout=2.0,
    )

    state = observer.handler._state
    state.stop_event.set()
    thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Task 3.2: an edit landing during sync must not be masked by the body cache.
#
# The worker refreshed its fence-only-write cache by RE-READING disk after
# ``sync_one_file`` returned. A save landing between sync's read and that
# refresh was baselined into the cache, so the follow-up event for the save
# was misclassified as fence-only and skipped — the DB never got the edit
# until a full sync. The fix caches the fence-stripped body sync actually
# indexed instead of re-reading disk.
# ---------------------------------------------------------------------------


class _EditDuringEmbed:
    """Embedder that overwrites a target file the first time it embeds.

    Models a user save landing during the (slow, real-backend) embed call
    inside ``sync_one_file``: the DB is built from the body sync READ, but the
    file on disk ends up holding the racing edit by the time sync returns.
    Standard test double (not monkey-patching) — it conforms to the
    ``Embedder`` protocol and delegates the actual vectors to ``inner``.
    """

    def __init__(self, inner: Any, target: Path, new_text: str) -> None:
        self.dim = inner.dim
        self._inner = inner
        self._target = target
        self._new_text = new_text
        self.fired = False

    def embed(
        self, texts: list[str], input_type: str = "document"
    ) -> list[list[float]]:
        if not self.fired:
            self.fired = True
            self._target.write_text(self._new_text, encoding="utf-8")
        return self._inner.embed(texts, input_type=input_type)  # type: ignore[no-any-return]

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)  # type: ignore[no-any-return]


def test_racing_edit_during_sync_is_not_masked_by_body_cache(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """A user edit landing during sync must still reach the DB (Task 3.2).

    Sync indexes the body it READ (``body_indexed``); a concurrent save
    replaces the file with ``body_raced`` before sync returns. The follow-up
    filesystem event for that save must trigger a real re-sync so the DB
    catches up — it must NOT be masked as a fence-only no-op by a cache that
    was baselined from the raced disk content.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "raced.md"
    note_id = str(uuid.uuid4())
    fm = {"id": note_id, "title": "Raced"}
    body_indexed = "alpha body version one\n"
    body_raced = "beta body version two — the edit that raced\n"
    raced_text = dump_frontmatter(fm, body_raced)

    # The note does NOT exist at startup, so the startup sync embeds nothing
    # and the edit-during-embed only fires on the post-startup event.
    embedder = _EditDuringEmbed(fake_embedder, note, raced_text)

    config = WatchConfig(vault_path=vault, debounce_ms=10)
    thread, _ = _start_watcher(
        conn_factory=_conn_factory(test_db),
        embedder=embedder,
        config=config,
    )
    observer = _wait_for_observer()
    state = observer.handler._state

    # First save: body_indexed on disk. The event triggers sync, whose embed
    # writes body_raced to disk mid-flight (the racing user edit).
    _write(note, fm, body_indexed)
    observer.inject(FileModifiedEvent(str(note)))

    # Sync 1 done: DB holds the INDEXED body, disk holds the raced body, and
    # the cache has been primed.
    _wait_for(lambda: embedder.fired, timeout=2.0)
    _wait_for(lambda: note in state.body_cache, timeout=2.0)
    row = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None and "alpha body version one" in row[0]

    # Second event: the racing save's own filesystem event. It must NOT be
    # masked as fence-only — the DB has to catch up to the raced body.
    observer.inject(FileModifiedEvent(str(note)))
    _wait_for(
        lambda: "beta body version two"
        in test_db.execute(
            "SELECT content FROM documents WHERE id = %s", (note_id,)
        ).fetchone()[0],
        timeout=2.0,
    )

    state.stop_event.set()
    thread.join(timeout=5.0)
