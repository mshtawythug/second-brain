"""Tests for brain.bin.monitor — cross-platform shims and core logic."""

import os
from pathlib import Path

import pytest

from brain.bin.monitor import (
    _current_build_id,
    _filter_build_log_line,
    _pid_alive,
    _recent_builds,
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


def test_current_build_id_returns_none_when_missing(tmp_path: Path) -> None:
    assert _current_build_id(tmp_path) == "(none)"


def test_current_build_id_reads_file(tmp_path: Path) -> None:
    quartz = tmp_path / ".quartz" / "current"
    quartz.mkdir(parents=True)
    (quartz / ".build-id").write_text("build-2026-05-11-1234")
    assert _current_build_id(tmp_path) == "build-2026-05-11-1234"


# ---------------------------------------------------------------------------
# _recent_builds
# ---------------------------------------------------------------------------


def test_recent_builds_sorted_chronological_last_n(tmp_path: Path) -> None:
    builds = tmp_path / ".quartz" / "builds"
    builds.mkdir(parents=True)
    for name in ["build-001", "build-003", "build-002", "build-005", "build-004"]:
        (builds / name).mkdir()
    result = _recent_builds(tmp_path, n=3)
    assert result == ["build-003", "build-004", "build-005"]


def test_recent_builds_empty_when_no_builds_dir(tmp_path: Path) -> None:
    assert _recent_builds(tmp_path) == []


def test_recent_builds_returns_all_when_fewer_than_n(tmp_path: Path) -> None:
    builds = tmp_path / ".quartz" / "builds"
    builds.mkdir(parents=True)
    (builds / "build-001").mkdir()
    (builds / "build-002").mkdir()
    result = _recent_builds(tmp_path, n=5)
    assert result == ["build-001", "build-002"]


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


def test_filter_build_log_line_drops_noise() -> None:
    assert _filter_build_log_line("LaTeX-incompatible input on line 3") is False
    assert _filter_build_log_line("") is False  # blank line
    assert _filter_build_log_line("   ") is False  # whitespace only
    assert _filter_build_log_line("Cleaned output directory /tmp/foo") is False
    assert _filter_build_log_line("No character metrics for font X") is False
    assert _filter_build_log_line("Warning: couldn't find git repository") is False
    assert _filter_build_log_line("Parsing input files using worker pool") is False


def test_filter_build_log_line_keeps_signal() -> None:
    assert _filter_build_log_line("[build] starting build-001") is True
    assert _filter_build_log_line("error: something broke") is True
    assert _filter_build_log_line("[quartz] Done in 3.2s") is True
    assert _filter_build_log_line("brain: writing 47 files") is True


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
    monkeypatch.setenv("BRAIN_WIKI_PORT", "18080")
    # Remove any stale /tmp pid files from view by patching the constants.
    monkeypatch.setattr("brain.bin.monitor.WATCH_PID", tmp_path / "watch.pid")
    monkeypatch.setattr("brain.bin.monitor.BUILD_PID", tmp_path / "build.pid")
    monkeypatch.setattr("brain.bin.monitor.WATCH_LOG", tmp_path / "watch.log")
    monkeypatch.setattr("brain.bin.monitor.BUILD_LOG", tmp_path / "build.log")
    rc = main([])
    assert rc == 0


def test_main_status_alias_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'status' is a recognised alias for snapshot."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr("brain.bin.monitor.WATCH_PID", tmp_path / "watch.pid")
    monkeypatch.setattr("brain.bin.monitor.BUILD_PID", tmp_path / "build.pid")
    monkeypatch.setattr("brain.bin.monitor.WATCH_LOG", tmp_path / "watch.log")
    monkeypatch.setattr("brain.bin.monitor.BUILD_LOG", tmp_path / "build.log")
    rc = main(["status"])
    assert rc == 0
