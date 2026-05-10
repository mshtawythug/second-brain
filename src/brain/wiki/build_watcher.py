"""Watchdog-driven Quartz rebuild trigger.

Watches the vault for changes and runs :func:`build_and_swap` after a
quiet period. The architecture is the same shape as
``brain.vault.watch`` (Observer thread → handler → debounce timer →
single-worker serialization) but stripped to one job type — "rebuild
the site" — because that's the only thing the wiki layer does.

Design notes:

- Only one build at a time. A ``threading.Lock`` guards
  ``build_and_swap`` so a slow build can't pile up.
- During a build, additional events accumulate in ``pending_batch``
  and drain into the next build after the in-flight build finishes.
  This means N events fired during one build → exactly one rebuild
  after (carrying all accumulated paths), not N.
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
import time
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
from .build_partial import BrainWikiPartialBuildError, run_build_partial
from .build_swap import BuildResult, _replace_vault_path, build_and_swap
from .edit_classifier import EditClassification, classify_edit
from .fastpath_state import FastpathState, FastpathStateError, write_state
from .slug import slugify_source_path

logger = logging.getLogger(__name__)

# Fast-path routing enabled by default.  Set ``BRAIN_FASTPATH_ENABLED=false``
# (or ``0`` / ``no``) at process startup to force every debounced edit through
# the full ``build_and_swap`` path — useful for bisecting routing regressions
# without restarting Python.  Read once at module load so the value is stable
# for the lifetime of the watcher process.
_FASTPATH_ENABLED: bool = os.environ.get("BRAIN_FASTPATH_ENABLED", "true").lower() not in (
    "false",
    "0",
    "no",
)


# Subdirectories under the vault we never rebuild for. ``.git`` is
# obvious; ``.quartz`` is critical because that's where *we* write the
# build output — without this the watcher would loop on its own writes.
_IGNORED_TOP_DIRS: frozenset[str] = frozenset({".git", ".quartz"})


def _is_single_md_file(batch: frozenset[Path]) -> bool:
    """Return True iff ``batch`` contains exactly one Markdown (``.md``) file.

    The fast path only handles single-file Markdown edits — multi-file
    batches (including mixed batches of ``.md`` + other types) always route
    to the full ``build_and_swap`` path.  Non-``.md`` files (images, CSS,
    JavaScript assets) are similarly never eligible for partial emit.
    """
    if len(batch) != 1:
        return False
    return next(iter(batch)).suffix.lower() == ".md"


@dataclass
class _WatcherState:
    """Mutable state shared between Observer thread and timer thread.

    Every field is touched only while holding ``lock``, with two
    intentional exceptions: ``stop_event`` is a thread-safe Event
    (its own lock), and ``timer`` itself is replaced under ``lock``
    but the cancel/start methods are safe to call from outside.

    Batch queue design (T6b):

    ``current_batch`` accumulates paths during the debounce window (i.e.
    between event arrival and _fire). When _fire runs and no build is in
    flight, ``current_batch`` is atomically drained into the build call.

    When _fire runs while a build IS in flight, paths move into
    ``pending_batch`` instead.  After the in-flight build finishes,
    ``_drain_pending`` drains ``pending_batch`` into a follow-up build.
    This ensures no path is ever dropped — contrast the old ``pending:
    bool`` design where only a "there is something pending" flag was kept
    and the actual paths were lost.

    All reads and writes of ``current_batch`` and ``pending_batch`` MUST
    be performed while holding ``lock``.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    timer: threading.Timer | None = None
    # Paths that triggered events during the current debounce window.
    # Populated by on_any_event (under ``lock``).  _fire drains this
    # atomically: either into a new build (no in-flight build) or into
    # ``pending_batch`` (in-flight build running).
    current_batch: set[Path] = field(default_factory=set)
    # Paths that arrived while a build was in flight.  Drained by
    # _drain_pending once the in-flight build completes.
    pending_batch: set[Path] = field(default_factory=set)
    # Build serialization: only one build call runs at a time.
    # ``running`` is true between "build started" and "build finished".
    # ``pending_batch`` being non-empty implies a follow-up build is needed.
    running: bool = False
    # refresh_related single-flight: same pattern as running/pending,
    # but for the post-build refresh_related call. Builds skip
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
        # Part 4 (T6b): read state.json on startup for telemetry only.
        # We use the state purely for logging — it never overrides routing.
        self._read_startup_state()

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
        # Accumulate the path for batch-size routing detection (T6a/T6b).
        # If a build is already in flight, route to pending_batch so the
        # path is preserved for the follow-up build.  Otherwise add to
        # current_batch for the next debounce fire.
        # All reads/writes of current_batch and pending_batch are
        # protected by state.lock.
        #
        # Fix (T6b stale-timer): only schedule a debounce timer when the
        # path goes into current_batch (i.e. no build is in flight).
        # When running=True the path enters pending_batch and _drain_pending
        # will start the follow-up build — no timer is needed or wanted.
        # Scheduling a timer here when running=True would leave a stale timer
        # that fires after _drain_pending clears current_batch; _fire would
        # then see running=False + empty current_batch and call
        # _run_build(frozenset()), routing an empty batch to _do_full_build().
        should_schedule = False
        raw = getattr(event, "src_path", None)
        if raw:
            path = _to_path(raw)
            with self._state.lock:
                if self._state.running:
                    self._state.pending_batch.add(path)
                else:
                    self._state.current_batch.add(path)
                    should_schedule = True
        if should_schedule:
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
        """Debounce expired — request a build (or queue one).

        Batch-queue semantics (T6b):

        - If a build is in flight (``running=True``): drain ``current_batch``
          INTO ``pending_batch`` (union), clear ``current_batch``, and return.
          The in-flight build's ``_drain_pending`` will pick up the merged
          pending_batch when it finishes.
        - Otherwise: drain ``current_batch`` → ``batch`` (frozenset), set
          ``running=True``, clear ``current_batch``, then run the build
          outside the lock.

        All state reads/writes are under ``state.lock``,
        which is the single lock covering both batch sets and ``running``.
        """
        with self._state.lock:
            self._state.timer = None
            if self._state.running:
                # In-flight build: preserve paths in pending_batch.
                self._state.pending_batch.update(self._state.current_batch)
                self._state.current_batch.clear()
                return
            # Fix (T6b stale-timer): defense in depth — if current_batch is
            # empty (e.g. a stale timer fired after _drain_pending already
            # consumed the batch and cleared running), return without starting
            # a build.  Without this guard, _run_build(frozenset()) routes an
            # empty batch to _do_full_build(), triggering a spurious full
            # rebuild.  With Fix #1 this path should be unreachable in normal
            # operation, but we defend against any future code path that might
            # install a timer without a corresponding current_batch entry.
            if not self._state.current_batch:
                return
            # No in-flight build: drain current_batch and start one.
            batch: frozenset[Path] = frozenset(self._state.current_batch)
            self._state.current_batch.clear()
            self._state.running = True

        try:
            self._run_build(batch)
        finally:
            self._drain_pending()

    def _drain_pending(self) -> None:
        """Run a single follow-up build if paths were queued during the last build.

        Loops because a follow-up build is itself an opportunity for
        more events to land while it runs — but at every iteration we
        only run *one* build, so even a constantly-saving editor can't
        starve us out.

        Batch-queue semantics (T6b):

        At each iteration:
        - If ``pending_batch`` is empty → clear ``running`` and return.
        - Otherwise → drain ``pending_batch`` → follow_batch (frozenset),
          keep ``running=True``, then run the follow-up build outside the lock.
          ``pending_batch`` is cleared atomically so any events arriving during
          the follow-up build accumulate into a fresh ``pending_batch`` and will
          be drained by the *next* iteration.
        """
        while True:
            with self._state.lock:
                if not self._state.pending_batch:
                    self._state.running = False
                    return
                follow_batch: frozenset[Path] = frozenset(self._state.pending_batch)
                self._state.pending_batch.clear()
                # running stays True — we're about to run another build.
            # _run_build already logs via its own try/except; suppressing
            # here just keeps the drain loop alive across transient
            # failures so a queued follow-up rebuild isn't stranded.
            with contextlib.suppress(Exception):
                self._run_build(follow_batch)

    def _run_build(self, batch: frozenset[Path]) -> None:
        """Route ``batch`` to the fast path or the full build.

        Fast path is attempted when:
        - :data:`_FASTPATH_ENABLED` is ``True`` (env gate), AND
        - exactly one Markdown file changed in the debounce window.

        All other cases (multi-file batch, non-md file, fast-path
        failure) fall through to :meth:`_do_full_build`.
        """
        if _FASTPATH_ENABLED and _is_single_md_file(batch):
            changed_file = next(iter(batch))
            try:
                self._try_fast_path(changed_file)
            except Exception:
                logger.exception(
                    "wiki watcher: fast-path routing failed unexpectedly;"
                    " falling back to full build"
                )
                self._do_full_build()
        else:
            self._do_full_build()

    def _try_fast_path(self, changed_file: Path) -> None:
        """Attempt a fast partial emit; fall back to full build on any failure.

        Routing (in order):
        1. Compute slug — ValueError (path outside vault) → full build.
        2. Unsupported-slug pre-check (index / tags/* / */index) → full build.
        3. Classify via manifest fingerprint — NON_TRIVIAL → full build.
        4. Resolve workspace/current → missing → full build.
        5. Call :func:`run_build_partial`; :class:`BrainWikiPartialBuildError`
           → log warning → full build.
        6. Success → log timing + schedule refresh_related.
        """
        workspace = (
            self._quartz_dir if self._quartz_dir is not None else self._vault / ".quartz"
        )

        # Step 1: slug computation — ValueError means path outside vault.
        try:
            slug = slugify_source_path(changed_file, self._vault)
        except ValueError:
            logger.debug(
                "wiki watcher: fast-path slug error (path outside vault=%s) → full build",
                self._vault,
            )
            self._do_full_build()
            return

        # Step 2: unsupported-slug pre-check.  These slug shapes require a
        # full build; skipping the subprocess avoids subprocess overhead for a
        # guaranteed-full-build case.  The Node guard (T4 exit 6) remains as a
        # direct-CLI safety net for callers that bypass this Python layer.
        if slug == "index" or slug.startswith("tags/") or slug.endswith("/index"):
            logger.debug(
                "wiki watcher: unsupported fast-path slug %r → full build", slug
            )
            self._do_full_build()
            return

        # Step 3: classify the edit via manifest fingerprint.
        fastpath_dir = workspace / ".cache" / "fastpath"
        result = classify_edit(
            fastpath_dir=fastpath_dir,
            source_path=changed_file,
            vault_root=self._vault,
        )
        if result.classification == EditClassification.NON_TRIVIAL:
            logger.debug(
                "wiki watcher: non-trivial edit (reason=%r) → full build",
                result.reason,
            )
            self._do_full_build()
            return

        # Step 4: resolve the active build dir from the workspace/current symlink.
        current_link = workspace / "current"
        if not current_link.exists():
            logger.debug("wiki watcher: no current build dir → full build")
            self._do_full_build()
            return
        try:
            build_dir = current_link.resolve(strict=True)
        except (OSError, RuntimeError):
            logger.debug("wiki watcher: cannot resolve current build dir → full build")
            self._do_full_build()
            return

        # Step 5: attempt the partial build.
        try:
            partial_result = run_build_partial(
                slug=slug,
                vault_dir=self._vault,
                build_dir=build_dir,
                workspace_dir=workspace,
                timeout_s=30.0,
            )
        except BrainWikiPartialBuildError as exc:
            logger.warning(
                "wiki: build-partial failed (kind=%s, slug=%s): %s; falling back to full build",
                exc.kind.value,
                exc.slug,
                exc,
            )
            # Increment partial failure counter in state.json before falling back.
            self._increment_partial_failure_count(fastpath_dir=fastpath_dir)
            self._do_full_build()
            return

        # Step 6: success — update state.json (advisory, non-blocking).
        logger.info(
            "wiki: build-partial slug=%s elapsed=%dms",
            slug,
            partial_result.elapsed_ms,
        )
        self._update_state_after_partial(fastpath_dir=fastpath_dir, slug=slug)
        # Kick off (or queue) the post-build refresh_related run — same
        # single-flight pattern as the full build path.
        self._schedule_refresh_related()

    def _do_full_build(self) -> None:
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
        # Build succeeded — update state.json (advisory, non-blocking).
        workspace = (
            self._quartz_dir if self._quartz_dir is not None else self._vault / ".quartz"
        )
        fastpath_dir = workspace / ".cache" / "fastpath"
        self._update_state_after_full(fastpath_dir=fastpath_dir, build_id=result.build_id)
        # Kick off (or queue) a background refresh_related run.
        # Single-flight: if one's already in flight, mark pending so
        # it re-runs once after it finishes.
        self._schedule_refresh_related()
        if self._on_build is not None:
            try:
                self._on_build(result)
            except Exception:
                logger.exception("wiki watcher: on_build hook raised")

    # ------------------------------------------------------------------
    # state.json helpers (Parts 3 + 4 of T6b)
    # ------------------------------------------------------------------

    def _read_startup_state(self) -> None:
        """Read state.json on watcher startup — advisory / telemetry only.

        Never raises. On missing file, IO error, or parse error, logs at
        INFO level and continues. Watcher_pid mismatch (stale state from
        a prior watcher) is also logged at INFO. The result is NEVER used
        as build-routing authority — only for informational logging.
        """
        workspace = (
            self._quartz_dir if self._quartz_dir is not None else self._vault / ".quartz"
        )
        fastpath_dir = workspace / ".cache" / "fastpath"
        if not fastpath_dir.exists():
            logger.debug(
                "wiki watcher: fastpath_dir %s not found at startup; "
                "state.json will be written on first successful build",
                fastpath_dir,
            )
            return
        from .fastpath_state import read_state  # local import — avoids startup overhead

        try:
            state = read_state(fastpath_dir)
        except FastpathStateError as exc:
            logger.info(
                "wiki watcher: startup state.json unreadable (%s); "
                "treating as fresh start",
                exc,
            )
            return

        if state is None:
            # Missing file or stale PID (read_state returns None for both).
            # Check if there WAS a file with a different PID to emit the
            # stale-PID log line.  We do this by reading raw JSON directly.
            try:
                import json as _json

                raw = (fastpath_dir / "state.json").read_text(encoding="utf-8")
                data = _json.loads(raw)
                prev_pid = data.get("watcher_pid")
                if prev_pid is not None and prev_pid != os.getpid():
                    logger.info(
                        "wiki watcher: previous watcher pid=%s; this is a fresh watcher",
                        prev_pid,
                    )
            except (OSError, ValueError):
                pass  # File missing or malformed — already handled above.
            return

        logger.info(
            "wiki watcher: resumed from state.json (last_full_at_ms=%d, "
            "last_partial_at_ms=%d, consecutive_partial_failures=%d)",
            state.last_full_at_ms,
            state.last_partial_at_ms,
            state.consecutive_partial_failures,
        )

    def _update_state_after_full(self, *, fastpath_dir: Path, build_id: str) -> None:
        """Write state.json after a successful full build.

        Resets ``consecutive_partial_failures`` to 0, records
        ``last_full_at_ms``, and stamps ``watcher_pid``. Non-blocking:
        if the fastpath_dir does not yet exist or any IO error occurs,
        logs a warning and continues — state.json is advisory only.
        """
        if not fastpath_dir.exists():
            # fastpath dir is created by the Node full-build hook (T2).
            # If it doesn't exist yet, state.json cannot be written.
            logger.debug(
                "wiki watcher: fastpath_dir %s missing; skipping state.json write",
                fastpath_dir,
            )
            return
        from .fastpath_state import read_state  # local import avoids circular deps

        try:
            existing = read_state(fastpath_dir)
        except FastpathStateError:
            existing = None

        now_ms = int(time.time() * 1000)
        new_state = FastpathState(
            version=1,
            watcher_pid=os.getpid(),
            last_partial_at_ms=existing.last_partial_at_ms if existing is not None else 0,
            last_full_at_ms=now_ms,
            last_partial_slug=existing.last_partial_slug if existing is not None else None,
            consecutive_partial_failures=0,
        )
        try:
            write_state(fastpath_dir, new_state)
        except FastpathStateError:
            logger.warning(
                "wiki watcher: failed to write state.json after full build (build_id=%s)",
                build_id,
                exc_info=True,
            )

    def _update_state_after_partial(self, *, fastpath_dir: Path, slug: str) -> None:
        """Write state.json after a successful partial build.

        Increments (resets to 0), records ``last_partial_at_ms``, and
        stores the slug for telemetry. Non-blocking on IO error.
        """
        if not fastpath_dir.exists():
            logger.debug(
                "wiki watcher: fastpath_dir %s missing; skipping state.json write",
                fastpath_dir,
            )
            return
        from .fastpath_state import read_state  # local import avoids circular deps

        try:
            existing = read_state(fastpath_dir)
        except FastpathStateError:
            existing = None

        now_ms = int(time.time() * 1000)
        new_state = FastpathState(
            version=1,
            watcher_pid=os.getpid(),
            last_partial_at_ms=now_ms,
            last_full_at_ms=existing.last_full_at_ms if existing is not None else 0,
            last_partial_slug=slug,
            consecutive_partial_failures=0,
        )
        try:
            write_state(fastpath_dir, new_state)
        except FastpathStateError:
            logger.warning(
                "wiki watcher: failed to write state.json after partial build (slug=%s)",
                slug,
                exc_info=True,
            )

    def _increment_partial_failure_count(self, *, fastpath_dir: Path) -> None:
        """Increment ``consecutive_partial_failures`` in state.json.

        Called on a partial-build failure so the watcher can force a
        full build after N consecutive partial failures (safety net —
        caller responsibility; this method only increments the counter).
        Non-blocking on IO error.
        """
        if not fastpath_dir.exists():
            logger.debug(
                "wiki watcher: fastpath_dir %s missing; skipping partial failure counter update",
                fastpath_dir,
            )
            return
        from .fastpath_state import read_state  # local import avoids circular deps

        try:
            existing = read_state(fastpath_dir)
        except FastpathStateError:
            existing = None

        current_failures = (
            existing.consecutive_partial_failures if existing is not None else 0
        )
        new_state = FastpathState(
            version=1,
            watcher_pid=os.getpid(),
            last_partial_at_ms=existing.last_partial_at_ms if existing is not None else 0,
            last_full_at_ms=existing.last_full_at_ms if existing is not None else 0,
            last_partial_slug=existing.last_partial_slug if existing is not None else None,
            consecutive_partial_failures=current_failures + 1,
        )
        try:
            write_state(fastpath_dir, new_state)
        except FastpathStateError:
            logger.warning(
                "wiki watcher: failed to write state.json for partial failure counter",
                exc_info=True,
            )

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
