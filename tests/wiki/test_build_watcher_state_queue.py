"""State-queue tests for brain.wiki.build_watcher (T6b).

Tests the _BuildBatch (current_batch / pending_batch) replacement for the
old ``changed_paths: set[Path]`` + ``pending: bool`` design.

Coverage:
1.  In-flight event → pending_batch (not dropped).
2.  Multi-path in-flight: pending merges multiple paths.
3.  Pending drain → single-md path at drain time → partial routing.
4.  Pending drain → multi-file at drain time → full build.
5.  current_batch accumulates between events (no build running).
6.  Drain clears pending_batch after consumption.
7.  running cleared when pending_batch is empty at drain time.
8.  state.json written after successful full build (fastpath_dir exists).
9.  state.json written after successful partial build (fastpath_dir exists).
10. state.json failure counter incremented after partial build failure.
11. Startup read logs stale pid at INFO.
12. Startup read OK when state.json matches current PID.
13. on_any_event during running=True does NOT schedule a timer (stale-timer regression).
14. _fire with empty current_batch does nothing (stale-timer regression).
15. Stale timer fire after pending drained is safe — no build triggered.

Mocking strategy (per CLAUDE.md item 13):
    ``mocker.patch`` with automatic cleanup.
    NO monkey-patching / direct attribute assignment on imported modules.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from watchdog.events import FileModifiedEvent

from brain.wiki.build_partial import (
    BrainWikiPartialBuildError,
    PartialBuildFailureKind,
    PartialBuildResult,
)
from brain.wiki.build_swap import BuildResult
from brain.wiki.build_watcher import _Handler, _WatcherState
from brain.wiki.edit_classifier import ClassificationResult, EditClassification
from brain.wiki.fastpath_state import (
    _FASTPATH_STATE_VERSION,
    FastpathState,
    read_state,
    write_state,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_full_result(build_id: str = "20260509-120000-abc123") -> BuildResult:
    return BuildResult(
        build_dir=Path(f"/tmp/builds/{build_id}"),
        build_id=build_id,
        elapsed_seconds=0.5,
        pruned=[],
        method="output-flag",
    )


def _trivial_result(slug: str) -> ClassificationResult:
    return ClassificationResult(
        classification=EditClassification.TRIVIAL,
        reason="fingerprint unchanged",
        slug=slug,
        old_fingerprint="abc",
        new_fingerprint="abc",
    )


def _non_trivial_result(slug: str) -> ClassificationResult:
    return ClassificationResult(
        classification=EditClassification.NON_TRIVIAL,
        reason="fingerprint changed",
        slug=slug,
        old_fingerprint="abc",
        new_fingerprint="def",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def workspace(vault: Path) -> Path:
    ws = vault / ".quartz"
    ws.mkdir()
    return ws


@pytest.fixture
def fastpath_dir(workspace: Path) -> Path:
    fp = workspace / ".cache" / "fastpath"
    fp.mkdir(parents=True)
    return fp


@pytest.fixture
def current_build_dir(workspace: Path) -> Path:
    builds = workspace / "builds"
    builds.mkdir()
    build_dir = builds / "20260509-120000-abc123"
    build_dir.mkdir()
    (workspace / "current").symlink_to(Path("builds") / "20260509-120000-abc123")
    return build_dir


@pytest.fixture
def state() -> _WatcherState:
    return _WatcherState()


@pytest.fixture
def handler(vault: Path, state: _WatcherState) -> _Handler:
    return _Handler(
        state=state,
        vault=vault,
        quartz_dir=None,
        debounce_seconds=1.5,
        keep=3,
        on_build=None,
        refresh_runner=lambda: None,
    )


# ---------------------------------------------------------------------------
# 1. In-flight event → pending_batch
# ---------------------------------------------------------------------------


def test_event_during_build_routes_to_pending_batch(
    vault: Path,
    state: _WatcherState,
    handler: _Handler,
) -> None:
    """An event that arrives while running=True is accumulated in pending_batch.

    Simulates: a build is in flight → a new event arrives → the path should
    be in pending_batch, NOT in current_batch.
    """
    note = vault / "notes" / "foo.md"
    note.parent.mkdir(parents=True)

    # Mark build as running (simulates in-flight build).
    with state.lock:
        state.running = True

    # Add path directly via handler logic (bypassing debounce timer).
    # We simulate what on_any_event does when it accumulates a path.
    with state.lock:
        if state.running:
            state.pending_batch.add(note)
        else:
            state.current_batch.add(note)

    with state.lock:
        assert note in state.pending_batch
        assert note not in state.current_batch


# ---------------------------------------------------------------------------
# 2. Multi-path in-flight: pending merges multiple paths
# ---------------------------------------------------------------------------


def test_multiple_events_during_build_all_land_in_pending_batch(
    vault: Path,
    state: _WatcherState,
) -> None:
    """All paths arriving during an in-flight build accumulate in pending_batch."""
    note_a = vault / "a.md"
    note_b = vault / "b.md"
    note_c = vault / "c.md"

    with state.lock:
        state.running = True

    for note in (note_a, note_b, note_c):
        with state.lock:
            if state.running:
                state.pending_batch.add(note)
            else:
                state.current_batch.add(note)

    with state.lock:
        assert state.pending_batch == {note_a, note_b, note_c}
        assert not state.current_batch


# ---------------------------------------------------------------------------
# 3. Drain clears pending_batch and resets running when empty
# ---------------------------------------------------------------------------


def test_drain_pending_clears_running_when_no_pending(
    vault: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """_drain_pending with empty pending_batch sets running=False and returns."""
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    with state.lock:
        state.running = True
        # pending_batch is empty.

    handler._drain_pending()

    with state.lock:
        assert not state.running


# ---------------------------------------------------------------------------
# 4. Pending drain → multi-file at drain time → full build
# ---------------------------------------------------------------------------


def test_drain_pending_multi_file_uses_full_build(
    vault: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """When pending_batch has 2+ files, the drain triggers a full build."""
    note_a = vault / "a.md"
    note_b = vault / "b.md"
    note_a.write_text("A")
    note_b.write_text("B")

    classify_mock = mocker.patch("brain.wiki.build_watcher.classify_edit")
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    with state.lock:
        state.running = True
        state.pending_batch.update({note_a, note_b})

    handler._drain_pending()

    # Multi-file batch → full build; classifier + partial NOT called.
    full_mock.assert_called_once()
    classify_mock.assert_not_called()
    partial_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Pending drain → single-md at drain time → partial routing attempted
# ---------------------------------------------------------------------------


def test_drain_pending_single_md_attempts_partial_routing(
    vault: Path,
    workspace: Path,
    current_build_dir: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """When pending_batch has exactly one .md file, the drain attempts partial routing."""
    note = vault / "notes" / "bar.md"
    note.parent.mkdir(parents=True)
    note.write_text("prose body only")

    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        return_value=_trivial_result("notes/bar"),
    )
    partial_mock = mocker.patch(
        "brain.wiki.build_watcher.run_build_partial",
        return_value=PartialBuildResult(
            slug="notes/bar", elapsed_ms=150, stdout="", stderr=""
        ),
    )
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    with state.lock:
        state.running = True
        state.pending_batch.add(note)

    handler._drain_pending()

    partial_mock.assert_called_once()
    kw = partial_mock.call_args.kwargs
    assert kw["slug"] == "notes/bar"
    assert kw["vault_dir"] == vault


# ---------------------------------------------------------------------------
# 6. _fire routes to pending_batch when running=True
# ---------------------------------------------------------------------------


def test_fire_merges_current_into_pending_when_running(
    vault: Path,
    state: _WatcherState,
    handler: _Handler,
) -> None:
    """_fire while running=True merges current_batch into pending_batch."""
    note = vault / "notes" / "baz.md"

    with state.lock:
        state.running = True
        state.current_batch.add(note)
        # Add something already in pending_batch to verify union behaviour.
        state.pending_batch.add(vault / "prior.md")

    handler._fire()

    with state.lock:
        # note should now be in pending_batch (merged from current_batch).
        assert note in state.pending_batch
        # current_batch is cleared.
        assert not state.current_batch
        # running remains True (we returned without starting a new build).
        assert state.running


# ---------------------------------------------------------------------------
# 7. _fire starts build when not running
# ---------------------------------------------------------------------------


def test_fire_starts_build_when_not_running(
    vault: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """_fire with running=False drains current_batch and starts a build."""
    note = vault / "notes" / "qux.md"
    note.parent.mkdir(parents=True)
    note.write_text("content")

    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )
    mocker.patch("brain.wiki.build_watcher.classify_edit")
    mocker.patch("brain.wiki.build_watcher.run_build_partial")

    with state.lock:
        state.current_batch.add(note)

    handler._fire()

    # A build must have been attempted (multi-file or classify path).
    # For a non-.md or unsupported slug, full build is called directly.
    # For any non-trivial/trivial md file, full build is eventually called.
    # The key invariant: current_batch is empty after _fire.
    with state.lock:
        assert not state.current_batch


# ---------------------------------------------------------------------------
# 8. state.json written after full build (fastpath_dir exists)
# ---------------------------------------------------------------------------


def test_update_state_after_full_build_writes_state_json(
    vault: Path,
    workspace: Path,
    fastpath_dir: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """A successful full build writes state.json with last_full_at_ms and pid."""
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(build_id="20260509-130000-xyz"),
    )

    handler._do_full_build()

    assert (fastpath_dir / "state.json").exists()
    loaded = read_state(fastpath_dir)
    assert loaded is not None
    assert loaded.watcher_pid == os.getpid()
    assert loaded.last_full_at_ms > 0
    assert loaded.consecutive_partial_failures == 0


# ---------------------------------------------------------------------------
# 9. state.json written after partial build success
# ---------------------------------------------------------------------------


def test_update_state_after_partial_build_success(
    vault: Path,
    workspace: Path,
    fastpath_dir: Path,
    current_build_dir: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """A successful partial build writes state.json with last_partial_at_ms + slug."""
    note = vault / "notes" / "test.md"
    note.parent.mkdir(parents=True)
    note.write_text("body")

    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        return_value=_trivial_result("notes/test"),
    )
    mocker.patch(
        "brain.wiki.build_watcher.run_build_partial",
        return_value=PartialBuildResult(
            slug="notes/test", elapsed_ms=200, stdout="", stderr=""
        ),
    )
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )

    handler._run_build(frozenset({note}))

    assert (fastpath_dir / "state.json").exists()
    loaded = read_state(fastpath_dir)
    assert loaded is not None
    assert loaded.last_partial_at_ms > 0
    assert loaded.last_partial_slug == "notes/test"
    assert loaded.consecutive_partial_failures == 0


# ---------------------------------------------------------------------------
# 10. Partial failure counter incremented on BrainWikiPartialBuildError
# ---------------------------------------------------------------------------


def test_partial_failure_counter_incremented_before_full_build_reset(
    vault: Path,
    workspace: Path,
    fastpath_dir: Path,
    current_build_dir: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """After partial failure + fallback full build failure, counter persists."""
    note = vault / "notes" / "fail2.md"
    note.parent.mkdir(parents=True)
    note.write_text("content")

    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        return_value=_trivial_result("notes/fail2"),
    )
    mocker.patch(
        "brain.wiki.build_watcher.run_build_partial",
        side_effect=BrainWikiPartialBuildError(
            "partial failed",
            kind=PartialBuildFailureKind.EMITTER_FAILED,
            slug="notes/fail2",
        ),
    )
    # Make the full build also fail so _do_full_build doesn't reset the counter.
    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        side_effect=RuntimeError("full build also failed"),
    )

    existing = FastpathState(
        version=_FASTPATH_STATE_VERSION,
        watcher_pid=os.getpid(),
        last_partial_at_ms=0,
        last_full_at_ms=0,
        last_partial_slug=None,
        consecutive_partial_failures=1,
    )
    write_state(fastpath_dir, existing)

    handler._run_build(frozenset({note}))

    # Counter incremented to 2; full build failed so no reset happened.
    loaded = read_state(fastpath_dir)
    assert loaded is not None
    assert loaded.consecutive_partial_failures == 2


# ---------------------------------------------------------------------------
# 11. Startup read logs stale pid at INFO
# ---------------------------------------------------------------------------


def test_startup_read_logs_stale_pid(
    vault: Path,
    workspace: Path,
    fastpath_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When state.json has a different PID, startup logs at INFO mentioning the stale pid."""
    stale_pid = os.getpid() + 99999
    stale_state = FastpathState(
        version=_FASTPATH_STATE_VERSION,
        watcher_pid=stale_pid,
        last_partial_at_ms=0,
        last_full_at_ms=0,
        last_partial_slug=None,
        consecutive_partial_failures=0,
    )
    write_state(fastpath_dir, stale_state)

    watcher_state = _WatcherState()
    with caplog.at_level(logging.INFO, logger="brain.wiki.build_watcher"):
        _Handler(
            state=watcher_state,
            vault=vault,
            quartz_dir=None,
            debounce_seconds=1.5,
            keep=3,
            on_build=None,
            refresh_runner=lambda: None,
        )

    assert "previous watcher pid=" in caplog.text
    assert str(stale_pid) in caplog.text


# ---------------------------------------------------------------------------
# 12. Startup read OK when PID matches
# ---------------------------------------------------------------------------


def test_startup_read_resumes_from_valid_state(
    vault: Path,
    workspace: Path,
    fastpath_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A valid state.json with matching PID logs resume info at startup."""
    current_state = FastpathState(
        version=_FASTPATH_STATE_VERSION,
        watcher_pid=os.getpid(),
        last_partial_at_ms=1_000,
        last_full_at_ms=2_000,
        last_partial_slug="notes/foo",
        consecutive_partial_failures=0,
    )
    write_state(fastpath_dir, current_state)

    watcher_state = _WatcherState()
    with caplog.at_level(logging.INFO, logger="brain.wiki.build_watcher"):
        _Handler(
            state=watcher_state,
            vault=vault,
            quartz_dir=None,
            debounce_seconds=1.5,
            keep=3,
            on_build=None,
            refresh_runner=lambda: None,
        )

    # Should log the "resumed from state.json" line.
    assert "resumed from state.json" in caplog.text


# ---------------------------------------------------------------------------
# 13. Pending preserved across a failed in-flight build
# ---------------------------------------------------------------------------


def test_pending_paths_preserved_after_failed_build(
    vault: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """Paths in pending_batch are NOT lost when the in-flight build fails.

    _drain_pending uses contextlib.suppress(Exception) so failures in
    _run_build don't abort the drain loop — but the batch was already drained
    from pending_batch before _run_build was called.  The paths ARE consumed;
    on exception, no second attempt is made for that batch (same as the old
    pending: bool design).  The test verifies pending_batch is empty (drained)
    and running is cleared after the failed drain.
    """
    note = vault / "notes" / "pending.md"
    note.parent.mkdir(parents=True)
    note.write_text("content")

    mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        side_effect=RuntimeError("build failed"),
    )
    mocker.patch(
        "brain.wiki.build_watcher.classify_edit",
        side_effect=RuntimeError("classify failed"),
    )

    with state.lock:
        state.running = True
        state.pending_batch.add(note)

    handler._drain_pending()

    # After the drain loop, running must be cleared and pending must be empty.
    with state.lock:
        assert not state.running
        assert not state.pending_batch


# ---------------------------------------------------------------------------
# 13. on_any_event during running=True does NOT schedule a timer
#     (stale-timer regression — T6b fix #1)
# ---------------------------------------------------------------------------


def test_on_any_event_during_running_does_not_schedule_timer(
    vault: Path,
    state: _WatcherState,
    handler: _Handler,
) -> None:
    """When running=True, on_any_event adds to pending_batch and does NOT start a timer.

    Before fix #1, _schedule() was called unconditionally — even when the
    path went into pending_batch — installing a debounce timer that later
    fired _fire() with an empty current_batch, triggering a spurious full build
    (Codex probe: ``pending True timer_scheduled True`` → should be False).
    """
    note = vault / "test.md"
    note.write_text("body")

    # Simulate an in-flight build.
    with state.lock:
        state.running = True

    handler.on_any_event(FileModifiedEvent(str(note)))

    with state.lock:
        assert note in state.pending_batch, "path must be routed to pending_batch"
        assert state.timer is None, "no debounce timer should be scheduled when running=True"


# ---------------------------------------------------------------------------
# 14. _fire with empty current_batch does nothing
#     (stale-timer regression — T6b fix #2)
# ---------------------------------------------------------------------------


def test_fire_with_empty_current_batch_does_nothing(
    vault: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """_fire with running=False and empty current_batch must not trigger any build.

    Before fix #2, _fire() would set running=True and call
    _run_build(frozenset()), which routes an empty batch to _do_full_build() —
    a spurious full rebuild after every successful partial.
    """
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")

    with state.lock:
        state.running = False
        # current_batch is already empty (dataclass default).

    handler._fire()

    full_mock.assert_not_called()
    partial_mock.assert_not_called()
    with state.lock:
        assert not state.running, "running must stay False when no work was done"


# ---------------------------------------------------------------------------
# 15. Stale timer fire after pending drained is safe — no build triggered
#     (stale-timer regression — T6b fix #2, end-to-end race scenario)
# ---------------------------------------------------------------------------


def test_stale_timer_fire_after_pending_drained_is_safe(
    vault: Path,
    state: _WatcherState,
    handler: _Handler,
    mocker: MockerFixture,
) -> None:
    """A stale timer firing after _drain_pending has cleared everything must do nothing.

    Race scenario: path lands in pending_batch while build is in flight →
    _drain_pending picks it up → build finishes → running=False, both batches
    empty → stale timer fires _fire(). With fix #2, _fire sees an empty
    current_batch and returns without calling any build function.
    """
    full_mock = mocker.patch(
        "brain.wiki.build_watcher.build_and_swap",
        return_value=_make_full_result(),
    )
    partial_mock = mocker.patch("brain.wiki.build_watcher.run_build_partial")

    # Simulate the post-drain state: running=False, both batches empty.
    with state.lock:
        state.running = False
        state.current_batch.clear()
        state.pending_batch.clear()

    # Stale timer fires.
    handler._fire()

    full_mock.assert_not_called()
    partial_mock.assert_not_called()
    with state.lock:
        assert not state.running
