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

Fence-only writes (Phase D, Task D.5):

The metadata-aware linker rewrites a fenced ``BRAIN_DERIVED_*`` section
inside every affected ``_ingested/`` body whenever derived edges are
rebuilt (`docs/specs/2026-04-30-derived-edges-in-bodies-design.md`).
Decision Q4=(b) means the renderer rewrites the fence even when its
content is byte-identical, which guarantees a filesystem mtime bump and
therefore another watch event. Without dedup, that event would re-enter
``sync_one_file`` → ``rebuild_derived_for`` → ``rewrite_derived_fences``
and we'd loop forever.

The dedup lives in the worker: every successful sync caches
``strip_fence(file_contents)`` keyed by absolute path. When a later
event fires for that path, the worker reads the file, strips the fence,
and compares to the cache. Fence-only changes (or no change at all) skip
``sync_one_file``. Cold start (first sight of a path), cache miss after a
delete, or any genuine body change all fall through to the normal sync
path, so we never silently drop a real edit.
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
from typing import TYPE_CHECKING, Any, Literal

import psycopg
from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver as Observer

# brain (2026-05-08): use PollingObserver instead of watchdog's default
# Observer (which on macOS dispatches via FSEventsObserver). On Python 3.13
# the FSEvents backend silently stops delivering events: the Observer
# starts cleanly and stays alive, but `on_any_event` is never called.
# No error, no log. Both this watcher and `brain.wiki.build_watcher` were
# affected — every edit to a vault `.md` was invisible until manual sync.
# PollingObserver scans the tree on a timer (default 1s) and synthesizes
# events from mtime/size diffs. CPU cost is a stat() per file per second
# (negligible for ~1100 docs); latency is the polling interval.
from ..ingest import Embedder
from .derived_links.fence import strip_fence
from .sync import SyncReport, sync_one_file, sync_vault

if TYPE_CHECKING:
    from ..graph_rag.sync import GraphSyncer

logger = logging.getLogger(__name__)


# Per-file actions the worker can run. A within-vault markdown rename is a
# single ``move`` job (carrying both src + dst) so the worker can UPDATE the
# row's ``vault_path`` in place — preserving the document id and, with it,
# every incoming backlink (``links.dst_document_id``). Moves whose
# destination leaves the vault (or stops being a ``.md`` file) still
# decompose into ``delete(src)`` + ``upsert(dst)`` in ``_classify_event``.
_Action = Literal["upsert", "delete", "move"]

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
    # Plumbed from ``brain vault sync --no-link-rewrite``: when False, neither
    # the startup full sync nor the per-file sync will rewrite vault-tier
    # ``[[…]]`` markers to vault-root-relative path form. The rewrite is on
    # by default — ``False`` is opt-out for users who want to preserve the
    # exact authored shape of their wiki-links.
    link_rewrite: bool = True
    # Plumbed from ``Config.owner_participants`` (env: BRAIN_OWNER_PARTICIPANTS):
    # identifiers (emails / display names, lowercased + trimmed at config-load
    # time) stripped from each ``DocSnapshot.participant_keys`` before R2/R3
    # rule evaluation in the linker pass. Empty frozenset (default) disables
    # the filter — used by every existing test.
    owner_participants: frozenset[str] = frozenset()


@dataclass
class _Job:
    """One unit of work for the worker thread.

    ``dest_path`` is only set for ``move`` jobs — it carries the rename
    destination so the worker can UPDATE ``documents.vault_path`` from
    ``abs_path`` (the source) to ``dest_path`` in place, preserving the
    document id and every incoming backlink. It stays ``None`` for every
    other action.
    """

    action: _Action
    abs_path: Path
    dest_path: Path | None = None


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
    skipped_fence_only: int = 0
    # Cache of the fence-stripped body last observed AFTER a successful
    # sync, keyed by absolute path. Lets the worker detect fence-only
    # rewrites (the linker's `BRAIN_DERIVED_*` section regen) and skip a
    # redundant ``sync_one_file`` that would just re-trigger the same
    # rewrite, looping forever. Only the worker thread touches this dict
    # — no lock is needed for the same reason ``processed`` doesn't
    # take one.
    body_cache: dict[Path, str] = field(default_factory=dict)


def run_watcher(
    conn_factory: Callable[[], psycopg.Connection[Any]],
    *,
    embedder: Embedder,
    config: WatchConfig,
    observer_factory: Callable[[], BaseObserver] | None = None,
    install_signal_handlers: bool = True,
    graph_syncer: GraphSyncer | None = None,
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
            link_rewrite=config.link_rewrite,
            owner_participants=config.owner_participants,
            graph_syncer=graph_syncer,
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
        args=(worker_conn, embedder, config, state, graph_syncer),
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
    # brain (2026-05-08): schedule per non-hidden top-level subdir + root
    # non-recursively, instead of `recursive=True` on the vault root.
    # PollingObserver snapshots every file in the watched tree on every
    # poll cycle; with `recursive=True` on a vault that contains
    # `<vault>/.quartz/node_modules` (~60k+ files) the snapshot work
    # starves the polling thread and edits stop being detected. The
    # event filter (_filter_path) already drops paths under hidden dirs,
    # so excluding them from the WATCHED tree is correct + cheap.
    scheduled_names: list[str] = []
    observer.schedule(handler, str(config.vault_path), recursive=False)
    for child in config.vault_path.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            observer.schedule(handler, str(child), recursive=True)
            scheduled_names.append(child.name)
    logger.info(
        "vault watcher: scoped polling on top-level dirs: %s",
        ", ".join(scheduled_names) or "(none)",
    )
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
        if worker_thread.is_alive():
            # The worker is mid-statement (a sync_vault call, an embed
            # request, etc.) past our 10s patience. Closing the
            # connection from the main thread now would race against
            # the in-flight statement and raise from the worker thread
            # while we're trying to return cleanly. The thread is a
            # daemon — it carries the orphaned connection on its back,
            # and Postgres reaps the backend after its idle timeout.
            logger.warning(
                "vault watcher: worker thread did not exit within 10s — "
                "leaking its connection to avoid a mid-statement close race"
            )
        else:
            try:
                worker_conn.close()
            except psycopg.Error:
                # Connection may already be closed (e.g. server-side timeout) —
                # nothing to recover, just don't crash on shutdown.
                logger.debug(
                    "vault watcher: worker connection already closed"
                )

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
        for action, path, dest in classified:
            _schedule_debounced(self._config, self._state, action, path, dest)


def _classify_event(
    event: FileSystemEvent, vault_path: Path
) -> list[tuple[_Action, Path, Path | None]] | None:
    """Decide what to do with a watchdog event.

    Returns a list of ``(action, absolute_path, dest_path)`` tuples — usually
    one entry with ``dest_path=None``. A within-vault ``.md`` rename becomes a
    single ``("move", src, dst)`` so the worker can UPDATE the row's
    ``vault_path`` in place, preserving the document id and every incoming
    backlink. A move whose destination leaves the vault (or stops being a
    ``.md`` file) still decomposes into ``("delete", src, None)`` +
    ``("upsert", dst, None)``. Returns ``None`` to skip the event entirely.

    Note: editors that emulate a rename as an atomic delete+create (and
    cross-device moves) emit no ``FileMovedEvent`` — those arrive as separate
    ``deleted`` / ``created`` events and keep the delete+upsert behavior, so a
    heavily-linked note renamed that way still relies on a full
    ``brain vault sync`` to restore its incoming backlinks.

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
        if src is not None and dst is not None:
            # Within-vault markdown rename: thread it as a single ``move`` so
            # the worker UPDATEs ``vault_path`` in place instead of
            # delete-cascading (then re-inserting) the row, which would wipe
            # every incoming backlink pointing at the moved doc.
            return [("move", src, dst)]
        results: list[tuple[_Action, Path, Path | None]] = []
        if src is not None:
            results.append(("delete", src, None))
        if dst is not None:
            results.append(("upsert", dst, None))
        return results or None

    path = _filter_path(_to_path(event.src_path), vault_path)
    if path is None:
        return None
    action: _Action = "delete" if event_type == "deleted" else "upsert"
    return [(action, path, None)]


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
    dest: Path | None = None,
) -> None:
    """Cancel any pending timer for ``path`` and start a new one.

    Each new event for the same path resets the debounce timer to
    ``config.debounce_ms`` from now. Once the path goes ``debounce_ms``
    without a new event, the timer fires and enqueues a job. ``dest`` is the
    rename destination for a ``move`` action (``None`` otherwise); the
    debounce is keyed on ``path`` (the source), so repeated moves of the same
    source coalesce and the last one's destination wins — the desired
    outcome for a burst of rename events.

    If the pending-timer dict overflows (a misbehaving editor or a
    bulk-replace operation), we fall back to "trigger an immediate full
    sync, then clear the buffer" — bounded resource usage trumps
    incremental optimization here.
    """
    delay = max(config.debounce_ms, 0) / 1000.0

    def _fire() -> None:
        with state.lock:
            state.pending_timers.pop(path, None)
        _enqueue(
            state,
            _Job(action=action, abs_path=path, dest_path=dest),
            config=config,
        )

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
) -> None:
    """Push a job onto the worker queue, after the optional test hook."""
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
    graph_syncer: GraphSyncer | None = None,
) -> None:
    """Consume jobs serially. Owns the DB connection.

    Four classes of work, distinguished by ``job.action`` / ``job.abs_path``:

    1. ``upsert`` of a regular file → ``sync_one_file`` (preceded by a
       fence-only-write check; see :func:`_is_fence_only_write`)
    2. ``delete`` of a regular file → DB row removal + link cleanup
       (we don't touch disk; the file is already gone) plus body-cache
       eviction so a re-creation event hits the cold-start path
    3. ``move`` of a regular file → ``_handle_move``: UPDATE the row's
       ``vault_path`` in place (preserving the document id and every
       incoming backlink) then upsert the destination
    4. ``upsert`` of the vault root path → full ``sync_vault`` (the
       overflow-recovery path) plus full body-cache flush so subsequent
       per-file events re-prime the cache from the post-sync state

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
                    link_rewrite=config.link_rewrite,
                    owner_participants=config.owner_participants,
                    graph_syncer=graph_syncer,
                )
                # The full sync may have rewritten any number of fences;
                # the cache entries from before the overflow window are
                # now stale. Clearing forces the next per-file event to
                # take the cold-start path (one extra sync per file,
                # then dedup resumes) — safer than a half-stale map.
                state.body_cache.clear()
            elif job.action == "move" and job.dest_path is not None:
                # Within-vault rename: UPDATE vault_path in place so the
                # document id (and every incoming backlink) survives, then
                # refresh the destination. See :func:`_handle_move`.
                _handle_move(
                    conn,
                    embedder=embedder,
                    config=config,
                    state=state,
                    src_abs=job.abs_path,
                    dst_abs=job.dest_path,
                    graph_syncer=graph_syncer,
                )
            elif job.action == "delete":
                # Defensive re-stat: a "deleted" fsevent can fire spuriously
                # when a file is atomically replaced. ``_atomic.atomic_write_text``
                # does ``os.replace(tmp, path)`` (used by ``rewrite_tags`` for
                # ``brain tag`` and by the sync engine itself), and on macOS
                # APFS the rename can surface as a ``deleted`` on the original
                # path with the follow-up ``created`` dropped or coalesced by
                # watchdog. If we trust the action label blindly we'd
                # ``DELETE FROM documents`` for a perfectly intact file —
                # discovered live on 2026-05-01 by ``brain tag`` round-trip.
                # The on-disk file is the source of truth here: re-stat
                # before deletion. If the file is still there, treat as a
                # plain upsert (which will reconcile any frontmatter changes
                # the producer just made).
                if job.abs_path.exists():
                    logger.debug(
                        "vault watcher: ignoring stale delete for %s "
                        "(file still exists — likely an atomic-replace event)",
                        job.abs_path,
                    )
                    if _is_fence_only_write(state, job.abs_path):
                        state.skipped_fence_only += 1
                    else:
                        sync_one_file(
                            conn,
                            embedder=embedder,
                            vault_path=config.vault_path,
                            file_path=job.abs_path,
                            link_rewrite=config.link_rewrite,
                            owner_participants=config.owner_participants,
                            graph_syncer=graph_syncer,
                        )
                        _refresh_body_cache(state, job.abs_path)
                else:
                    _handle_delete(
                        conn,
                        job.abs_path,
                        config.vault_path,
                        graph_syncer=graph_syncer,
                    )
                    # Drop the cache entry so a future creation event for
                    # the same path is treated as cold-start, not a
                    # spurious fence-only no-op.
                    state.body_cache.pop(job.abs_path, None)
            else:
                if not job.abs_path.exists():
                    # Symmetric to the stale-delete guard above: an
                    # ``upsert`` event can survive past the file's
                    # actual existence, e.g. a transient editor temp
                    # file, a ``brain note new`` followed immediately
                    # by ``brain rm``, or any tool that creates +
                    # removes within the debounce window. Without this
                    # guard ``sync_one_file`` would raise
                    # ``FileNotFoundError``, the outer ``except`` would
                    # bump ``state.errors``, and the shutdown report
                    # would falsely flag a phantom failure. Skip the
                    # job entirely (``continue``) so neither
                    # ``processed`` nor ``errors`` advances — there
                    # was no real work to count.
                    logger.debug(
                        "vault watcher: ignoring stale upsert for %s "
                        "(file no longer exists — likely a transient "
                        "create+delete)",
                        job.abs_path,
                    )
                    continue
                if _is_fence_only_write(state, job.abs_path):
                    state.skipped_fence_only += 1
                    logger.debug(
                        "vault watcher: skipping fence-only write for %s",
                        job.abs_path,
                    )
                else:
                    sync_one_file(
                        conn,
                        embedder=embedder,
                        vault_path=config.vault_path,
                        file_path=job.abs_path,
                        link_rewrite=config.link_rewrite,
                        owner_participants=config.owner_participants,
                        graph_syncer=graph_syncer,
                    )
                    # Re-read after sync so the cache reflects whatever
                    # fence the linker just rewrote — that way the
                    # follow-up filesystem event triggered by our own
                    # write is recognized as fence-only and skipped.
                    _refresh_body_cache(state, job.abs_path)
            state.processed += 1
        except Exception:
            state.errors += 1
            logger.exception(
                "vault watcher: worker failed on %s %s",
                job.action,
                job.abs_path,
            )


def _is_fence_only_write(state: _WatcherState, abs_path: Path) -> bool:
    """Return True iff ``abs_path`` differs from cache only inside the fence.

    Reads the file at ``abs_path``, strips its ``BRAIN_DERIVED_*`` fence
    region, and compares the result to the cached fence-stripped body
    recorded after the most recent successful sync. Equal means the
    write was a fence regeneration (`docs/specs/2026-04-30-derived-edges-in-bodies-design.md`)
    and re-running ``sync_one_file`` would just produce the same rewrite,
    looping forever — caller should skip.

    Returns ``False`` (i.e. don't skip; run sync) when:

    - There's no cache entry for ``abs_path`` (cold start — spec
      requires first-sight syncs to run).
    - The file can't be read (vanished between debounce and worker
      pickup, permissions error, etc.) — let ``sync_one_file`` see the
      same problem and report it through the existing error path.
    - The fence-stripped body genuinely changed.
    """
    try:
        current = abs_path.read_text(encoding="utf-8")
    except OSError:
        return False
    cached = state.body_cache.get(abs_path)
    if cached is None:
        return False
    return cached == strip_fence(current)


def _refresh_body_cache(state: _WatcherState, abs_path: Path) -> None:
    """Update the body cache for ``abs_path`` from its current on-disk content.

    Called after every successful ``sync_one_file`` so the cached
    fence-stripped body reflects whatever the linker just rendered. The
    next watch event for the same path can then detect a fence-only
    rewrite and short-circuit.

    If the file vanished between sync and this read, the stale entry is
    evicted rather than left out of sync with disk.
    """
    try:
        current = abs_path.read_text(encoding="utf-8")
    except OSError:
        state.body_cache.pop(abs_path, None)
        return
    state.body_cache[abs_path] = strip_fence(current)


def _vault_relative_posix(abs_path: Path, vault_path: Path) -> str | None:
    """Return ``abs_path`` relative to the vault as a POSIX string, or None.

    Mirrors the ``resolve()``-then-lexical-fallback ladder used elsewhere in
    the watcher so a vanished path (a delete or move source, already gone
    from disk) still yields the correct relative key without touching the FS.
    Returns ``None`` when the path is outside the vault.
    """
    try:
        return abs_path.relative_to(vault_path.resolve()).as_posix()
    except ValueError:
        try:
            return abs_path.relative_to(vault_path).as_posix()
        except ValueError:
            return None


def _handle_delete(
    conn: psycopg.Connection[Any],
    abs_path: Path,
    vault_path: Path,
    *,
    graph_syncer: GraphSyncer | None = None,
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

    Schema-drift defense: the migration ``003_vault_model.sql`` declares
    a unique partial index ``documents_vault_path_idx`` on
    ``vault_path`` WHERE ``vault_path IS NOT NULL``, so this DELETE
    matches at most one row in normal operation. We still run the SQL
    with ``RETURNING id`` and log a loud ``logger.error`` if the result
    set has more than one row — observability for a future schema bug
    (manual ``ALTER``, dropped index, migration regression). The
    surplus rows are still deleted (we don't raise) so the user isn't
    left with a half-finished delete and orphaned chunks; the error log
    is what surfaces the invariant break.
    """
    relative = _vault_relative_posix(abs_path, vault_path)
    if relative is None:
        logger.debug(
            "vault watcher: delete event for %s outside vault — skipping",
            abs_path,
        )
        return
    deleted = conn.execute(
        "DELETE FROM documents WHERE kind = 'vault' AND vault_path = %s "
        "RETURNING id",
        (relative,),
    ).fetchall()
    if len(deleted) > 1:
        logger.error(
            "vault watcher: deleted %d rows for vault_path=%s — "
            "schema uniqueness invariant broken (expected 0 or 1)",
            len(deleted),
            relative,
        )
    # Wave G1-c: drop each deleted doc from the people graph (best-effort /
    # never-raises; no-op when graph sync is disabled or AGE is absent). The
    # worker connection is autocommit, so the DELETE above already committed.
    if graph_syncer is not None:
        for row in deleted:
            graph_syncer.remove(conn, str(row[0]))


def _handle_move(
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    config: WatchConfig,
    state: _WatcherState,
    src_abs: Path,
    dst_abs: Path,
    graph_syncer: GraphSyncer | None = None,
) -> None:
    """Handle a within-vault markdown rename WITHOUT destroying incoming links.

    The old classifier decomposed a move into ``delete(src) + upsert(dst)``.
    The delete ran ``DELETE FROM documents WHERE vault_path=src`` whose
    ``ON DELETE CASCADE`` wiped every ``links`` row pointing AT the moved doc
    (its incoming backlinks); the follow-up upsert only restored the doc's
    OUTGOING links. Renaming a heavily-linked note therefore silently
    orphaned every backlink until the next full ``brain vault sync``.

    The fix mirrors the full-sync rename semantics: UPDATE the row's
    ``vault_path`` in place so the document id — and thus every incoming
    ``links`` row — survives, then run the normal per-file upsert on the
    destination to refresh content / tags / chunks / outgoing links / mirror
    bookkeeping (it matches the row by frontmatter id and updates it in
    place). The UPDATE is a single statement, so this keeps the DB write
    window no larger than the old DELETE — it does not widen the historically
    deadlock-prone lock footprint.

    A move whose destination has vanished by the time the worker runs
    (a move-then-delete inside the debounce window) can't be applied in
    place — there's no file to sync, and pointing ``vault_path`` at a missing
    file would strand the row. In that case we fall back to a plain delete of
    the source row, symmetric to the stale-upsert existence guard in the
    worker loop.
    """
    if not dst_abs.exists():
        _handle_delete(
            conn, src_abs, config.vault_path, graph_syncer=graph_syncer
        )
        state.body_cache.pop(src_abs, None)
        state.body_cache.pop(dst_abs, None)
        return

    src_rel = _vault_relative_posix(src_abs, config.vault_path)
    dst_rel = _vault_relative_posix(dst_abs, config.vault_path)
    if src_rel is not None and dst_rel is not None and src_rel != dst_rel:
        # Preserve the document id (and every incoming ``links`` row) by
        # moving ``vault_path`` in place instead of delete + re-insert. The
        # worker connection is autocommit, so this commits immediately — the
        # same single-statement footprint as ``_handle_delete``'s DELETE.
        conn.execute(
            "UPDATE documents SET vault_path = %s "
            "WHERE kind = 'vault' AND vault_path = %s",
            (dst_rel, src_rel),
        )

    # Refresh content / mirror bookkeeping on the destination. This matches
    # the row by frontmatter id and updates it in place (full-sync
    # semantics), re-materializing the moved doc's OUTGOING links and retrying
    # any previously-unresolved refs scoped to it.
    sync_one_file(
        conn,
        embedder=embedder,
        vault_path=config.vault_path,
        file_path=dst_abs,
        link_rewrite=config.link_rewrite,
        owner_participants=config.owner_participants,
        graph_syncer=graph_syncer,
    )

    # The source path's cached fence-stripped body is now stale (file gone);
    # refresh the destination so the follow-up filesystem event triggered by
    # sync's own write is recognized as fence-only and skipped.
    state.body_cache.pop(src_abs, None)
    _refresh_body_cache(state, dst_abs)
