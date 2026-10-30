"""``brain doctor``'s ``github`` row (RM-31): is ``gh`` on ``PATH``?

The probe only looks the binary up, through the ``which`` seam; it never runs
``gh`` (``gh auth status`` would call the code host).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.doctor_github import github_doctor_check


def test_gh_on_path_is_ok() -> None:
    seen: list[str] = []

    def which(name: str) -> str | None:
        seen.append(name)
        return "/usr/local/bin/gh"

    [check] = github_doctor_check(which=which)

    assert seen == ["gh"]
    assert check.check == "github"
    assert check.status == "ok"
    assert check.detail == "gh CLI on PATH, for brain ingest-github"


def test_gh_missing_is_a_warning_with_the_install_remedy() -> None:
    [check] = github_doctor_check(which=lambda _name: None)

    assert check.status == "warn"
    assert "brain ingest-github is disabled" in check.detail
    assert check.remedy == "brew install gh && gh auth login"
    assert not check.lines[0].err  # a WARN goes to stdout, never fails doctor


def test_runtime_checks_include_the_row_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from brain.doctor_runtime import runtime_doctor_checks

    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    checks = runtime_doctor_checks(launchd_dir=tmp_path, launchctl="launchctl", platform="linux")

    assert [c.check for c in checks].count("github") == 1


def test_the_default_probe_is_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.doctor_github.shutil.which", lambda _name: None)

    [check] = github_doctor_check()

    assert check.status == "warn"
