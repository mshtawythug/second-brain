"""Long-running watcher for ``brain vault sync --watch``.

A watcher daemon that reconciles vault edits into the DB without the user
having to run ``brain vault sync`` after every save. The lifecycle is:

1. Run a one-shot ``sync_vault`` on startup so any edits made while the
   watcher was off get caught up.
2. Start a watchdog ``Observer`` that emits filesystem events from the OS
   (FSEvents on macOS, inotify on Linux). The Observer runs on its own
   thread and pushes raw events through ``_handle_event``.
3. ``_handle_event`` filters out non-``.md`` paths, hidden directories,
   templates, and attachments, then debounces per absolute path with a
   ``threading.Timer``. When a path's debounce fires it enqueues a
   ``_Job`` onto the worker queue.
4. A single worker thread consumes the queue serially. It owns the only
   DB connection used by the watcher (we never share a psycopg connection
   across threads). Each job runs ``sync_one_file`` (created/modified) or
   removes the row + relinks (deleted) for the affected path. Errors from
   one job never kill the worker — they're logged and the loop continues.
5. SIGINT / SIGTERM flip a ``threading.Event``; the main thread stops the
   Observer, waits for any in-flight debounce timers to fire (so pending
   edits drain instead of getting dropped), then joins the worker.

Concurrency invariants worth restating:

- Exactly one psycopg connection lives in the worker thread. The Observer
  thread never touches the DB. The main thread never touches the DB
  after the startup sync returns. (Sharing connections across threads
  triggers psycopg ``InterfaceError: another command is already in
  progress`` under load.)
- ``_pending_timers`` and ``_jobs`` are only touched while holding
  ``_state_lock`` — a short, fine-grained mutex that doesn't block the
  worker (the worker holds the queue's lock instead).
- The debounce buffer is capped (``_MAX_PENDING``) so a runaway editor
  spamming events can't OOM the process; on overflow we trigger an
  immediate full sync and clear the buffer.
"""
from __future__ import annotations

import logging
import os
import queue
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import psycopg
from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from ..ingest import Embedder
from .sync import SyncReport, sync_one_file, sync_vault

logger = logging.getLogger(__name__)


# Per-file actions the worker can run. Filesystem moves are decomposed
# into ``deleted(src) + created(dst)`` upstream so this short list covers
# every event type the worker actually sees.
_Action = Literal["upsert", "delete"]

# Hard cap on the debounce buffer. If the user (or an editor's autosave
# loop) outpaces this, we fall back to a full vault sync to recover —
# conceptually an admission-control valve, not a normal code path.
_MAX_PENDING = 1000


@dataclass
class WatchConfig:
    """User-facing knobs for the watcher.

    ``debounce_ms`` is the per-path quiet period: rapid bursts (e.g. an
    editor's "save → reformat → save again" cycle) collapse into one sync
    after the path has been quiet for this long. The production default
    (500 ms) is comfortable on macOS FSEvents which itself coalesces;
    tests use a much smaller value (10 ms) to stay snappy without
    sleeping.

    ``prune`` is plumbed through to the startup ``sync_vault`` call only —
    we don't auto-prune in response to single-file delete events because
    we don't want a race between "file moved to a temp dir while editor
    saves" and "row gone from DB" to cause data loss. A delete event
    triggers a row-removal for that single document, which is precise.

    ``on_event`` is a test-only hook called once per debounced job *just
    before* it's enqueued. Production passes ``None``; tests pass a
    closure to assert which paths actually made it through filtering.
    """

    vault_path: Path
    debounce_ms: int = 500
    prune: bool = False
    on_event: Callable[[_Action, Path], None] | None = None


@dataclass
class _Job:
    """One unit of work for the worker thread."""

    action: _Action
    abs_path: Path


@dataclass
class _WatcherState:
    """Mutable state shared across threads — guarded by ``lock``."""

    pending_timers: dict[Path, threading.Timer] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    jobs: queue.Queue[_Job | None] = field(default_factory=queue.Queue)
    # ``processed`` and ``errors`` accumulate over the watcher's lifetime
    # so the final shutdown report can summarize "watched 14 files; 1
    # error". Only the worker thread touches them, so no lock is needed.
    processed: int = 0
    errors: int = 0


def run_watcher(
    conn_factory: Callable[[], psycopg.Connection[Any]],
    *,
    embedder: Embedder,
    config: WatchConfig,
    observer_factory: Callable[[], BaseObserver] | None = None,
    install_signal_handlers: bool = True,
) -> SyncReport:
    """Block until SIGINT/SIGTERM, watching ``config.vault_path``.

    On entry, performs a one-shot full sync so any edits made while the
    watcher was off are picked up. Then starts a watchdog ``Observer``
    plus a worker thread, and parks the main thread on
    ``state.stop_event`` until a signal fires.

    Returns the startup ``SyncReport`` so the CLI can print "watching
    after initial sync: created 3, updated 0…" before parking.

    ``conn_factory`` MUST return a fresh psycopg connection each call;
    the watcher takes ownership of exactly one connection (lives in the
    worker thread for the duration of the watcher) and closes it on
    shutdown. Production passes ``lambda: psycopg.connect(database_url)``
    via the CLI; tests pass a closure that opens the test DB.

    ``observer_factory`` defaults to watchdog's real ``Observer``; tests
    inject a fake to avoid actually touching the OS watcher subsystem.

    ``install_signal_handlers`` defaults to True. Tests set this to False
    so they can drive shutdown via ``state.stop_event`` directly without
    interfering with pytest's own signal handling.
    """
    state = _WatcherState()

    # 1. Startup sync. Use a short-lived connection scoped to this call —
    #    we don't want to hold a connection while parked on the stop event.
    startup_conn = conn_factory()
    try:
        startup_conn.autocommit = True
        report = sync_vault(
            startup_conn,
            embedder=embedder,
            vault_path=config.vault_path,
            prune=config.prune,
            dry_run=False,
        )
    finally:
        startup_conn.close()

    # 2. Worker thread. Owns the long-lived DB connection and consumes
    #    jobs from the queue. ``daemon=True`` so a crashing worker
    #    doesn't prevent process exit; we still join below for clean
    #    shutdown.
    worker_conn = conn_factory()
    worker_conn.autocommit = True
    worker_thread = threading.Thread(
        target=_worker_loop,
        args=(worker_conn, embedder, config, state),
        name="brain-vault-watcher-worker",
        daemon=True,
    )
    worker_thread.start()

    # 3. Observer + handler. The handler is a thin shim that turns
    #    watchdog events into ``_Job``s via the debounce path. The
    #    Observer runs on its own thread (managed by watchdog).
    factory = observer_factory or Observer
    observer = factory()
    handler = _Handler(config=config, state=state)
    observer.schedule(handler, str(config.vault_path), recursive=True)
    observer.start()

    # 4. Signal handlers. SIGINT for Ctrl-C, SIGTERM for systemd /
    #    process managers. We can only install these from the main
    #    thread — Python enforces this — and the CLI calls run_watcher
    #    from the main thread, so this is fine. Tests set
    #    ``install_signal_handlers=False`` to skip this.
    previous_handlers: dict[signal.Signals, Any] = {}
    if install_signal_handlers:
        def _signal_handler(signum: int, _frame: Any) -> None:
            logger.info("vault watcher: received signal %s, shutting down", signum)
            state.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, _signal_handler)

    try:
        # Block until a signal (or test) flips the stop_event.
        state.stop_event.wait()
    finally:
        # Restore previous signal handlers so a follow-up CliRunner /
        # KeyboardInterrupt still behaves as the test runner expects.
        for sig, prev in previous_handlers.items():
            signal.signal(sig, prev)

        # 5. Shutdown.
        observer.stop()
        observer.join(timeout=5.0)

        # Drain any in-flight debounce timers: cancel them, then enqueue
        # an immediate job for each path that had a pending timer so the
        # worker still picks up the user's last edit before exiting.
        with state.lock:
            pending_paths = list(state.pending_timers.keys())
            for timer in state.pending_timers.values():
                timer.cancel()
            state.pending_timers.clear()
        for path in pending_paths:
            _enqueue(state, _Job(action="upsert", abs_path=path), config=config)

        # Sentinel tells the worker its queue is now closed.
        state.jobs.put(None)
        worker_thread.join(timeout=10.0)
        try:
            worker_conn.close()
        except psycopg.Error:
            # Connection may already be closed (e.g. server-side timeout) —
            # nothing to recover, just don't crash on shutdown.
            logger.debug("vault watcher: worker connection already closed")

    logger.info(
        "vault watcher: stopped after %d events (%d errors)",
        state.processed,
        state.errors,
    )
    return report


# ---------------------------------------------------------------------------
# Internal helpers — event classification, debouncing, worker loop.
# ---------------------------------------------------------------------------


class _Handler(FileSystemEventHandler):
    """Watchdog handler that funnels events into our debounce path.

    Kept deliberately small: classification + filtering live in
    ``_classify_event``, debouncing lives in ``_schedule_debounced``,
    job execution lives in ``_worker_loop``. The handler just glues
    them together.
    """

    def __init__(self, *, config: WatchConfig, state: _WatcherState) -> None:
        self._config = config
        self._state = state

    def on_any_event(self, event: FileSystemEvent) -> None:
        # ``on_any_event`` fires for every event type — we route
        # everything through one classifier rather than per-type
        # handlers so the filtering logic lives in one place.
        try:
            classified = _classify_event(event, self._config.vault_path)
        except Exception:
            # Defensive: a misbehaving event must never kill the
            # observer thread. Log and move on.
            logger.exception("vault watcher: classify failed for %r", event)
            return
        if classified is None:
            return
        for action, path in classified:
            _schedule_debounced(self._config, self._state, action, path)


def _classify_event(
    event: FileSystemEvent, vault_path: Path
) -> list[tuple[_Action, Path]] | None:
    """Decide what to do with a watchdog event.

    Returns a list of (action, absolute_path) tuples — usually one entry,
    but a ``moved`` event becomes two (delete src + upsert dst). Returns
    ``None`` to skip the event entirely.

    Filtering rules (kept conservative — the worker can always re-derive
    state from the DB if a real edit slipped past, but we do NOT want to
    waste cycles on every byte-level FSEvent):

    - directory events are skipped (we sync per-file)
    - non-``.md`` paths are skipped (editor temp files, ``.DS_Store``,
      attachments, etc.)
    - paths with any hidden component (starts with ``.``) are skipped —
      catches ``.git/``, ``.obsidian/``, VSCode's ``.vscode/`` etc.
    - paths under ``_templates/`` or ``_attachments/`` are skipped —
      the sync engine itself ignores these so syncing them is wasted work
    """
    if event.is_directory:
        return None

    event_type = event.event_type  # 'created' | 'modified' | 'deleted' | 'moved'
    if event_type == "moved":
        # Watchdog's FileMovedEvent exposes both src and dest. Path values
        # are typed as ``bytes | str`` upstream — decode bytes paths via
        # ``os.fsdecode`` so Path() accepts them on every platform.
        src = _filter_path(_to_path(event.src_path), vault_path)
        dest_attr = getattr(event, "dest_path", None)
        dst = _filter_path(_to_path(dest_attr), vault_path) if dest_attr else None
        results: list[tuple[_Action, Path]] = []
        if src is not None:
            results.append(("delete", src))
        if dst is not None:
            results.append(("upsert", dst))
        return results or None

    path = _filter_path(_to_path(event.src_path), vault_path)
    if path is None:
        return None
    action: _Action = "delete" if event_type == "deleted" else "upsert"
    return [(action, path)]


def _to_path(raw: str | bytes) -> Path:
    """Coerce a watchdog event path (``bytes | str``) into a ``Path``.

    Watchdog declares event paths as ``bytes | str`` because some
    backends (e.g. inotify on Linux) can surface raw bytes. We decode
    eagerly via ``os.fsdecode`` so the rest of the watcher uses
    ``pathlib.Path`` exclusively.
    """
    if isinstance(raw, bytes):
        return Path(os.fsdecode(raw))
    return Path(raw)


def _filter_path(path: Path, vault_path: Path) -> Path | None:
    """Return the absolute path if it's a syncable ``.md`` file, else None.

    The full filter ladder lives here so ``_classify_event`` (which
    handles move events with two paths) can reuse it without
    duplication.
    """
    abs_path = path if path.is_absolute() else (vault_path / path)
    try:
        relative = abs_path.resolve().relative_to(vault_path.resolve())
    except ValueError:
        # Outside the vault — should not happen since the Observer
        # watches inside the vault, but defensive.
        return None
    except OSError:
        # ``resolve()`` can fail on a vanished file (deleted events).
        # Fall back to lexical comparison: strip the vault prefix
        # without touching the FS.
        try:
            relative = abs_path.relative_to(vault_path)
        except ValueError:
            return None
    if abs_path.suffix.lower() != ".md":
        return None
    parts = relative.parts
    if not parts:
        return None
    # Hidden directories anywhere in the path — covers .git, .obsidian,
    # editor scratch dirs, etc.
    if any(part.startswith(".") for part in parts):
        return None
    first = parts[0]
    if first in {"_templates", "_attachments"}:
        return None
    return abs_path


def _schedule_debounced(
    config: WatchConfig,
    state: _WatcherState,
    action: _Action,
    path: Path,
) -> None:
    """Cancel any pending timer for ``path`` and start a new one.

    Each new event for the same path resets the debounce timer to
    ``config.debounce_ms`` from now. Once the path goes ``debounce_ms``
    without a new event, the timer fires and enqueues a job.

    If the pending-timer dict overflows (a misbehaving editor or a
    bulk-replace operation), we fall back to "trigger an immediate full
    sync, then clear the buffer" — bounded resource usage trumps
    incremental optimization here.
    """
    delay = max(config.debounce_ms, 0) / 1000.0

    def _fire() -> None:
        with state.lock:
            state.pending_timers.pop(path, None)
        _enqueue(state, _Job(action=action, abs_path=path), config=config)

    with state.lock:
        existing = state.pending_timers.pop(path, None)
        if existing is not None:
            existing.cancel()
        if len(state.pending_timers) >= _MAX_PENDING:
            # Buffer overflow → drop everything, queue a full-vault sync.
            for timer in state.pending_timers.values():
                timer.cancel()
            state.pending_timers.clear()
            logger.warning(
                "vault watcher: debounce buffer overflow (%d pending) — "
                "scheduling full sync",
                _MAX_PENDING,
            )
            _enqueue(
                state,
                _Job(action="upsert", abs_path=config.vault_path),
                config=config,
                bypass_filter=True,
            )
            return
        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        state.pending_timers[path] = timer
        timer.start()


def _enqueue(
    state: _WatcherState,
    job: _Job,
    *,
    config: WatchConfig,
    bypass_filter: bool = False,
) -> None:
    """Push a job onto the worker queue, after the optional test hook.

    ``bypass_filter`` is True only for the synthetic full-sync job
    enqueued on debounce-buffer overflow — that job uses the vault root
    path which would otherwise be rejected by ``_filter_path``.
    """
    del bypass_filter  # currently informational; kept for future caller filtering
    if config.on_event is not None:
        try:
            config.on_event(job.action, job.abs_path)
        except Exception:
            logger.exception("vault watcher: on_event hook raised")
    state.jobs.put(job)


def _worker_loop(
    conn: psycopg.Connection[Any],
    embedder: Embedder,
    config: WatchConfig,
    state: _WatcherState,
) -> None:
    """Consume jobs serially. Owns the DB connection.

    Three classes of work, distinguished by ``job.abs_path``:

    1. ``upsert`` of a regular file → ``sync_one_file``
    2. ``delete`` of a regular file → DB row removal + link cleanup
       (we don't touch disk; the file is already gone)
    3. ``upsert`` of the vault root path → full ``sync_vault`` (the
       overflow-recovery path)

    Each call is wrapped in try/except so a single-file failure logs
    but doesn't kill the worker — the watcher should be self-healing.
    """
    while True:
        job = state.jobs.get()
        if job is None:
            return
        try:
            if job.abs_path == config.vault_path and job.action == "upsert":
                # Overflow-recovery: do a full sync.
                sync_vault(
                    conn,
                    embedder=embedder,
                    vault_path=config.vault_path,
                    prune=False,  # never auto-prune in watch mode
                    dry_run=False,
                )
            elif job.action == "delete":
                _handle_delete(conn, job.abs_path, config.vault_path)
            else:
                sync_one_file(
                    conn,
                    embedder=embedder,
                    vault_path=config.vault_path,
                    file_path=job.abs_path,
                )
            state.processed += 1
        except Exception:
            state.errors += 1
            logger.exception(
                "vault watcher: worker failed on %s %s",
                job.action,
                job.abs_path,
            )


def _handle_delete(
    conn: psycopg.Connection[Any], abs_path: Path, vault_path: Path
) -> None:
    """Remove the vault-tier ``documents`` row for ``abs_path``.

    Why not call ``sync_vault(prune=True)``? Two reasons: (1) we'd be
    doing a full walk for a single deletion, which is wasteful, and (2)
    auto-prune in ``sync_vault`` only fires when *every* file's been
    walked — for a focused delete event we want to act on just this
    path. So instead we look up the row by ``vault_path`` and delete
    it; ``ON DELETE CASCADE`` handles chunks + outgoing links.

    Ingested-tier rows are intentionally never auto-deleted in watch
    mode — those mirror an upstream system (Krisp, Slack, Gmail), and a
    transient ``.md`` removal under ``_ingested/`` shouldn't blow away
    the canonical DB record. The user can always re-export.

    Idempotent: a delete event for a path with no DB row is a no-op.
    """
    try:
        relative = abs_path.relative_to(vault_path.resolve()).as_posix()
    except ValueError:
        try:
            relative = abs_path.relative_to(vault_path).as_posix()
        except ValueError:
            logger.debug(
                "vault watcher: delete event for %s outside vault — skipping",
                abs_path,
            )
            return
    conn.execute(
        "DELETE FROM documents WHERE kind = 'vault' AND vault_path = %s",
        (relative,),
    )
