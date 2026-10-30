"""``brain doctor``'s ``ocr`` row (RM-32): can image-only PDFs be OCR'd?

``which`` and ``runner`` are seams, so no test needs or runs the real
``tesseract``. Missing tesseract is an OK, not a WARN: OCR is optional, and an
image-only PDF then ingests as an empty document with a hint.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from brain.doctor_ocr import ocr_doctor_check
from brain.doctor_runtime import runtime_doctor_checks
from brain.ingest.ocr import Runner

LISTING = b'List of available languages in "/opt/share/tessdata/" (2):\neng\nosd\n'


def _which(found: str | None) -> Callable[[str], str | None]:
    return lambda name: found if name == "tesseract" else None


def _runner(answers: dict[str, bytes | int | BaseException]) -> Runner:
    """Answer ``--version`` / ``--list-langs`` with stdout bytes, an exit code, or a raise."""

    def run(cmd: list[str], stdin: bytes, timeout: float) -> subprocess.CompletedProcess[bytes]:
        answer = answers[cmd[1]]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            return subprocess.CompletedProcess(cmd, answer, b"", b"SYNTHETIC stderr")
        return subprocess.CompletedProcess(cmd, 0, answer, b"")

    return run


HEALTHY = {"--version": b"tesseract 5.5.1\n leptonica-1.85.0\n", "--list-langs": LISTING}


def test_off_reports_off_and_probes_nothing() -> None:
    (check,) = ocr_doctor_check(
        env={"BRAIN_OCR": "off"}, which=_which("/opt/bin/tesseract"), runner=_runner({})
    )

    assert (check.check, check.status, check.detail) == ("ocr", "ok", "off, BRAIN_OCR=off")


def test_missing_tesseract_is_ok_and_says_how_to_install_it() -> None:
    (check,) = ocr_doctor_check(env={}, which=_which(None), runner=_runner({}))

    assert check.status == "ok"
    assert "tesseract not installed" in check.detail
    assert "brew install tesseract" in check.detail


def test_healthy_tesseract_reports_its_version_and_language() -> None:
    (check,) = ocr_doctor_check(env={}, which=_which("/opt/bin/tesseract"), runner=_runner(HEALTHY))

    assert check.status == "ok"
    assert check.detail == "tesseract 5.5.1, lang eng, up to 50 pages"


def test_a_missing_language_pack_is_a_warn_naming_the_codes() -> None:
    (check,) = ocr_doctor_check(
        env={"BRAIN_OCR_LANG": "eng+deu+fra"},
        which=_which("/opt/bin/tesseract"),
        runner=_runner(HEALTHY),
    )

    assert check.status == "warn"
    assert "deu, fra" in check.detail
    assert check.remedy is not None and "tesseract-lang" in check.remedy


@pytest.mark.parametrize(
    ("answers", "label"),
    [
        ({"--version": 1}, "OcrError[exit 1]"),
        ({"--version": subprocess.TimeoutExpired(["tesseract"], 5.0)}, "OcrTimeoutError"),
        ({**HEALTHY, "--list-langs": 2}, "OcrError[exit 2]"),
    ],
    ids=["version-exit", "version-timeout", "list-langs-exit"],
)
def test_a_tesseract_that_fails_its_probe_is_a_warn_class_only(
    answers: dict[str, bytes | int | BaseException], label: str
) -> None:
    (check,) = ocr_doctor_check(env={}, which=_which("/opt/bin/tesseract"), runner=_runner(answers))

    assert check.status == "warn"
    assert label in check.detail
    assert "SYNTHETIC" not in check.detail


def test_a_bad_knob_is_a_warn_naming_the_variable_not_the_value() -> None:
    (check,) = ocr_doctor_check(
        env={"BRAIN_OCR_MAX_PAGES": "synthetic-bad"}, which=_which(None), runner=_runner({})
    )

    assert check.status == "warn"
    assert "BRAIN_OCR_MAX_PAGES" in check.detail
    assert "synthetic-bad" not in check.detail


def test_the_row_is_composed_into_the_runtime_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_OCR", "off")
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))

    checks = runtime_doctor_checks(launchd_dir=tmp_path, launchctl="launchctl", platform="linux")

    assert [c.detail for c in checks if c.check == "ocr"] == ["off, BRAIN_OCR=off"]
