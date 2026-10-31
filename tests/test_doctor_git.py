"""``brain doctor``'s ``git`` row (RM-43): is ``git`` on ``PATH``?

The probe only looks the binary up, through the ``which`` seam; it never runs
``git``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.doctor_git import git_doctor_check

pytestmark = pytest.mark.nodb


def test_git_on_path_is_ok() -> None:
    seen: list[str] = []

    def which(name: str) -> str | None:
        seen.append(name)
        return "/usr/bin/git"

    [check] = git_doctor_check(which=which)

    assert seen == ["git"]
    assert check.check == "git"
    assert check.status == "ok"
    assert check.detail == "git on PATH, for brain ingest-code"


def test_git_missing_is_a_warning_with_the_install_remedy() -> None:
    [check] = git_doctor_check(which=lambda _name: None)

    assert check.status == "warn"
    assert "brain ingest-code is disabled" in check.detail
    assert check.remedy == "xcode-select --install (or brew install git)"
    assert not check.lines[0].err  # a WARN goes to stdout, never fails doctor


def test_runtime_checks_include_the_row_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from brain.doctor_runtime import runtime_doctor_checks

    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    checks = runtime_doctor_checks(launchd_dir=tmp_path, launchctl="launchctl", platform="linux")

    assert [c.check for c in checks].count("git") == 1


def test_the_default_probe_is_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.doctor_git.shutil.which", lambda _name: None)

    [check] = git_doctor_check()

    assert check.status == "warn"
