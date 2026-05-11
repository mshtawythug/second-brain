"""Tests for brain.bin._launcher — shim install, drift protection, and exec_shim."""
from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from brain.bin._launcher import ensure_shim, exec_shim

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_bytes(name: str) -> bytes:
    """Read the canonical source bytes from package data."""
    from importlib.resources import files as resource_files

    return (resource_files("brain.templates.bin") / f"{name}.sh").read_bytes()


# ---------------------------------------------------------------------------
# Test 1: fresh install
# ---------------------------------------------------------------------------


def test_ensure_shim_fresh_install(tmp_path: Path) -> None:
    """ensure_shim installs the shim at <brain_home>/.shims/<name> on a fresh call."""
    # Setup: empty brain_home
    brain_home = tmp_path / "brain_home"

    # Exercise
    installed = ensure_shim("brain-up", brain_home)

    # Verify: path, content, executable bit
    expected = brain_home / ".shims" / "brain-up"
    assert installed == expected
    assert installed.exists(), "installed shim must exist"
    src_bytes = _source_bytes("brain-up")
    assert _sha256(installed.read_bytes()) == _sha256(src_bytes), "sha256 must match source"
    assert os.access(installed, os.X_OK), "installed shim must be executable"


# ---------------------------------------------------------------------------
# Test 2: up-to-date no-op (mtime preserved)
# ---------------------------------------------------------------------------


def test_ensure_shim_up_to_date_no_op(tmp_path: Path, capsys: Any) -> None:
    """ensure_shim called twice: second call is a no-op (mtime unchanged, no stderr)."""
    brain_home = tmp_path / "brain_home"

    # First call — installs
    ensure_shim("brain-up", brain_home)
    installed = brain_home / ".shims" / "brain-up"
    mtime_before = installed.stat().st_mtime_ns

    # Drain first-call stderr
    capsys.readouterr()

    # Second call — must be a no-op
    ensure_shim("brain-up", brain_home)
    mtime_after = installed.stat().st_mtime_ns

    captured = capsys.readouterr()
    assert mtime_before == mtime_after, "mtime must not change on a no-op"
    assert captured.err == "", "no-op must not write to stderr"


# ---------------------------------------------------------------------------
# Test 3: stale bytes replaced
# ---------------------------------------------------------------------------


def test_ensure_shim_stale_bytes_replaced(tmp_path: Path, capsys: Any) -> None:
    """ensure_shim replaces a stale shim (wrong sha256) and logs to stderr."""
    brain_home = tmp_path / "brain_home"
    bin_dir = brain_home / ".shims"
    bin_dir.mkdir(parents=True)
    stale_path = bin_dir / "brain-up"
    stale_path.write_bytes(b"#!/bin/bash\n# stale content\n")
    stale_path.chmod(0o755)

    # Exercise
    ensure_shim("brain-up", brain_home)

    # Verify: content matches source now
    src_bytes = _source_bytes("brain-up")
    assert _sha256(stale_path.read_bytes()) == _sha256(src_bytes), "stale shim must be replaced"

    captured = capsys.readouterr()
    assert "shim updated: brain-up" in captured.err, "must log shim update to stderr"


# ---------------------------------------------------------------------------
# Test 4: concurrent race safety
# ---------------------------------------------------------------------------


def test_ensure_shim_concurrent_race_safe(tmp_path: Path) -> None:
    """4 threads calling ensure_shim concurrently produce no torn write."""
    brain_home = tmp_path / "brain_home"
    src_bytes = _source_bytes("brain-up")
    src_hash = _sha256(src_bytes)

    def call_ensure() -> Path:
        return ensure_shim("brain-up", brain_home)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(call_ensure) for _ in range(4)]
        results = [f.result() for f in futures]

    installed = brain_home / ".shims" / "brain-up"
    assert installed.exists(), "installed shim must exist after concurrent installs"
    assert _sha256(installed.read_bytes()) == src_hash, "no torn write — sha256 must match source"
    # All returned the same path
    assert all(p == installed for p in results)


# ---------------------------------------------------------------------------
# Test 5: error cleanup — no leftover tmpfiles
# ---------------------------------------------------------------------------


def test_ensure_shim_error_cleanup(tmp_path: Path) -> None:
    """If os.replace raises, no leftover tmpfiles remain in brain_home/bin/."""
    brain_home = tmp_path / "brain_home"

    with (
        patch("brain.bin._launcher.os.replace", side_effect=OSError("injected")),
        pytest.raises(OSError, match="injected"),
    ):
        ensure_shim("brain-up", brain_home)

    bin_dir = brain_home / ".shims"
    if bin_dir.exists():
        leftover = list(bin_dir.iterdir())
        assert leftover == [], f"leftover tmpfiles found: {leftover}"


# ---------------------------------------------------------------------------
# Test 6: exec_shim injects BRAIN_PY into env
# ---------------------------------------------------------------------------


def test_exec_shim_passes_brain_py(tmp_path: Path) -> None:
    """exec_shim passes BRAIN_PY=sys.executable in the execvpe env."""
    brain_home = tmp_path / "brain_home"
    captured: dict[str, Any] = {}

    def fake_execvpe(path: str, args: list[str], env: dict[str, str]) -> None:
        captured["path"] = path
        captured["args"] = args
        captured["env"] = env

    with (
        patch("brain.bin._launcher._brain_home_root", return_value=brain_home),
        patch("brain.bin._launcher.os.execvpe", side_effect=fake_execvpe),
    ):
        exec_shim("brain-up", ["--help"])

    assert "BRAIN_PY" in captured["env"], "BRAIN_PY must be in execvpe env"
    assert captured["env"]["BRAIN_PY"] == sys.executable, "BRAIN_PY must equal sys.executable"


# ---------------------------------------------------------------------------
# Test 7: exec_shim argv shape
# ---------------------------------------------------------------------------


def test_exec_shim_passes_argv(tmp_path: Path) -> None:
    """exec_shim calls execvpe with [shim_path, *extra_args] and path ends in /bin/brain-up."""
    brain_home = tmp_path / "brain_home"
    captured: dict[str, Any] = {}

    def fake_execvpe(path: str, args: list[str], env: dict[str, str]) -> None:
        captured["path"] = path
        captured["args"] = args
        captured["env"] = env

    with (
        patch("brain.bin._launcher._brain_home_root", return_value=brain_home),
        patch("brain.bin._launcher.os.execvpe", side_effect=fake_execvpe),
    ):
        exec_shim("brain-up", ["arg1", "arg2"])

    shim_path = captured["path"]
    assert shim_path.endswith("/.shims/brain-up"), (
        f"shim path must end in /.shims/brain-up, got {shim_path!r}"
    )
    assert not shim_path.endswith(".sh"), "installed path must not have .sh suffix"
    assert captured["args"] == [shim_path, "arg1", "arg2"], (
        f"argv mismatch: {captured['args']!r}"
    )


# ---------------------------------------------------------------------------
# Tests 8-10: T1.6 entry-point launchers — brain-down, brain-status, brain-rebuild
# ---------------------------------------------------------------------------


def test_brain_down_main_dispatches_to_shim(monkeypatch: Any) -> None:
    """brain-down's main() calls exec_shim with the correct shim name."""
    from brain.bin import down

    captured: dict[str, Any] = {}

    def fake_exec_shim(name: str, args: Sequence[str]) -> None:
        captured["name"] = name
        captured["args"] = list(args)

    monkeypatch.setattr("brain.bin.down.exec_shim", fake_exec_shim)
    monkeypatch.setattr(sys, "argv", ["brain-down", "--verbose"])
    down.main()

    assert captured["name"] == "brain-down"
    assert captured["args"] == ["--verbose"]


def test_brain_status_main_dispatches_to_shim(monkeypatch: Any) -> None:
    """brain-status's main() calls exec_shim with the correct shim name."""
    from brain.bin import status

    captured: dict[str, Any] = {}

    def fake_exec_shim(name: str, args: Sequence[str]) -> None:
        captured["name"] = name
        captured["args"] = list(args)

    monkeypatch.setattr("brain.bin.status.exec_shim", fake_exec_shim)
    monkeypatch.setattr(sys, "argv", ["brain-status", "--json"])
    status.main()

    assert captured["name"] == "brain-status"
    assert captured["args"] == ["--json"]


def test_brain_rebuild_main_dispatches_to_shim(monkeypatch: Any) -> None:
    """brain-rebuild's main() calls exec_shim with the correct shim name."""
    from brain.bin import rebuild

    captured: dict[str, Any] = {}

    def fake_exec_shim(name: str, args: Sequence[str]) -> None:
        captured["name"] = name
        captured["args"] = list(args)

    monkeypatch.setattr("brain.bin.rebuild.exec_shim", fake_exec_shim)
    monkeypatch.setattr(sys, "argv", ["brain-rebuild", "--clean-cache"])
    rebuild.main()

    assert captured["name"] == "brain-rebuild"
    assert captured["args"] == ["--clean-cache"]
