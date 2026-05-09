"""Watchdog-driven Quartz rebuild trigger.

Watches the vault for changes and runs :func:`build_and_swap` after a
quiet period. The architecture is the same shape as
``brain.vault.watch`` (Observer thread → handler → debounce timer →
single-worker serialization) but stripped to one job type — "rebuild
the site" — because that's the only thing the wiki layer does.

Design notes:

- Only one build at a time. A ``threading.Lock`` guards
  ``build_and_swap`` so a slow build can't pile up.
- During a build, additional events accumulate but collapse to a single
  follow-up build via a "pending" flag. This means N events fired
  during one build → exactly one rebuild after, not N.
- Editor scratch files (``.#foo``, ``foo~``) and the ``.git`` /
  ``.quartz`` subtrees are filtered before debouncing — there's no
  point waking the build for our own output (loop) or for files
  Quartz won't render anyway.
- ``observer_factory`` is the test seam: production passes ``None`` and
  watchdog's real ``Observer`` is used; tests pass a fake observer
  with an ``inject(event)`` method to drive the handler synchronously.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver as Observer

# brain (2026-05-08): use PollingObserver instead of watchdog's default
# Observer (FSEvents backend). The FSEvents backend silently stops
# delivering events on Python 3.13 — Observer starts cleanly, stays
# alive, but never invokes the handler. See identical comment in
# brain/vault/watch.py for the diagnosis.
from .build_swap import BuildResult, _replace_vault_path, build_and_swap

logger = logging.getLogger(__name__)


# Subdirectories under the vault we never rebuild for. ``.git`` is
# obvious; ``.quartz`` is critical because that's where *we* write the
# build output — without this the watcher would loop on its own writes.
_IGNORED_TOP_DIRS: frozenset[str] = frozenset({".git", ".quartz"})


@dataclass
class _WatcherState:
    """Mutable state shared between Observer thread and timer thread.

    Every field is touched only while holding ``lock``, with two
    intentional exceptions: ``stop_event`` is a thread-safe Event
    (its own lock), and ``timer`` itself is replaced under ``lock``
    but the cancel/start methods are safe to call from outside.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    timer: threading.Timer | None = None
    # Build serialization: only one ``build_and_swap`` call runs at a
    # time. ``running`` is true between "build started" and "build
    # finished"; ``pending`` is true if any event arrived during a
    # build, so the worker knows to schedule one follow-up rebuild.
    build_lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    pending: bool = False
    # refresh_related single-flight: same pattern as build_lock/running/
    # pending, but for the post-build refresh_related call. Builds skip
    # refresh_related inline (it costs ~70s on a 1100-doc vault); this
    # state coordinates the daemon thread that runs it after each build.
    refresh_lock: threading.Lock = field(default_factory=threading.Lock)
    refresh_running: bool = False
    refresh_pending: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)


def run_watcher(
    vault: Path,
    *,
    quartz_dir: Path | None = None,
    debounce_seconds: float = 1.5,
    keep: int = 3,
    on_build: Callable[[BuildResult], None] | None = None,
    observer_factory: Callable[[], BaseObserver] | None = None,
    refresh_runner: Callable[[], None] | None = None,
) -> None:
    """Block until the stop event fires, rebuilding on debounced events.

    ``vault`` is watched recursively. ``quartz_dir`` defaults to
    ``vault/.quartz`` and is forwarded to :func:`build_and_swap`.
    ``debounce_seconds`` is the quiet period before any event triggers a
    build; rapid bursts (1000 events from a save-format-save IDE cycle)
    collapse to one build.

    ``keep`` is the post-build GC window — see :func:`build_and_swap`.

    ``on_build`` fires once per *successful* build with the
    ``BuildResult``. Production wires this to a logging hook so the
    user can tail ``/tmp/brain-build.log`` and see one line per build.
    Tests use it to capture and assert on results.

    ``observer_factory`` defaults to watchdog's stock ``Observer``;
    tests pass a fake to drive events synchronously without touching
    the real FSEvents/inotify subsystem.

    ``refresh_runner`` is forwarded to :class:`_Handler` as its
    background-refresh injection seam. When ``None`` (the default),
    the handler runs the real ``refresh_related`` DB/vault path after
    each build. Tests pass a no-op or spy runner here so they can
    exercise handler logic without touching the real DB or vault. See
    :class:`_Handler` for the full contract.

    The function returns when ``state.stop_event`` is set — production
    flips it from a SIGINT/SIGTERM handler installed by :func:`main`;
    tests flip it directly.
    """
    state = _WatcherState()
    handler = _Handler(
        state=state,
        vault=vault,
        quartz_dir=quartz_dir,
        debounce_seconds=debounce_seconds,
        keep=keep,
        on_build=on_build,
        refresh_runner=refresh_runner,
    )
    factory: Callable[[], BaseObserver] = observer_factory or Observer
    observer = factory()
    # brain (2026-05-08): schedule per non-hidden top-level subdir + root
    # non-recursively, instead of `recursive=True` on the vault root.
    # PollingObserver snapshots every file in the watched tree per poll
    # cycle; with `recursive=True` on a vault containing
    # `<vault>/.quartz/node_modules` (~60k+ files) the snapshot starves
    # the polling thread and edits stop being detected. _should_trigger
    # already filters hidden dirs from delivered events, so excluding
    # them from the SCANNED tree is correct + cheap.
    scheduled_names: list[str] = []
    observer.schedule(handler, str(vault), recursive=False)
    for child in vault.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            observer.schedule(handler, str(child), recursive=True)
            scheduled_names.append(child.name)
    logger.info(
        "wiki watcher: scoped polling on top-level dirs: %s",
        ", ".join(scheduled_names) or "(none)",
    )
    observer.start()

    try:
        state.stop_event.wait()
    finally:
        # Cancel any pending debounce timer so it doesn't fire after we
        # return — otherwise the test's tmp_path may already be torn
        # down by the time the timer thread runs.
        with state.lock:
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None
        observer.stop()
        observer.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Handler + filtering.
# ---------------------------------------------------------------------------


class _Handler(FileSystemEventHandler):
    """Watchdog handler that gates events into the debounce + build path.

    Kept thin: filtering lives in :func:`_should_trigger`, debouncing
    lives in :func:`_schedule`, the actual build lives in
    :func:`_run_build`. The handler just wires them together so each
    piece is independently testable.

    ``refresh_runner`` — optional injection seam for the background
    refresh path. When ``None`` (the default), each refresh cycle calls
    :meth:`_run_refresh_related_once`, which loads ``Config`` and calls
    the real ``refresh_related`` against the DB and vault. When a
    runner is provided, the loop calls it instead and skips the
    ``Config.load()`` / ``refresh_related()`` path entirely. The
    runner must be thread-safe: it is invoked from the daemon refresh
    thread, not the main thread. It must run synchronously to
    completion before returning; the loop's drain check depends on it.
    Tests use this seam to inject a spy or a no-op so they can
    exercise handler logic without touching the real DB or vault.
    """

    def __init__(
        self,
        *,
        state: _WatcherState,
        vault: Path,
        quartz_dir: Path | None,
        debounce_seconds: float,
        keep: int,
        on_build: Callable[[BuildResult], None] | None,
        refresh_runner: Callable[[], None] | None = None,
    ) -> None:
        self._state = state
        self._vault = vault
        self._quartz_dir = quartz_dir
        self._debounce_seconds = debounce_seconds
        self._keep = keep
        self._on_build = on_build
        self._refresh_runner = refresh_runner

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Funnel everything through one classifier so the filter set
        # stays in one place. ``on_any_event`` fires for every type
        # (created/modified/deleted/moved) including directory events,
        # which we drop unconditionally.
        try:
            if not _should_trigger(event, self._vault):
                return
        except Exception:
            # A misbehaving event (bad utf-8 path, etc.) must never
            # kill the Observer thread. Log and move on.
            logger.exception("wiki watcher: filter raised on %r", event)
            return
        self._schedule()

    def _schedule(self) -> None:
        """Reset the debounce timer to ``debounce_seconds`` from now."""
        with self._state.lock:
            if self._state.timer is not None:
                self._state.timer.cancel()
            timer = threading.Timer(self._debounce_seconds, self._fire)
            timer.daemon = True
            self._state.timer = timer
            timer.start()

    def _fire(self) -> None:
        """Debounce expired — request a build (or queue one)."""
        with self._state.lock:
            self._state.timer = None

        # If a build is already running, just flag a follow-up rebuild
        # and return immediately. The worker that's currently building
        # will see the flag when it finishes and run exactly one more
        # build, no matter how many ``_fire`` calls land during the
        # in-flight build.
        with self._state.build_lock:
            if self._state.running:
                self._state.pending = True
                return
            self._state.running = True

        try:
            self._run_build()
        finally:
            self._drain_pending()

    def _drain_pending(self) -> None:
        """Run a single follow-up build if one was queued during the last build.

        Loops because a follow-up build is itself an opportunity for
        more events to land while it runs — but at every iteration we
        only run *one* build, so even a constantly-saving editor can't
        starve us out.
        """
        while True:
            with self._state.build_lock:
                if not self._state.pending:
                    self._state.running = False
                    return
                self._state.pending = False
            # _run_build already logs via its own try/except; suppressing
            # here just keeps the drain loop alive across transient
            # failures so a queued follow-up rebuild isn't stranded.
            with contextlib.suppress(Exception):
                self._run_build()

    def _run_build(self) -> None:
        """Call :func:`build_and_swap`; log + ignore failures."""
        try:
            result = build_and_swap(
                self._vault,
                quartz_dir=self._quartz_dir,
                keep=self._keep,
                # Skip the inline ~70s hybrid-search recompute. The
                # post-build refresh thread below picks it up so the
                # next build emits fresh related-docs JSON without
                # blocking edit-to-UI on this one.
                refresh_related_inline=False,
            )
        except Exception:
            logger.exception("wiki watcher: build failed")
            return
        # Build succeeded — kick off (or queue) a background
        # refresh_related run. Single-flight: if one's already in
        # flight, mark pending so it re-runs once after it finishes.
        self._schedule_refresh_related()
        if self._on_build is not None:
            try:
                self._on_build(result)
            except Exception:
                logger.exception("wiki watcher: on_build hook raised")

    def _schedule_refresh_related(self) -> None:
        """Spawn a daemon thread to run refresh_related, single-flight.

        If a refresh is already running, just set ``refresh_pending`` —
        the running thread's drain loop will see the flag when it
        finishes and run exactly one more refresh.
        """
        with self._state.refresh_lock:
            if self._state.refresh_running:
                self._state.refresh_pending = True
                return
            self._state.refresh_running = True
        t = threading.Thread(
            target=self._refresh_related_loop,
            name="brain-wiki-refresh-related",
            daemon=True,
        )
        t.start()

    def _refresh_related_loop(self) -> None:
        """Run ``refresh_related`` until no more pending flag is set.

        Mirrors ``_drain_pending`` for builds: after each refresh the
        thread checks if another build landed during its run, and if
        so runs exactly one more refresh. This keeps the related-docs
        JSON converging on the latest DB state without spawning
        unbounded concurrent refreshes.

        When ``self._refresh_runner`` is set (injected via
        :class:`_Handler.__init__`), that runner is invoked instead
        of :meth:`_run_refresh_related_once` — no ``Config.load()``
        or ``refresh_related`` is called. Production-default behavior
        is ``None``; tests inject a runner here.
        """
        while True:
            try:
                if self._refresh_runner is not None:
                    self._refresh_runner()
                else:
                    self._run_refresh_related_once()
            except Exception:
                # refresh_related catches its own DB/IO errors and returns
                # a summary; getting here means an unexpected programmer
                # error. Log and let the drain decide whether to retry.
                logger.exception("wiki watcher: refresh_related raised")
            with self._state.refresh_lock:
                if not self._state.refresh_pending:
                    self._state.refresh_running = False
                    return
                self._state.refresh_pending = False

    def _run_refresh_related_once(self) -> None:
        """Best-effort single invocation of ``refresh_related``."""
        # Imports inside the function so a Config-load failure (no
        # DATABASE_URL on PATH for whatever reason) doesn't kill the
        # whole watcher process at startup.
        try:
            from ..config import Config, ConfigError
            from .build_related import refresh_related
        except ImportError:
            logger.exception("wiki watcher: refresh_related import failed")
            return
        try:
            cfg = Config.load()
        except ConfigError as exc:
            logger.warning(
                "wiki watcher: refresh_related skipped (Config.load failed: %s)",
                exc,
            )
            return
        target_vault = self._vault.expanduser().resolve()
        cfg_for_build = (
            cfg
            if cfg.vault_path == target_vault
            else _replace_vault_path(cfg, target_vault)
        )
        refresh_related(cfg_for_build)


def _should_trigger(event: FileSystemEvent, vault: Path) -> bool:
    """Return True iff this event should reset the debounce timer.

    Filtering rules:

    - Directory events skipped — Quartz cares about file content, not
      directory mtime.
    - Paths under ``<vault>/.git`` or ``<vault>/.quartz`` skipped —
      ``.git`` is irrelevant to the rendered site, and ``.quartz``
      contains our *own* build output (a loop trigger).
    - Editor-artifact paths skipped — Emacs lock files (``.#foo``)
      and tilde-suffix backups (``foo~``) shouldn't kick a 40s build.
    - Everything else (any extension, any depth) accepted: a vault
      that includes images, custom CSS, or non-``.md`` source files
      should still rebuild on those changes.
    """
    if event.is_directory:
        return False
    raw = getattr(event, "src_path", None)
    if not raw:
        return False
    path = _to_path(raw)
    if not _within(path, vault):
        # Watchdog can occasionally surface paths outside the watched
        # tree (e.g. when a file is moved out); we don't rebuild for
        # those.
        return False
    name = path.name
    if name.startswith(".#") or name.endswith("~"):
        return False
    try:
        relative = path.resolve().relative_to(vault.resolve())
    except (OSError, ValueError):
        try:
            relative = path.relative_to(vault)
        except ValueError:
            return False
    parts = relative.parts
    if not parts:
        return False
    if parts[0] in _IGNORED_TOP_DIRS:
        return False
    # Exclude our own writes to <vault>/static/related/<slug>.json — the
    # post-build refresh thread writes those, and a fresh build is the
    # ONLY thing that would publish them. If we treat those writes as
    # rebuild triggers we get an infinite loop:
    #   build → spawn refresh thread → write JSON → fsevent → another
    #   build → spawn another refresh → another JSON write → …
    # See `_schedule_refresh_related` in this module.
    return not (len(parts) >= 2 and parts[0] == "static" and parts[1] == "related")


def _to_path(raw: str | bytes) -> Path:
    """Decode a watchdog event path (``bytes | str``) to ``Path``.

    Mirrors ``brain.vault.watch._to_path`` — Linux inotify can surface
    raw bytes; macOS FSEvents always sends str; we normalize eagerly.
    """
    if isinstance(raw, bytes):
        return Path(os.fsdecode(raw))
    return Path(raw)


def _within(path: Path, vault: Path) -> bool:
    """Cheap lexical check: is ``path`` under ``vault``?

    Used as a pre-filter before the more expensive ``resolve()`` call
    in :func:`_should_trigger` — handles the common case where the
    event path is already absolute and the vault path is canonical.
    """
    try:
        path.relative_to(vault)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Console entry — ``python -m brain.wiki.build_watcher --vault ...``.

    Parses CLI flags, optionally runs an initial synchronous build (for
    cold starts where ``current/`` doesn't exist yet), then enters the
    watcher loop and blocks until SIGINT/SIGTERM. ``brain-up`` spawns
    this as a background process; ``brain-down`` kills it via PID file.
    """
    parser = argparse.ArgumentParser(
        prog="brain.wiki.build_watcher",
        description="Watch a vault and trigger atomic Quartz rebuilds.",
    )
    parser.add_argument("--vault", required=True, type=Path, help="Vault root path.")
    parser.add_argument(
        "--quartz-dir",
        type=Path,
        default=None,
        help="Quartz workspace dir (default: <vault>/.quartz).",
    )
    parser.add_argument(
        "--debounce-seconds",
        type=float,
        default=1.5,
        help="Quiet period before a rebuild (default: 1.5s).",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=3,
        help="How many old build dirs to retain after each swap (default: 3).",
    )
    parser.add_argument(
        "--initial-build",
        action="store_true",
        help="Run one synchronous build before starting the watcher.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    vault: Path = args.vault.expanduser().resolve()
    quartz_dir: Path | None = (
        args.quartz_dir.expanduser().resolve() if args.quartz_dir is not None else None
    )

    if args.initial_build:
        _run_initial_build(vault, quartz_dir=quartz_dir, keep=args.keep)

    logger.info("wiki watcher: started for vault=%s", vault)
    run_watcher(
        vault,
        quartz_dir=quartz_dir,
        debounce_seconds=args.debounce_seconds,
        keep=args.keep,
    )


def _run_initial_build(
    vault: Path, *, quartz_dir: Path | None, keep: int
) -> BuildResult | None:
    """Run one synchronous ``build_and_swap`` before the watcher starts.

    Surfaced as a separate function so :func:`main` stays small and
    tests can drive the cold-start path directly without spinning up
    the whole watcher. Returns the build result on success, ``None``
    on failure (failures are logged; the watcher still starts so the
    next vault edit gets another shot).
    """
    try:
        result = build_and_swap(vault, quartz_dir=quartz_dir, keep=keep)
    except Exception:
        logger.exception("wiki watcher: initial build failed")
        return None
    logger.info(
        "wiki watcher: initial build %s done in %.2fs",
        result.build_id,
        result.elapsed_seconds,
    )
    return result


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in CLI
    main(sys.argv[1:])
