"""Tests for brain.bin.monitor — cross-platform shims and core logic."""

import os
from pathlib import Path

import pytest

from brain.bin.monitor import (
    _pid_alive,
    _recent_vault_files,
    main,
)

# ---------------------------------------------------------------------------
# _pid_alive
# ---------------------------------------------------------------------------


def test_pid_alive_true_for_self(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    pid_file.write_text(str(os.getpid()))
    assert _pid_alive(pid_file) is True


def test_pid_alive_false_for_dead_pid(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    pid_file.write_text("999999999")  # very unlikely to exist
    assert _pid_alive(pid_file) is False


def test_pid_alive_false_for_missing_file(tmp_path: Path) -> None:
    assert _pid_alive(tmp_path / "nope") is False


def test_pid_alive_handles_garbage_content(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    pid_file.write_text("not-a-number\n")
    assert _pid_alive(pid_file) is False


# ---------------------------------------------------------------------------
# _current_build_id
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _recent_builds
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _recent_vault_files — cross-platform mtime replacement for BSD stat -f
# ---------------------------------------------------------------------------


def test_recent_vault_files_excludes_dotdirs(tmp_path: Path) -> None:
    (tmp_path / ".quartz").mkdir()
    (tmp_path / ".quartz" / "noise.md").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "real.md").write_text("real")
    (tmp_path / ".DS_Store").write_text("apple junk")
    results = _recent_vault_files(tmp_path)
    paths = [path for _, path in results]
    assert any(p.endswith("real.md") for p in paths)
    assert not any("/.quartz/" in p for p in paths)
    assert not any("/.git/" in p for p in paths)
    assert not any(".DS_Store" in p for p in paths)


def test_recent_vault_files_returns_mtime_float(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("hello")
    results = _recent_vault_files(tmp_path)
    assert len(results) == 1
    mtime, path = results[0]
    assert isinstance(mtime, float)
    assert mtime > 0
    assert path.endswith("note.md")


def test_recent_vault_files_sorted_newest_first(tmp_path: Path) -> None:
    import time

    (tmp_path / "old.md").write_text("old")
    time.sleep(0.05)
    (tmp_path / "new.md").write_text("new")
    results = _recent_vault_files(tmp_path)
    assert len(results) == 2
    # Newest file should be first
    assert results[0][0] > results[1][0]
    assert results[0][1].endswith("new.md")


def test_recent_vault_files_respects_limit(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"file{i:02d}.md").write_text(f"content {i}")
    results = _recent_vault_files(tmp_path, n=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# _filter_build_log_line
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# main — help + snapshot smoke
# ---------------------------------------------------------------------------


def test_main_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--help"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "brain-monitor" in captured


def test_main_snapshot_returns_zero_with_no_daemons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot mode must not crash when daemons are stopped and vault is empty."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    # Remove any stale /tmp pid files from view by patching the constants.
    monkeypatch.setattr("brain.bin.monitor.WATCH_PID", tmp_path / "watch.pid")
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))

    rc = main([])
    assert rc == 0


def test_main_status_alias_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'status' is a recognised alias for snapshot."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr("brain.bin.monitor.WATCH_PID", tmp_path / "watch.pid")
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))

    rc = main(["status"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Bash-compat CLI aliases — regression tests for the positional aliases that
# the bash case statement handled but the initial argparse port missed.
# ---------------------------------------------------------------------------


def test_main_help_positional_alias(capsys: pytest.CaptureFixture[str]) -> None:
    """'brain-monitor help' (positional) must print usage and exit 0."""
    rc = main(["help"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "brain-monitor" in captured


def test_main_tail_positional_alias_errors_without_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'brain-monitor tail' (positional) must be accepted and route to tail mode.

    When no log exists, _tail() returns 1 — that's the correct fast-exit path.
    """

    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    rc = main(["tail"])
    assert rc == 1  # sync-watcher log absent → "no log to tail"


# ---------------------------------------------------------------------------
# Unknown-argument rejection — regression for parse_known_args silent-swallow.
# Any unrecognised flag / word must cause exit 2, not be silently ignored.
# (The narrow `probe <N>` carve-out went with `probe` at R3.)
# ---------------------------------------------------------------------------


def test_main_rejects_unknown_flag() -> None:
    """Unknown flags must still be rejected (exit 2), not silently swallowed."""
    rc = main(["--bogus-flag"])
    assert rc == 2


def test_main_tail_flag_rejects_unknown_extra() -> None:
    """--tail with an unknown flag must be rejected before dispatch (exit 2).

    Regression: validation was placed AFTER the early `if args.tail` return,
    so `brain-monitor --tail --bogus` was silently accepted.
    """
    rc = main(["--tail", "--bogus"])
    assert rc == 2


def test_main_f_flag_rejects_unknown_extra() -> None:
    """-f with an unknown flag must also be rejected (exit 2)."""
    rc = main(["-f", "--bogus"])
    assert rc == 2


def test_main_rejects_unknown_flag_after_snapshot() -> None:
    """Unknown flags after a valid positional command are still rejected."""
    rc = main(["snapshot", "--no-such-option"])
    assert rc == 2


def test_main_rejects_extra_positional_after_non_probe_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extra positional args are never valid (``probe <N>`` was the only one, R3)."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr("brain.bin.monitor.WATCH_PID", tmp_path / "watch.pid")
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))

    rc = main(["snapshot", "extra"])
    assert rc == 2


# ---------------------------------------------------------------------------
# R3 (wiki-to-ui phase 9, ruling R9-6): the Quartz build half is gone.
# ---------------------------------------------------------------------------


def test_probe_command_is_gone(capsys: pytest.CaptureFixture[str]) -> None:
    """``probe`` only ever timed the Quartz build watcher's rebuild; R3 deleted it.

    It is rejected by ``choices=`` (exit 2), and the help no longer offers it.
    """
    assert main(["probe"]) == 2
    assert "probe" not in capsys.readouterr().err.split("choose from", 1)[-1]
    assert main(["--help"]) == 0
    assert "probe" not in capsys.readouterr().out


def test_snapshot_reports_no_build_watcher_even_over_a_leftover_quartz_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An upgraded vault keeps its ``.quartz`` until the user deletes it (plan §6).

    The snapshot must not report on it: no build-watcher row, no current build,
    no recent builds, no wiki URL, no build log. The sync-watcher half stays.
    """
    builds = tmp_path / ".quartz" / "builds" / "20260501-000000-deadbe"
    builds.mkdir(parents=True)
    (builds / ".build-id").write_text("20260501-000000-deadbe\n", encoding="utf-8")
    (tmp_path / ".quartz" / "current").symlink_to(Path("builds") / builds.name)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    monkeypatch.setattr("brain.bin.monitor.WATCH_PID", tmp_path / "watch.pid")
    (tmp_path / "watch.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr("brain.bin.monitor._pgrep_count", lambda pattern: 1)

    assert main([]) == 0

    out = capsys.readouterr().out
    assert "sync watcher" in out and "running" in out, out
    for gone in ("build watcher", "current build", "recent builds", "wiki url", "build-log"):
        assert gone not in out, f"{gone!r} survived R3: {out}"
    assert "process count vs pid file" not in out, out
