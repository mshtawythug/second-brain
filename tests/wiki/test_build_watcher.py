"""Unit tests for ``brain.wiki.build_watcher``.

The watcher is a thin wrapper around watchdog's ``Observer`` plus a
debouncer + build lock. We swap two things in tests:

1. The Observer itself, via ``observer_factory=`` — a ``_FakeObserver``
   captures the handler so the test can drive synthetic events
   synchronously rather than relying on FSEvents/inotify.
2. ``build_and_swap`` itself, via ``mocker.patch`` — :func:`build_and_swap`
   shells out to ``npx`` which we don't want in unit tests.
   ``mocker.patch`` is pytest-mock with automatic cleanup, NOT
   production monkey-patching — fine per CLAUDE.md.

Tests don't sleep blindly; they synchronize on either ``threading.Event``s
fired from the patched ``build_and_swap`` body, or short bounded waits
guarded by ``_wait_for(predicate)`` helpers.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture
from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
)

from brain.wiki.build_swap import BuildResult
from brain.wiki.build_watcher import (
    _Handler,
    _run_initial_build,
    _should_trigger,
    _WatcherState,
    main,
    run_watcher,
)

# ---------------------------------------------------------------------------
# Fake watchdog Observer.
# ---------------------------------------------------------------------------


class _FakeObserver:
    """In-process stand-in for ``watchdog.observers.Observer``.

    Captures the handler passed to ``schedule`` so tests can later
    inject events directly via :meth:`inject`. Mirrors the real
    Observer's ``start/stop/join`` surface but does no real work.

    A class-level ``instances`` registry lets the test fish out the
    observer that the watcher created for it without plumbing through
    a return value.
    """

    instances: list[_FakeObserver] = []

    def __init__(self) -> None:
        self.handler: Any = None
        self.scheduled_path: str | None = None
        self.started = False
        self.stopped = False
        _FakeObserver.instances.append(self)

    def schedule(self, handler: Any, path: str, recursive: bool = False) -> None:
        self.handler = handler
        self.scheduled_path = path

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        return None

    def inject(self, event: Any) -> None:
        """Route a synthetic event through the registered handler."""
        assert self.handler is not None, "schedule() must be called first"
        self.handler.on_any_event(event)


@pytest.fixture(autouse=True)
def _reset_fake_observers() -> None:
    """Drop any per-test Observer instances between tests."""
    _FakeObserver.instances.clear()


def _wait_for(predicate: Any, timeout: float = 2.0) -> None:
    """Block until ``predicate()`` returns truthy or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("predicate never became true")


def _start_watcher(
    *,
    vault: Path,
    debounce_seconds: float,
    on_build: Any = None,
    refresh_runner: Any = None,
) -> tuple[threading.Thread, _FakeObserver, Any]:
    """Run ``run_watcher`` in a background thread.

    Returns the thread, the FakeObserver the watcher set up, and a
    ``stop()`` callable that flips the watcher's internal stop event
    so the thread exits cleanly. Tests must call ``stop()`` and join.

    ``refresh_runner`` is forwarded to :func:`run_watcher` as its
    background-refresh injection seam. Defaults to a no-op so tests
    that don't supply a runner are isolated from the real
    ``Config.load()`` / ``refresh_related()`` DB+vault path.
    Pass an explicit spy if you need to assert on refresh calls.
    """
    # Default to a no-op so tests that omit refresh_runner don't
    # accidentally fire the real Config.load + refresh_related path
    # against the live DB/vault via the daemon thread.
    if refresh_runner is None:
        refresh_runner = lambda: None  # noqa: E731
    # The watcher creates the observer via observer_factory — we
    # smuggle a reference out via the class-level registry and a
    # ``stop_event`` reference via a captured closure.
    captured_state: dict[str, Any] = {}

    def _factory() -> _FakeObserver:
        return _FakeObserver()

    def _target() -> None:
        # We monkey via the module's _WatcherState path: the watcher
        # owns its own state internally, but we don't need to reach in
        # for it — the observer.stop() in run_watcher's finally is
        # triggered by setting the state's stop_event indirectly.
        # Simpler: use ``main`` -- no, we want full control. Just call
        # run_watcher and stop it via observer.stop on test teardown.
        run_watcher(
            vault,
            debounce_seconds=debounce_seconds,
            observer_factory=_factory,
            on_build=on_build,
            refresh_runner=refresh_runner,
        )

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    _wait_for(lambda: bool(_FakeObserver.instances), timeout=2.0)
    observer = _FakeObserver.instances[0]
    _wait_for(lambda: observer.started, timeout=2.0)

    captured_state["thread"] = thread
    captured_state["observer"] = observer
    return thread, observer, captured_state


# ---------------------------------------------------------------------------
# Filter unit tests — pure, no observer / thread machinery.
# ---------------------------------------------------------------------------


def test_should_trigger_skips_directory_events(tmp_path: Path) -> None:
    """Directory events (mkdir / rmdir) shouldn't kick a build."""
    from watchdog.events import DirCreatedEvent

    event = DirCreatedEvent(str(tmp_path / "subdir"))
    assert _should_trigger(event, tmp_path) is False


def test_should_trigger_accepts_md_change(tmp_path: Path) -> None:
    """A vault-relative ``.md`` modification triggers a rebuild."""
    note = tmp_path / "note.md"
    note.write_text("hi")
    event = FileModifiedEvent(str(note))
    assert _should_trigger(event, tmp_path) is True


def test_should_trigger_accepts_non_md_files(tmp_path: Path) -> None:
    """Non-``.md`` files (CSS, images) still trigger — Quartz cares about them."""
    asset = tmp_path / "logo.png"
    asset.write_text("png-bytes")
    event = FileModifiedEvent(str(asset))
    assert _should_trigger(event, tmp_path) is True


def test_should_trigger_skips_quartz_subtree(tmp_path: Path) -> None:
    """Events under ``<vault>/.quartz/`` are ignored (loop guard)."""
    target = tmp_path / ".quartz" / "builds" / "x" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("<html></html>")
    event = FileModifiedEvent(str(target))
    assert _should_trigger(event, tmp_path) is False


def test_should_trigger_skips_git_subtree(tmp_path: Path) -> None:
    """Events under ``<vault>/.git/`` are ignored."""
    target = tmp_path / ".git" / "HEAD"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ref: refs/heads/main")
    event = FileModifiedEvent(str(target))
    assert _should_trigger(event, tmp_path) is False


def test_should_trigger_skips_emacs_lock(tmp_path: Path) -> None:
    """``.#foo.md`` (Emacs lockfile) is ignored."""
    target = tmp_path / ".#foo.md"
    # Don't actually need to write it — _should_trigger only inspects
    # the path components for filename-based filters.
    event = FileModifiedEvent(str(target))
    assert _should_trigger(event, tmp_path) is False


def test_should_trigger_skips_tilde_backup(tmp_path: Path) -> None:
    """``foo.md~`` (tilde backup) is ignored."""
    target = tmp_path / "foo.md~"
    event = FileModifiedEvent(str(target))
    assert _should_trigger(event, tmp_path) is False


def test_should_trigger_skips_outside_vault(tmp_path: Path) -> None:
    """Events whose path is outside the vault aren't our concern."""
    other = tmp_path.parent / "outside.md"
    event = FileModifiedEvent(str(other))
    assert _should_trigger(event, tmp_path) is False


# ---------------------------------------------------------------------------
# Debounce + locking integration tests — exercise the full watcher loop
# with the real debounce timer but a patched ``build_and_swap``.
# ---------------------------------------------------------------------------


def _make_result(build_id: str = "20260501-120000-abcdef") -> BuildResult:
    """Synthetic ``BuildResult`` for tests that don't actually run a build."""
    return BuildResult(
        build_dir=Path(f"/tmp/builds/{build_id}"),
        build_id=build_id,
        elapsed_seconds=0.01,
        pruned=[],
        method="output-flag",
    )


def test_debounce_coalesces_events(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """10 modify events within 100ms produce exactly 1 ``build_and_swap`` call.

    Uses the real debouncer (``debounce_seconds=0.05`` for snappy
    tests) and a ``mocker.patch`` on ``build_and_swap`` so we can both
    count calls and avoid running ``npx``.
    """
    patched = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_result(),
    )

    note = tmp_path / "note.md"
    note.write_text("# hi")

    thread, observer, _ = _start_watcher(
        vault=tmp_path, debounce_seconds=0.05
    )
    try:
        for _ in range(10):
            observer.inject(FileModifiedEvent(str(note)))
            time.sleep(0.005)
        # Wait long enough for the debounce timer to fire and the
        # build to complete.
        _wait_for(lambda: patched.call_count >= 1, timeout=2.0)
        # Give any spurious follow-up calls a chance to land before
        # asserting the final count.
        time.sleep(0.2)
    finally:
        # Stop the watcher: setting the observer's handler-state
        # stop_event isn't directly exposed; calling observer.stop()
        # alone won't unblock run_watcher's wait — we need to flip the
        # state.stop_event the watcher allocated. Reach into the
        # handler to find it (the handler holds a reference).
        handler = observer.handler
        handler._state.stop_event.set()
        thread.join(timeout=2.0)

    assert patched.call_count == 1, f"expected 1 call, got {patched.call_count}"


def test_lock_serializes_concurrent_builds(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Events fired during an in-flight build collapse to one follow-up build.

    Make ``build_and_swap`` sleep 0.5s. Fire event A; while it's
    "building", fire event B. Assert exactly two builds run total
    (A's, plus a single coalesced follow-up after A finishes).
    """
    enter_count = threading.Semaphore(0)

    def _slow_build(*_args: Any, **_kwargs: Any) -> BuildResult:
        enter_count.release()
        time.sleep(0.3)
        return _make_result()

    patched = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap", side_effect=_slow_build
    )

    note = tmp_path / "note.md"
    note.write_text("# hi")

    thread, observer, _ = _start_watcher(
        vault=tmp_path, debounce_seconds=0.05
    )
    try:
        observer.inject(FileModifiedEvent(str(note)))
        # Wait until the first build is actually running.
        assert enter_count.acquire(timeout=2.0)
        # Inject 5 more events while build #1 is in flight.
        for _ in range(5):
            observer.inject(FileModifiedEvent(str(note)))
            time.sleep(0.01)
        # Wait for the follow-up coalesced build to enter.
        assert enter_count.acquire(timeout=3.0)
        # No more should be queued — give them a window to fire if they
        # were going to, then assert.
        time.sleep(0.4)
    finally:
        handler = observer.handler
        handler._state.stop_event.set()
        thread.join(timeout=3.0)

    assert patched.call_count == 2, (
        f"expected 2 builds (1 in-flight + 1 coalesced), got {patched.call_count}"
    )


def test_ignores_quartz_events(tmp_path: Path, mocker: MockerFixture) -> None:
    """Events under ``<vault>/.quartz/`` never schedule a build."""
    patched = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_result(),
    )
    target = tmp_path / ".quartz" / "builds" / "foo" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("<html></html>")

    thread, observer, _ = _start_watcher(
        vault=tmp_path, debounce_seconds=0.05
    )
    try:
        for _ in range(5):
            observer.inject(FileModifiedEvent(str(target)))
            time.sleep(0.01)
        # Give the debounce window time to expire and for any errant
        # build to fire.
        time.sleep(0.3)
    finally:
        handler = observer.handler
        handler._state.stop_event.set()
        thread.join(timeout=2.0)

    patched.assert_not_called()


def test_ignores_git_events(tmp_path: Path, mocker: MockerFixture) -> None:
    """Events under ``<vault>/.git/`` never schedule a build."""
    patched = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_result(),
    )
    target = tmp_path / ".git" / "HEAD"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ref: refs/heads/main")

    thread, observer, _ = _start_watcher(
        vault=tmp_path, debounce_seconds=0.05
    )
    try:
        observer.inject(FileModifiedEvent(str(target)))
        time.sleep(0.3)
    finally:
        handler = observer.handler
        handler._state.stop_event.set()
        thread.join(timeout=2.0)

    patched.assert_not_called()


def test_ignores_editor_artifacts(tmp_path: Path, mocker: MockerFixture) -> None:
    """Editor artifacts (``.#foo``, ``foo~``) never schedule a build."""
    patched = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_result(),
    )
    targets = [tmp_path / ".#foo.md", tmp_path / "foo.md~"]

    thread, observer, _ = _start_watcher(
        vault=tmp_path, debounce_seconds=0.05
    )
    try:
        for t in targets:
            observer.inject(FileCreatedEvent(str(t)))
            observer.inject(FileModifiedEvent(str(t)))
        time.sleep(0.3)
    finally:
        handler = observer.handler
        handler._state.stop_event.set()
        thread.join(timeout=2.0)

    patched.assert_not_called()


# ---------------------------------------------------------------------------
# Initial-build + main() entry point.
# ---------------------------------------------------------------------------


def test_initial_build_runs_one_build(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """``_run_initial_build`` calls ``build_and_swap`` exactly once."""
    patched = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_result(),
    )
    result = _run_initial_build(tmp_path, quartz_dir=None, keep=3)
    assert result is not None
    assert patched.call_count == 1
    # Args were forwarded faithfully.
    call = patched.call_args
    assert call.args == (tmp_path,)
    assert call.kwargs == {"quartz_dir": None, "keep": 3}


def test_initial_build_swallows_failure(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """A failed initial build returns None instead of propagating the exception."""
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        side_effect=RuntimeError("boom"),
    )
    result = _run_initial_build(tmp_path, quartz_dir=None, keep=3)
    assert result is None


def test_main_with_initial_build_flag(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """``main(--initial-build)`` runs one build before entering the watch loop.

    We patch :func:`run_watcher` to return immediately so ``main``
    completes without actually entering the observer wait loop, and
    patch :func:`build_and_swap` so we can count calls.
    """
    build_patch = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_result(),
    )
    run_patch = mocker.patch("brain.wiki.build_watcher.run_watcher")
    # Synthesize a vault with a quartz workspace so ``main`` can
    # resolve paths even though we don't actually build.
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hello.md").write_text("# hi")

    main(["--vault", str(vault), "--initial-build", "--debounce-seconds", "0.01"])

    assert build_patch.call_count == 1
    assert run_patch.call_count == 1


def test_main_without_initial_build_skips_build(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """``main`` (no --initial-build) goes straight to the watcher."""
    build_patch = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_result(),
    )
    run_patch = mocker.patch("brain.wiki.build_watcher.run_watcher")
    vault = tmp_path / "vault"
    vault.mkdir()

    main(["--vault", str(vault)])

    assert build_patch.call_count == 0
    assert run_patch.call_count == 1


# ---------------------------------------------------------------------------
# on_build hook coverage.
# ---------------------------------------------------------------------------


def test_on_build_hook_invoked(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """A debounced build calls the supplied ``on_build`` hook with the result."""
    expected = _make_result(build_id="20260501-153912-ab12cd")
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap", return_value=expected
    )

    received: list[BuildResult] = []
    note = tmp_path / "note.md"
    note.write_text("# hi")

    thread, observer, _ = _start_watcher(
        vault=tmp_path, debounce_seconds=0.05, on_build=received.append
    )
    try:
        observer.inject(FileModifiedEvent(str(note)))
        _wait_for(lambda: len(received) >= 1, timeout=2.0)
    finally:
        handler = observer.handler
        handler._state.stop_event.set()
        thread.join(timeout=2.0)

    assert received == [expected]


def test_build_failure_does_not_kill_watcher(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """A raising ``build_and_swap`` is logged + swallowed; subsequent events still trigger."""
    calls = 0

    def _flaky(*_args: Any, **_kwargs: Any) -> BuildResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first build boom")
        return _make_result()

    mocker.patch("brain.wiki.build_watcher.build_and_swap", side_effect=_flaky)

    note = tmp_path / "note.md"
    note.write_text("# hi")

    received: list[BuildResult] = []
    thread, observer, _ = _start_watcher(
        vault=tmp_path, debounce_seconds=0.05, on_build=received.append
    )
    try:
        observer.inject(FileModifiedEvent(str(note)))
        _wait_for(lambda: calls >= 1, timeout=2.0)
        # Second event should still trigger — watcher is alive.
        observer.inject(FileModifiedEvent(str(note)))
        _wait_for(lambda: len(received) >= 1, timeout=2.0)
    finally:
        handler = observer.handler
        handler._state.stop_event.set()
        thread.join(timeout=2.0)

    assert calls >= 2
    assert received  # second build succeeded → on_build received it


# ---------------------------------------------------------------------------
# New tests — A1–A4 for Task 3 of the edit-to-UI latency closeout.
# ---------------------------------------------------------------------------


def test_refresh_related_skipped_inline_in_watcher(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """Pins: _run_build calls build_and_swap with refresh_related_inline=False.

    The watcher deliberately defers the heavy refresh_related (~73s) off
    the build hot path. This test asserts that the kwarg is always False
    when invoked from the watcher, so any future accidental reversion is
    caught immediately.
    """
    spy = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_result(),
    )
    note = tmp_path / "note.md"
    note.write_text("# hi")

    # Inject a no-op refresh_runner so the daemon thread doesn't attempt
    # the real Config.load() / refresh_related() DB path during the test.
    thread, observer, _ = _start_watcher(
        vault=tmp_path,
        debounce_seconds=0.05,
        refresh_runner=lambda: None,
    )
    try:
        observer.inject(FileModifiedEvent(str(note)))
        _wait_for(lambda: spy.call_count >= 1, timeout=2.0)
    finally:
        handler = observer.handler
        handler._state.stop_event.set()
        thread.join(timeout=2.0)

    assert spy.call_count == 1
    # The watcher MUST always pass refresh_related_inline=False to avoid
    # blocking edit-to-UI on the 73s hybrid-search recompute.
    assert spy.call_args.kwargs.get("refresh_related_inline") is False


def test_should_trigger_excludes_static_related(tmp_path: Path) -> None:
    """Pins: _should_trigger returns False for paths under <vault>/static/related/.

    The post-build refresh_related thread writes JSON files there; treating
    those writes as rebuild triggers would cause an infinite build loop.
    Also verifies the correct-trigger side (control cases).
    """
    # Paths that should NOT trigger a rebuild — loop-prevention filter.
    assert (
        _should_trigger(
            FileModifiedEvent(str(tmp_path / "static" / "related" / "foo.json")),
            tmp_path,
        )
        is False
    ), "static/related/foo.json should be filtered out"

    assert (
        _should_trigger(
            FileModifiedEvent(
                str(tmp_path / "static" / "related" / "sub" / "bar.json")
            ),
            tmp_path,
        )
        is False
    ), "static/related/sub/bar.json should be filtered out (nested)"

    # Control: paths that SHOULD trigger — correct positive cases.
    assert (
        _should_trigger(
            FileModifiedEvent(str(tmp_path / "notes" / "foo.md")),
            tmp_path,
        )
        is True
    ), "notes/foo.md should trigger"

    assert (
        _should_trigger(
            FileModifiedEvent(str(tmp_path / "static" / "other.json")),
            tmp_path,
        )
        is True
    ), "static/other.json is NOT under related/ — should trigger"

    assert (
        _should_trigger(
            FileModifiedEvent(str(tmp_path / "_ingested" / "manual" / "x.md")),
            tmp_path,
        )
        is True
    ), "_ingested/manual/x.md should trigger"


def test_refresh_singleflight_pending(tmp_path: Path) -> None:
    """Pins single-flight refresh behavior: concurrent requests collapse to exactly two runs.

    Exercises the Task 2 DI seam (refresh_runner injection):
    - First _schedule_refresh_related() spawns a daemon thread that blocks.
    - Second call while the first is in flight sets refresh_pending=True
      instead of spawning a second thread.
    - Releasing the block causes the loop to drain the pending flag with
      exactly one more run.
    Total runner invocations must be 2, regardless of concurrent scheduling.
    Uses the refresh_runner injection point so no real DB or vault is touched.
    """
    call_log: list[int] = []  # each invocation appends 1; len == invocation count
    block_event = threading.Event()  # controls when the first call unblocks

    def fake_runner() -> None:
        """Record call then block until released; subsequent calls return immediately."""
        call_log.append(1)  # list.append is atomic in CPython
        block_event.wait()

    state = _WatcherState()
    handler = _Handler(
        state=state,
        vault=tmp_path,
        quartz_dir=None,
        debounce_seconds=0.05,
        keep=3,
        on_build=None,
        refresh_runner=fake_runner,
    )

    try:
        # Step 1: First schedule → spawns daemon thread → calls fake_runner → blocks.
        handler._schedule_refresh_related()
        _wait_for(lambda: len(call_log) >= 1, timeout=2.0)

        # Step 2: Second schedule while first is in flight → pending, NOT a new thread.
        handler._schedule_refresh_related()
        with state.refresh_lock:
            assert state.refresh_pending, (
                "Second _schedule_refresh_related() call should set refresh_pending=True "
                "instead of spawning a second concurrent refresh thread."
            )

        # Step 3: Release the block → first runner returns → loop drains pending → runs once more.
        block_event.set()
        _wait_for(lambda: len(call_log) >= 2, timeout=2.0)

        assert len(call_log) == 2, (
            f"Expected exactly 2 runner invocations (1 in-flight + 1 drained), "
            f"got {len(call_log)}"
        )
    finally:
        # Always release so the daemon thread can exit — block_event.set() is
        # idempotent, so this is a no-op on the happy path but prevents the
        # thread from blocking forever if an assertion above fails early.
        block_event.set()
        for t in threading.enumerate():
            if t.name == "brain-wiki-refresh-related":
                t.join(timeout=2.0)
                break


def test_scoped_polling_does_not_pick_up_new_top_level_dir(tmp_path: Path) -> None:
    """Pins: top-level dirs created after watcher start are NOT scheduled for polling.

    ``run_watcher`` calls ``vault.iterdir()`` exactly once at startup to
    schedule each non-hidden top-level subdir with ``recursive=True``. A
    directory created after startup is invisible to the PollingObserver
    until the watcher process is restarted.

    This test ASSERTS that current behavior (Option 2 from
    docs/plans/2026-05-09-edit-to-ui-latency-closeout.md Task 3). If this
    test fails, runtime DirCreated handling has been added — update the
    Known-limitation note in docs/plans/2026-05-09-edit-to-ui-latency.md.
    """
    notes = tmp_path / "notes"
    notes.mkdir()

    # An Observer that records all schedule() calls without actually watching.
    class _ScheduleRecordingObserver:
        def __init__(self) -> None:
            self.schedule_calls: list[tuple[str, bool]] = []
            self.started = False
            self._handler: Any = None

        def schedule(
            self, handler: Any, path: str, recursive: bool = False
        ) -> None:
            self._handler = handler
            self.schedule_calls.append((path, recursive))

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            pass

        def join(self, timeout: Any = None) -> None:
            pass

    observer_holder: list[_ScheduleRecordingObserver] = []

    def _factory() -> _ScheduleRecordingObserver:
        obs = _ScheduleRecordingObserver()
        observer_holder.append(obs)
        return obs

    thread = threading.Thread(
        target=run_watcher,
        args=(tmp_path,),
        kwargs={
            "observer_factory": _factory,
            "debounce_seconds": 0.05,
            "refresh_runner": lambda: None,  # avoid real DB path
        },
        daemon=True,
    )
    thread.start()

    # Wait until observer.start() fires — all schedule() calls precede it.
    _wait_for(
        lambda: bool(observer_holder) and observer_holder[0].started,
        timeout=2.0,
    )
    obs = observer_holder[0]

    # Startup must have scheduled exactly two paths:
    # 1. vault root (non-recursive)
    # 2. notes/ (recursive)
    assert (str(tmp_path), False) in obs.schedule_calls, (
        f"Root vault should be scheduled non-recursively; got {obs.schedule_calls}"
    )
    assert (str(notes), True) in obs.schedule_calls, (
        f"notes/ should be scheduled recursively; got {obs.schedule_calls}"
    )
    assert len(obs.schedule_calls) == 2, (
        f"Expected exactly 2 schedule() calls at startup; got {obs.schedule_calls}"
    )

    # Create a new top-level dir post-startup and add a file under it.
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "plan.md").write_text("# plan")

    # Give any hypothetical runtime scheduling time to fire, then assert none did.
    time.sleep(0.2)

    # Per Task 3 Option 2 in docs/plans/2026-05-09-edit-to-ui-latency-closeout.md:
    # "investing in runtime DirCreated handling here would likely be wasted."
    # If this assertion fails, runtime scheduling has been added — update the
    # Known-limitation section in docs/plans/2026-05-09-edit-to-ui-latency.md
    # and the Task 3 Option 2 decision in the closeout plan.
    assert len(obs.schedule_calls) == 2, (
        "Expected no new schedule() calls for post-startup dir 'projects/'. "
        "If this test fails, runtime DirCreated handling has been added — "
        "update the Known-limitation note in "
        "docs/plans/2026-05-09-edit-to-ui-latency.md."
    )

    # Cleanup: stop the watcher.
    if obs._handler is not None:
        obs._handler._state.stop_event.set()
    thread.join(timeout=2.0)
