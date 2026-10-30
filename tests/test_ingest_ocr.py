"""``brain.ingest.ocr``: the ``OcrEngine`` Protocol and its tesseract backend (RM-32).

``TesseractOcr`` is driven through its injectable runner, so no test needs the
real binary; ``run_subprocess`` (the production runner) is exercised against
``sys.executable``. All image bytes and recognised text are synthetic.
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

import pytest

from brain.errors import describe_failure
from brain.ingest.ocr import (
    PAGE_TIMEOUT_S,
    OcrEngine,
    OcrError,
    OcrTimeoutError,
    TesseractOcr,
    installed_languages,
    resolve_ocr,
    run_subprocess,
    tesseract_version,
)
from brain.settings.ocr import OcrSettings

pytestmark = pytest.mark.nodb

PNG = b"\x89PNG synthetic page"


class _Runner:
    """Records each call and answers with a canned process result (or raises)."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        returncode: int = 0,
        stderr: bytes = b"",
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], bytes, float]] = []
        self._result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self._raises = raises

    def __call__(
        self, cmd: list[str], stdin: bytes, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((cmd, stdin, timeout))
        if self._raises is not None:
            raise self._raises
        return self._result


def test_tesseract_reads_the_png_on_stdin_and_returns_stdout() -> None:
    runner = _Runner(stdout=b"SYNTHETIC RECEIPT 12.34\n")
    engine = TesseractOcr(binary="/opt/bin/tesseract", runner=runner)

    text = engine.image_to_text(PNG, lang="eng+deu", timeout=7.5)

    assert text == "SYNTHETIC RECEIPT 12.34\n"
    assert runner.calls == [
        (["/opt/bin/tesseract", "stdin", "stdout", "-l", "eng+deu"], PNG, 7.5)
    ]
    assert engine.name == "tesseract"


def test_invalid_utf8_from_tesseract_is_replaced_not_raised() -> None:
    engine = TesseractOcr(binary="tesseract", runner=_Runner(stdout=b"ok \xff"))

    assert engine.image_to_text(PNG, lang="eng", timeout=1.0) == "ok �"


def test_a_timeout_is_an_ocr_timeout_error_with_no_chained_output() -> None:
    expired = subprocess.TimeoutExpired(["tesseract"], 1.0, output=b"SYNTHETIC partial")
    engine = TesseractOcr(binary="tesseract", runner=_Runner(raises=expired))

    with pytest.raises(OcrTimeoutError) as caught:
        engine.image_to_text(PNG, lang="eng", timeout=1.0)

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert describe_failure(caught.value) == "OcrTimeoutError"
    assert isinstance(caught.value, OcrError)


def test_a_non_zero_exit_names_the_code_and_never_the_stderr() -> None:
    runner = _Runner(returncode=1, stderr=b"SYNTHETIC recognised words echoed on stderr")
    engine = TesseractOcr(binary="tesseract", runner=runner)

    with pytest.raises(OcrError) as caught:
        engine.image_to_text(PNG, lang="eng", timeout=1.0)

    assert describe_failure(caught.value) == "OcrError[exit 1]"
    assert "SYNTHETIC" not in str(caught.value)


def test_a_binary_that_cannot_start_is_an_ocr_error_keeping_the_errno() -> None:
    missing = FileNotFoundError(2, "No such file or directory", "/gone/tesseract")
    engine = TesseractOcr(binary="/gone/tesseract", runner=_Runner(raises=missing))

    with pytest.raises(OcrError) as caught:
        engine.image_to_text(PNG, lang="eng", timeout=1.0)

    assert describe_failure(caught.value) == "OcrError from FileNotFoundError[ENOENT]"


def test_tesseract_ocr_satisfies_the_engine_protocol() -> None:
    engine: OcrEngine = TesseractOcr(binary="tesseract")

    assert engine.name == "tesseract"
    assert PAGE_TIMEOUT_S == 60.0


def test_run_subprocess_pipes_stdin_and_captures_stdout() -> None:
    echo = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"
    cmd = [sys.executable, "-c", echo]

    result = run_subprocess(cmd, b"abc", 30.0)

    assert (result.returncode, result.stdout) == (0, b"cba")


def test_run_subprocess_raises_timeout_expired() -> None:
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

    with pytest.raises(subprocess.TimeoutExpired):
        run_subprocess(cmd, b"", 0.5)


def test_run_subprocess_does_not_raise_on_a_non_zero_exit() -> None:
    result = run_subprocess([sys.executable, "-c", "raise SystemExit(3)"], b"", 30.0)

    assert result.returncode == 3


# ---------------------------------------------------------------------------
# resolve_ocr
# ---------------------------------------------------------------------------


def _which(found: str | None) -> tuple[list[str], Callable[[str], str | None]]:
    asked: list[str] = []

    def which(name: str) -> str | None:
        asked.append(name)
        return found

    return asked, which


def test_resolve_off_builds_no_engine_and_does_not_look_for_the_binary() -> None:
    asked, which = _which("/opt/bin/tesseract")

    setup = resolve_ocr({"BRAIN_OCR": "off"}, which=which)

    assert setup.engine is None
    assert not setup.missing_binary
    assert setup.settings.mode == "off"
    assert asked == []


def test_resolve_auto_without_tesseract_reports_the_missing_binary() -> None:
    asked, which = _which(None)

    setup = resolve_ocr({}, which=which)

    assert setup.engine is None
    assert setup.missing_binary
    assert asked == ["tesseract"]


def test_resolve_auto_with_tesseract_builds_an_engine_on_its_path() -> None:
    runner = _Runner(stdout=b"text")
    _, which = _which("/opt/bin/tesseract")

    setup = resolve_ocr(
        {"BRAIN_OCR_LANG": "deu", "BRAIN_OCR_MAX_PAGES": "4"},
        which=which,
        runner=runner,
    )

    assert setup.settings == OcrSettings(mode="auto", lang="deu", max_pages=4)
    assert not setup.missing_binary
    assert setup.engine is not None
    setup.engine.image_to_text(PNG, lang="deu", timeout=1.0)
    assert runner.calls[0][0][0] == "/opt/bin/tesseract"


def test_resolve_auto_with_tesseract_defaults_to_the_subprocess_runner() -> None:
    _, which = _which("/opt/bin/tesseract")

    setup = resolve_ocr({}, which=which)

    assert isinstance(setup.engine, TesseractOcr)
    assert setup.engine.runner is run_subprocess


# ---------------------------------------------------------------------------
# doctor helpers
# ---------------------------------------------------------------------------


def test_tesseract_version_is_the_first_line_of_dash_dash_version() -> None:
    runner = _Runner(stdout=b"tesseract 5.5.1\n leptonica-1.85.0\n")

    assert tesseract_version("/opt/bin/tesseract", runner=runner) == "tesseract 5.5.1"
    assert runner.calls == [(["/opt/bin/tesseract", "--version"], b"", 5.0)]


def test_tesseract_version_with_empty_output_is_unknown() -> None:
    version = tesseract_version("tesseract", runner=_Runner(stdout=b""))

    assert version == "tesseract (unknown version)"


def test_tesseract_version_failing_raises_ocr_error() -> None:
    with pytest.raises(OcrError) as caught:
        tesseract_version("tesseract", runner=_Runner(returncode=2))

    assert describe_failure(caught.value) == "OcrError[exit 2]"


def test_installed_languages_skips_the_header_line() -> None:
    listing = b'List of available languages in "/opt/share/tessdata/" (3):\neng\nosd\ndeu\n'
    runner = _Runner(stdout=listing)

    assert installed_languages("tesseract", runner=runner) == frozenset({"eng", "osd", "deu"})
    assert runner.calls == [(["tesseract", "--list-langs"], b"", 5.0)]


def test_installed_languages_timing_out_raises_ocr_timeout_error() -> None:
    expired = subprocess.TimeoutExpired(["tesseract"], 5.0)

    with pytest.raises(OcrTimeoutError):
        installed_languages("tesseract", runner=_Runner(raises=expired))
