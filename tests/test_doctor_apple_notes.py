"""``brain doctor``'s ``apple notes`` row (RM-36): can ``osascript`` run here?

The probe only looks the binary up, through the ``which`` seam; it never runs
``osascript`` (that would raise the Automation prompt and read the owner's notes).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.doctor_apple_notes import apple_notes_doctor_check


def _forbidden(_name: str) -> str | None:
    raise AssertionError("off macOS the probe must not look anything up")


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_off_macos_the_row_is_ok_and_says_unavailable(platform: str) -> None:
    [check] = apple_notes_doctor_check(platform=platform, which=_forbidden)

    assert check.check == "apple notes"
    assert check.status == "ok"
    assert check.detail == "not macOS; brain ingest-apple-notes is unavailable here"


def test_osascript_on_path_is_ok_and_names_the_permission_prompt() -> None:
    seen: list[str] = []

    def which(name: str) -> str | None:
        seen.append(name)
        return "/usr/bin/osascript"

    [check] = apple_notes_doctor_check(platform="darwin", which=which)

    assert seen == ["osascript"]
    assert check.status == "ok"
    assert check.detail == (
        "osascript on PATH, for brain ingest-apple-notes "
        "(its first run asks for Automation permission)"
    )


def test_a_missing_osascript_on_macos_is_a_warning_with_a_remedy() -> None:
    [check] = apple_notes_doctor_check(platform="darwin", which=lambda _name: None)

    assert check.status == "warn"
    assert check.detail == "osascript not on PATH; brain ingest-apple-notes is disabled"
    assert check.remedy == "add /usr/bin to PATH"
    assert not check.lines[0].err  # a WARN goes to stdout, never fails doctor


def test_the_default_probe_is_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brain.doctor_apple_notes.shutil.which", lambda _name: None)

    [check] = apple_notes_doctor_check(platform="darwin")

    assert check.status == "warn"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_runtime_checks_include_the_row_once_for_their_platform(
    platform: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from brain.doctor_runtime import runtime_doctor_checks

    def no_launchctl(*_a: object, **_k: object) -> object:
        raise OSError("no launchctl in this test")  # never the real one

    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    monkeypatch.setattr("brain.doctor_runtime.subprocess.run", no_launchctl)
    monkeypatch.setattr("brain.doctor_apple_notes.shutil.which", lambda _name: None)
    checks = runtime_doctor_checks(
        launchd_dir=tmp_path, launchctl="launchctl", platform=platform, processes=()
    )

    [row] = [c for c in checks if c.check == "apple notes"]
    assert row.status == ("warn" if platform == "darwin" else "ok")
