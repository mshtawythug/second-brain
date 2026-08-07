"""Tests for brain.log_rotation — size caps for launchd-owned daemon logs.

These tests have zero DB dependency. Run with:
    .venv/bin/pytest --no-cov --noconftest -q tests/test_log_rotation.py -v
"""

import threading
from pathlib import Path

import pytest

from brain.log_rotation import (
    DEFAULT_MAX_BYTES,
    MAX_BYTES_ENV,
    ROTATED_SUFFIX,
    default_log_dir,
    main,
    resolve_max_bytes,
    rotate_daemon_logs,
    rotate_if_oversized,
    start_background_rotator,
)

# A stand-in for the ~2 KB Rich ConfigError traceback that launchd's
# StandardErrorPath captured 250k times. Synthetic — no real paths or PII.
_TRACEBACK_BLOB = b"ConfigError: DATABASE_URL is not set (see .env.example)\n" * 40


# ---------------------------------------------------------------------------
# rotate_if_oversized — core copy-truncate behaviour
# ---------------------------------------------------------------------------


def test_rotate_if_oversized_truncates_and_keeps_previous_generation(
    tmp_path: Path,
) -> None:
    """An oversized log is copied to <name>.1 and truncated back to zero."""
    log = tmp_path / "com.brain.watcher.err.log"
    log.write_bytes(b"x" * 5000)

    assert rotate_if_oversized(log, max_bytes=1000) is True

    assert log.stat().st_size == 0
    rotated = tmp_path / ("com.brain.watcher.err.log" + ROTATED_SUFFIX)
    assert rotated.stat().st_size == 5000


def test_rotate_if_oversized_leaves_under_cap_file_alone(tmp_path: Path) -> None:
    """A log at or below the cap is untouched and no .1 generation appears."""
    log = tmp_path / "small.err.log"
    log.write_bytes(b"x" * 900)

    assert rotate_if_oversized(log, max_bytes=1000) is False

    assert log.stat().st_size == 900
    assert not (tmp_path / ("small.err.log" + ROTATED_SUFFIX)).exists()


def test_rotate_preserves_inode_so_launchd_keeps_writing(tmp_path: Path) -> None:
    """Rotation must NOT swap the inode — launchd holds fd 1/2 open.

    This is the crux of the whole design. launchd opens StandardErrorPath once
    at spawn and hands the daemon that descriptor; the descriptor follows the
    *inode*, not the path. A rename/unlink rotation would leave the daemon
    writing into the rotated-away file forever while the live log stayed empty.

    The still-open handle below stands in for launchd's fd: after rotation it
    must still land in the original path.
    """
    log = tmp_path / "daemon.err.log"
    log.write_bytes(b"x" * 5000)
    inode_before = log.stat().st_ino

    with open(log, "a", encoding="utf-8") as launchd_fd:
        assert rotate_if_oversized(log, max_bytes=1000) is True
        launchd_fd.write("written after rotation\n")
        launchd_fd.flush()

    assert log.stat().st_ino == inode_before
    assert "written after rotation" in log.read_text(encoding="utf-8")


def test_rotate_if_oversized_ignores_missing_file(tmp_path: Path) -> None:
    """A log the daemon has not created yet is not an error."""
    assert rotate_if_oversized(tmp_path / "absent.err.log", max_bytes=10) is False


def test_rotate_if_oversized_disabled_by_zero_cap(tmp_path: Path) -> None:
    """max_bytes=0 is the documented 'disable rotation' escape hatch."""
    log = tmp_path / "daemon.err.log"
    log.write_bytes(b"x" * 5000)

    assert rotate_if_oversized(log, max_bytes=0) is False
    assert log.stat().st_size == 5000


# ---------------------------------------------------------------------------
# The actual incident shape — a crash loop must stay bounded
# ---------------------------------------------------------------------------


def test_crash_loop_stays_bounded(tmp_path: Path) -> None:
    """500 crash-loop restarts cannot grow the log without bound.

    Reproduces the reported failure: com.brain.watcher.err.log reached
    506,756,066 bytes because a dead config chain made the daemon exit(1) on
    every launchd restart, each time dumping a Rich traceback to fd 2, with no
    rotation anywhere. Here each iteration models one restart — the shim runs
    rotation, then the doomed process appends its traceback.

    Without rotate_daemon_logs this loop writes ~1.1 MB against an 8 KB cap,
    so the assertion below genuinely fails when the fix is removed.
    """
    log = tmp_path / "com.brain.watcher.err.log"
    log.write_bytes(b"")
    cap = 8 * 1024

    for _ in range(500):
        rotate_daemon_logs(tmp_path, max_bytes=cap)  # what the shim does pre-exec
        with open(log, "ab") as handle:
            handle.write(_TRACEBACK_BLOB)

    unbounded_size = 500 * len(_TRACEBACK_BLOB)
    assert unbounded_size > cap * 10, "fixture too small to prove the cap matters"

    # Live file can overshoot by at most the final un-rotated write.
    assert log.stat().st_size <= cap + len(_TRACEBACK_BLOB)
    # Total on disk is bounded by live + one retained generation.
    rotated = tmp_path / ("com.brain.watcher.err.log" + ROTATED_SUFFIX)
    total = log.stat().st_size + rotated.stat().st_size
    assert total <= 2 * (cap + len(_TRACEBACK_BLOB))


# ---------------------------------------------------------------------------
# rotate_daemon_logs — directory sweep
# ---------------------------------------------------------------------------


def test_rotate_daemon_logs_sweeps_every_oversized_log(tmp_path: Path) -> None:
    """All oversized *.log files rotate; under-cap ones are left alone."""
    big_err = tmp_path / "com.brain.watcher.err.log"
    big_out = tmp_path / "com.brain.build.err.log"
    small = tmp_path / "com.brain.brief.out.log"
    big_err.write_bytes(b"x" * 5000)
    big_out.write_bytes(b"x" * 5000)
    small.write_bytes(b"x" * 10)

    rotated = rotate_daemon_logs(tmp_path, max_bytes=1000)

    assert set(rotated) == {big_err, big_out}
    assert small.stat().st_size == 10


def test_rotate_daemon_logs_never_rotates_the_retained_generation(
    tmp_path: Path,
) -> None:
    """The *.log glob must not pick up *.log.1 and rotate it into *.log.1.1."""
    previous = tmp_path / ("com.brain.watcher.err.log" + ROTATED_SUFFIX)
    previous.write_bytes(b"x" * 5000)

    assert rotate_daemon_logs(tmp_path, max_bytes=1000) == []

    assert previous.stat().st_size == 5000
    assert not (tmp_path / "com.brain.watcher.err.log.1.1").exists()


def test_rotate_daemon_logs_ignores_non_daemon_logs(tmp_path: Path) -> None:
    """Unrelated *.log files sharing the directory must never be truncated.

    Regression for a real blast radius: $BRAIN_HOME resolves to the dev checkout
    when BRAIN_HOME is unset, and that checkout's logs/ dir holds operator
    artefacts (concept-backfill runs, ad-hoc reruns) alongside daemon streams.
    A bare *.log glob would truncate those as a side effect of a daemon start.
    """
    daemon_log = tmp_path / "com.brain.watcher.err.log"
    backfill_log = tmp_path / "concept-backfill-20260522-133749.log"
    rerun_log = tmp_path / "v3-rerun-detached-20260524-104821.log"
    for path in (daemon_log, backfill_log, rerun_log):
        path.write_bytes(b"x" * 5000)

    rotated = rotate_daemon_logs(tmp_path, max_bytes=1000)

    assert rotated == [daemon_log]
    assert backfill_log.stat().st_size == 5000
    assert rerun_log.stat().st_size == 5000
    assert not (tmp_path / (backfill_log.name + ROTATED_SUFFIX)).exists()


def test_rotate_daemon_logs_tolerates_missing_directory(tmp_path: Path) -> None:
    """A daemon that has never run yet has no logs dir — not an error."""
    assert rotate_daemon_logs(tmp_path / "nonexistent") == []


# ---------------------------------------------------------------------------
# Cap resolution
# ---------------------------------------------------------------------------


def test_resolve_max_bytes_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MAX_BYTES_ENV, raising=False)
    assert resolve_max_bytes() == DEFAULT_MAX_BYTES


def test_resolve_max_bytes_honours_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MAX_BYTES_ENV, "4096")
    assert resolve_max_bytes() == 4096


@pytest.mark.parametrize("bad", ["not-a-number", "-1"])
def test_resolve_max_bytes_falls_back_on_bad_values(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A malformed cap must not disable rotation silently."""
    monkeypatch.setenv(MAX_BYTES_ENV, bad)
    assert resolve_max_bytes() == DEFAULT_MAX_BYTES


def test_resolve_max_bytes_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_BYTES_ENV, "0")
    assert resolve_max_bytes() == 0


# ---------------------------------------------------------------------------
# default_log_dir + CLI entry point
# ---------------------------------------------------------------------------


def test_default_log_dir_follows_brain_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sweep targets $BRAIN_HOME/logs — the dir the plists write into."""
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    assert default_log_dir() == tmp_path / "logs"


def test_main_rotates_and_always_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shim invokes this pre-exec; it must cap logs and never block start."""
    monkeypatch.delenv(MAX_BYTES_ENV, raising=False)
    log = tmp_path / "com.brain.watcher.err.log"
    log.write_bytes(b"x" * 5000)

    assert main(["--log-dir", str(tmp_path), "--max-bytes", "1000"]) == 0
    assert log.stat().st_size == 0


def test_main_exits_zero_on_unusable_log_dir(tmp_path: Path) -> None:
    """Rotation failure must never be the reason a daemon fails to start."""
    assert main(["--log-dir", str(tmp_path / "nope")]) == 0


# ---------------------------------------------------------------------------
# Background rotator (covers a daemon that stays UP and loops on errors)
# ---------------------------------------------------------------------------


def test_background_rotator_caps_a_live_daemons_log(tmp_path: Path) -> None:
    """A long-lived daemon's log gets capped without any process restart."""
    log = tmp_path / "com.brain.build.err.log"
    log.write_bytes(b"x" * 5000)
    stop = threading.Event()

    thread = start_background_rotator(
        tmp_path, max_bytes=1000, interval_seconds=0.01, stop_event=stop
    )
    try:
        deadline = threading.Event()
        # Poll rather than sleep on a fixed duration — keeps the test fast and
        # non-flaky regardless of scheduler jitter.
        for _ in range(200):
            if log.stat().st_size == 0:
                break
            deadline.wait(0.01)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert log.stat().st_size == 0
    assert not thread.is_alive()


def test_background_rotator_thread_is_daemonic(tmp_path: Path) -> None:
    """It must never block interpreter shutdown."""
    stop = threading.Event()
    thread = start_background_rotator(
        tmp_path, interval_seconds=60.0, stop_event=stop
    )
    try:
        assert thread.daemon is True
    finally:
        stop.set()
        thread.join(timeout=5)
