"""`brain doctor`'s ``drop folders`` row (RM-33).

``$BRAIN_HOME`` and the LaunchAgents directory are ``tmp_path`` folders; ``which`` is a
stub, so the real ``tesseract`` is never looked up.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.doctor_drop_folders import drop_folders_doctor_check
from brain.doctor_runtime import runtime_doctor_checks

ON_PATH = "/fake/bin/tesseract"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    brain_home = tmp_path / "home"
    brain_home.mkdir()
    return brain_home


@pytest.fixture
def agents(tmp_path: Path) -> Path:
    directory = tmp_path / "agents"
    directory.mkdir()
    return directory


def _folder(tmp_path: Path, name: str, extra: str = "") -> str:
    path = tmp_path / name
    path.mkdir()
    return f'[[folder]]\npath = "{path}"\n{extra}\n'


def _check(home: Path, agents: Path, *, ocr: str = "auto", binary: str | None = ON_PATH):
    (check,) = drop_folders_doctor_check(
        launchd_dir=agents, brain_home=home, env={"BRAIN_OCR": ocr}, which=lambda _: binary
    )
    return check


def test_no_file_is_ok_and_says_none_are_configured(home: Path, agents: Path) -> None:
    check = _check(home, agents)

    assert check.status == "ok"
    assert "none configured" in check.detail


def test_a_bad_file_warns_naming_the_key(home: Path, agents: Path, tmp_path: Path) -> None:
    (home / "drop-folders.toml").write_text(_folder(tmp_path, "d", 'kind = "nope"'))

    check = _check(home, agents)

    assert check.status == "warn"
    assert "folder[0].kind" in check.detail
    assert "nope" not in check.detail


def test_image_folders_without_tesseract_warn_with_the_fix(
    home: Path, agents: Path, tmp_path: Path
) -> None:
    (home / "drop-folders.toml").write_text(_folder(tmp_path, "d"))

    check = _check(home, agents, binary=None)

    assert check.status == "warn"
    assert "brew install tesseract" in check.detail


def test_image_folders_with_ocr_off_warn_with_the_fix(
    home: Path, agents: Path, tmp_path: Path
) -> None:
    (home / "drop-folders.toml").write_text(_folder(tmp_path, "d"))

    check = _check(home, agents, ocr="off")

    assert check.status == "warn"
    assert "BRAIN_OCR=auto" in check.detail


def test_a_bad_ocr_knob_warns(home: Path, agents: Path, tmp_path: Path) -> None:
    (home / "drop-folders.toml").write_text(_folder(tmp_path, "d"))

    check = _check(home, agents, ocr="sometimes")

    assert check.status == "warn"
    assert "BRAIN_OCR" in check.detail


def test_folders_without_images_need_no_ocr(home: Path, agents: Path, tmp_path: Path) -> None:
    (home / "drop-folders.toml").write_text(
        _folder(tmp_path, "d", 'kind = "manual"\nextensions = [".md"]')
    )

    check = _check(home, agents, ocr="off")

    assert check.status == "ok"


def test_configured_and_not_scheduled(home: Path, agents: Path, tmp_path: Path) -> None:
    (home / "drop-folders.toml").write_text(
        _folder(tmp_path, "a") + _folder(tmp_path, "b", "auto = false")
    )

    check = _check(home, agents)

    assert check.status == "ok"
    assert check.detail == (
        "2 folder(s), 1 run automatically; "
        "not scheduled: brain-install-launchd --with-dropfolders"
    )


def test_configured_and_scheduled(home: Path, agents: Path, tmp_path: Path) -> None:
    (home / "drop-folders.toml").write_text(_folder(tmp_path, "a"))
    (agents / "com.brain.dropfolders.plist").write_text("<plist/>")

    check = _check(home, agents)

    assert check.detail == "1 folder(s), 1 run automatically; com.brain.dropfolders installed"


def test_brain_home_defaults_to_the_resolved_root(
    home: Path, agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_HOME", str(home))

    (check,) = drop_folders_doctor_check(launchd_dir=agents)

    assert "none configured" in check.detail


def test_runtime_doctor_composes_the_row(
    home: Path, agents: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_HOME", str(home))

    checks = runtime_doctor_checks(launchd_dir=agents, platform="linux", processes=[])

    assert "drop folders" in [c.check for c in checks]


def test_a_missing_folder_warns_by_index_not_path(home: Path, agents: Path, tmp_path: Path) -> None:
    gone = tmp_path / "unplugged-volume"
    (home / "drop-folders.toml").write_text(
        _folder(tmp_path, "a") + f'[[folder]]\npath = "{gone}"\n'
    )

    check = _check(home, agents)

    assert check.status == "warn"
    assert "folder[1]" in check.detail
    assert "the other folders still run" in check.detail
    assert "unplugged-volume" not in check.detail
