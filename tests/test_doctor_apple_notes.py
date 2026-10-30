"""``brain doctor``'s ``apple notes`` row (RM-36): can ``osascript`` run here?

The probe only checks ``/usr/bin/osascript`` exists, through the ``is_file`` seam; it never runs
``osascript`` (that would raise the Automation prompt and read the owner's notes).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.doctor_apple_notes import apple_notes_doctor_check


def _forbidden(_path: Path) -> bool:
    raise AssertionError("off macOS the probe must not look anything up")


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_off_macos_the_row_is_ok_and_says_unavailable(platform: str) -> None:
    [check] = apple_notes_doctor_check(platform=platform, is_file=_forbidden)

    assert check.check == "apple notes"
    assert check.status == "ok"
    assert check.detail == "not macOS; brain ingest-apple-notes is unavailable here"


def test_osascript_present_is_ok_and_names_the_permission_prompt() -> None:
    seen: list[Path] = []

    def is_file(path: Path) -> bool:
        seen.append(path)
        return True

    [check] = apple_notes_doctor_check(platform="darwin", is_file=is_file)

    assert seen == [Path("/usr/bin/osascript")]  # the absolute path, never a PATH lookup
    assert check.status == "ok"
    assert check.detail == (
        "/usr/bin/osascript present, for brain ingest-apple-notes "
        "(its first run asks for Automation permission)"
    )


def test_a_missing_osascript_on_macos_is_a_warning_with_a_remedy() -> None:
    [check] = apple_notes_doctor_check(platform="darwin", is_file=lambda _path: False)

    assert check.status == "warn"
    assert check.detail == "/usr/bin/osascript missing; brain ingest-apple-notes is disabled"
    assert check.remedy == "check /usr/bin/osascript, which ships with macOS"
    assert not check.lines[0].err  # a WARN goes to stdout, never fails doctor


@pytest.mark.parametrize("exists", [True, False])
def test_the_default_probe_checks_the_file(
    exists: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "osascript"
    if exists:
        target.write_text("")
    monkeypatch.setattr("brain.doctor_apple_notes.OSASCRIPT_PATH", str(target))

    [check] = apple_notes_doctor_check(platform="darwin")

    assert check.status == ("ok" if exists else "warn")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_runtime_checks_include_the_row_once_for_their_platform(
    platform: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from brain.doctor_runtime import runtime_doctor_checks

    def no_launchctl(*_a: object, **_k: object) -> object:
        raise OSError("no launchctl in this test")  # never the real one

    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    monkeypatch.setattr("brain.doctor_runtime.subprocess.run", no_launchctl)
    monkeypatch.setattr("brain.doctor_apple_notes.OSASCRIPT_PATH", str(tmp_path / "missing"))
    checks = runtime_doctor_checks(
        launchd_dir=tmp_path, launchctl="launchctl", platform=platform, processes=()
    )

    [row] = [c for c in checks if c.check == "apple notes"]
    assert row.status == ("warn" if platform == "darwin" else "ok")
