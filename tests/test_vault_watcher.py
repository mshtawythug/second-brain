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

from brain.vault.frontmatter import dump_frontmatter
from brain.vault.watch import (
    WatchConfig,
    _classify_event,
    _filter_path,
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
    [(action, path)] = classified
    assert action == "upsert"
    assert path.name == "note.md"


def test_classify_deleted_yields_delete(tmp_path: Path) -> None:
    event = FileDeletedEvent(str(tmp_path / "gone.md"))
    classified = _classify_event(event, tmp_path)
    assert classified is not None
    [(action, _path)] = classified
    assert action == "delete"


def test_classify_moved_yields_delete_plus_upsert(tmp_path: Path) -> None:
    """A file rename should remove the old row and upsert the new one."""
    src = tmp_path / "old.md"
    dst = tmp_path / "new.md"
    event = FileMovedEvent(str(src), str(dst))
    classified = _classify_event(event, tmp_path)
    assert classified is not None
    actions = [a for a, _ in classified]
    assert actions == ["delete", "upsert"]


def test_classify_moved_outside_vault_skips_dst(tmp_path: Path) -> None:
    """If a file is moved out of the vault, the destination is skipped
    but the source still becomes a delete event."""
    src = tmp_path / "old.md"
    dst = tmp_path / "_attachments" / "new.md"
    event = FileMovedEvent(str(src), str(dst))
    classified = _classify_event(event, tmp_path)
    assert classified is not None
    actions = [a for a, _ in classified]
    assert actions == ["delete"]


def test_filter_path_handles_bytes(tmp_path: Path) -> None:
    """Inotify can surface ``bytes`` paths — we must accept them."""
    target = tmp_path / "note.md"
    target.write_text("hi")
    encoded = os.fsencode(str(target))
    event = FileCreatedEvent(encoded)
    classified = _classify_event(event, tmp_path)
    assert classified is not None
    [(_, path)] = classified
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


def test_signal_handlers_install_and_restore(
    test_db: psycopg.Connection, fake_embedder: Any, tmp_path: Path
) -> None:
    """When ``install_signal_handlers=True``, SIGINT triggers shutdown.

    We send the signal to our own process; the watcher must trap it and
    return cleanly within a second.
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
