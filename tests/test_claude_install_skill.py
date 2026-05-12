"""Tests for brain.cli_claude — install_skill() filesystem behaviour."""
from __future__ import annotations

from importlib.resources import files as resource_files
from pathlib import Path

import pytest

from brain.cli_claude import install_skill


def _pkg_bytes() -> bytes:
    """Return the canonical SKILL.md bytes from package data."""
    return (resource_files("brain.templates.skill") / "SKILL.md").read_bytes()


# ---------------------------------------------------------------------------
# 1. Fresh install — no pre-existing directory
# ---------------------------------------------------------------------------


def test_install_fresh(tmp_path: Path) -> None:
    """Installing into an empty tmp_path creates brain/SKILL.md with correct content."""
    install_skill(target_root=tmp_path)

    target = tmp_path / "brain" / "SKILL.md"
    assert target.is_file(), "SKILL.md should exist after fresh install"
    assert target.read_bytes() == _pkg_bytes(), "installed bytes must match package data"


# ---------------------------------------------------------------------------
# 2. Idempotent — same bytes → no rewrite, "skill up to date" message
# ---------------------------------------------------------------------------


def test_install_idempotent_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Re-installing with identical bytes prints 'skill up to date' and does not rewrite."""
    skill_dir = tmp_path / "brain"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "SKILL.md"
    target.write_bytes(_pkg_bytes())

    mtime_before = target.stat().st_mtime

    install_skill(target_root=tmp_path)

    captured = capsys.readouterr()
    assert "skill up to date" in captured.out, "expected 'skill up to date' on stdout"
    assert target.stat().st_mtime == mtime_before, "mtime must not change on no-op"


# ---------------------------------------------------------------------------
# 3. Differing target + --force → overwrites without prompting
# ---------------------------------------------------------------------------


def test_install_differing_target_force_overwrites(tmp_path: Path) -> None:
    """--force replaces a differing SKILL.md without interactive prompt."""
    skill_dir = tmp_path / "brain"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "SKILL.md"
    target.write_bytes(b"old stale content that differs from package data")

    install_skill(target_root=tmp_path, force=True)

    assert target.read_bytes() == _pkg_bytes(), (
        "bytes must match package data after forced overwrite"
    )


# ---------------------------------------------------------------------------
# 4. Uninstall — removes file and empty directory
# ---------------------------------------------------------------------------


def test_uninstall_removes_file_and_empty_dir(tmp_path: Path) -> None:
    """Uninstall removes SKILL.md and the now-empty brain/ directory."""
    install_skill(target_root=tmp_path)

    skill_dir = tmp_path / "brain"
    target = skill_dir / "SKILL.md"
    assert target.is_file(), "precondition: SKILL.md should exist"

    install_skill(target_root=tmp_path, uninstall=True)

    assert not target.exists(), "SKILL.md should be gone after uninstall"
    assert not skill_dir.exists(), "brain/ directory should be removed when empty"


# ---------------------------------------------------------------------------
# 5. Uninstall with other files present — removes file, leaves directory
# ---------------------------------------------------------------------------


def test_uninstall_preserves_dir_with_other_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Uninstall removes SKILL.md but leaves brain/ intact when other files exist."""
    install_skill(target_root=tmp_path)

    skill_dir = tmp_path / "brain"
    other_file = skill_dir / "other.txt"
    other_file.write_text("unrelated content", encoding="utf-8")

    install_skill(target_root=tmp_path, uninstall=True)

    target = skill_dir / "SKILL.md"
    assert not target.exists(), "SKILL.md must be removed"
    assert skill_dir.is_dir(), "brain/ must remain when it contains other files"
    assert other_file.exists(), "other.txt must be preserved"

    captured = capsys.readouterr()
    assert "not empty" in captured.err, "warning about non-empty dir should appear on stderr"
