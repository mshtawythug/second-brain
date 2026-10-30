"""Local OCR for image-only PDF pages: the ``OcrEngine`` Protocol and its tesseract backend (RM-32).

:class:`TesseractOcr` runs ``tesseract stdin stdout -l <lang>`` with one page's PNG on
stdin and a per-page timeout, through an injectable runner so tests never need the binary.
OCR runs on this machine only; nothing leaves it.

**Errors carry no document text.** tesseract can echo recognised words on stderr, so stderr
is never read into a message, and a timeout drops ``subprocess.TimeoutExpired`` (which holds
the partial output) with ``from None``. :class:`OcrError`'s ``log_detail`` is the
content-free part (``exit 1``) that :func:`brain.errors.describe_failure` prints.

:func:`resolve_ocr` turns the ``BRAIN_OCR*`` knobs (:mod:`brain.settings.ocr`) into an engine,
or ``None`` when OCR is off or the binary is missing. ``brain doctor``'s ``ocr`` row uses
:func:`tesseract_version` and :func:`installed_languages`.
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from brain.errors import BrainError
from brain.settings.ocr import OcrSettings, load_ocr_settings

#: The ticket's per-page bound: a page tesseract has not finished in a minute is skipped.
PAGE_TIMEOUT_S = 60.0
#: Pages are rendered at this resolution, tesseract's recommended minimum for print.
RENDER_DPI = 200
#: ``--version`` and ``--list-langs`` answer at once; the bound only stops a hang.
PROBE_TIMEOUT_S = 5.0

TESSERACT = "tesseract"

#: ``(cmd, stdin, timeout) -> CompletedProcess``. May raise ``subprocess.TimeoutExpired``
#: or ``OSError``; must not raise on a non-zero exit.
Runner = Callable[[list[str], bytes, float], "subprocess.CompletedProcess[bytes]"]
Which = Callable[[str], "str | None"]


class OcrError(BrainError):
    """An OCR call failed. The message never carries recognised text.

    ``log_detail`` is the content-free part :func:`brain.errors.describe_failure` prints.
    """

    def __init__(self, message: str, *, log_detail: str = "") -> None:
        super().__init__(message)
        self.log_detail = log_detail


class OcrTimeoutError(OcrError):
    """An OCR call ran past its timeout."""


class OcrEngine(Protocol):
    """Turns one page image into text. ``name`` is what ``metadata.ocr.engine`` records."""

    @property
    def name(self) -> str: ...

    def image_to_text(self, png: bytes, *, lang: str, timeout: float) -> str: ...


def run_subprocess(
    cmd: list[str], stdin: bytes, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    """The production :data:`Runner`: no shell, output captured, no raise on a non-zero exit."""
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout, check=False)


def _run(runner: Runner, cmd: list[str], stdin: bytes, timeout: float) -> bytes:
    """Run ``cmd`` and return its stdout, mapping every failure onto :class:`OcrError`."""
    try:
        result = runner(cmd, stdin, timeout)
    except subprocess.TimeoutExpired:
        # ``from None``: the TimeoutExpired holds tesseract's partial output.
        raise OcrTimeoutError(f"tesseract ran past {timeout:g}s") from None
    except OSError as exc:
        raise OcrError("tesseract could not be started") from exc
    if result.returncode != 0:
        raise OcrError(
            f"tesseract exited {result.returncode}", log_detail=f"exit {result.returncode}"
        )
    return result.stdout


@dataclass(frozen=True)
class TesseractOcr:
    """:class:`OcrEngine` over the ``tesseract`` CLI. ``runner`` is the test seam."""

    binary: str = TESSERACT
    runner: Runner = run_subprocess

    @property
    def name(self) -> str:
        return TESSERACT

    def image_to_text(self, png: bytes, *, lang: str, timeout: float) -> str:
        cmd = [self.binary, "stdin", "stdout", "-l", lang]
        return _run(self.runner, cmd, png, timeout).decode("utf-8", errors="replace")


@dataclass(frozen=True)
class OcrSetup:
    """The resolved knobs and engine. ``engine`` is ``None`` when OCR cannot or must not run.

    ``missing_binary`` is true only when ``BRAIN_OCR=auto`` and ``tesseract`` is not on
    ``PATH``: the one case where the user is told how to turn OCR on.
    """

    settings: OcrSettings
    engine: OcrEngine | None
    missing_binary: bool = False


def resolve_ocr(
    env: Mapping[str, str],
    *,
    which: Which = shutil.which,
    runner: Runner | None = None,
) -> OcrSetup:
    """Read the knobs from ``env`` and find ``tesseract``. ``which`` and ``runner`` are seams."""
    settings = load_ocr_settings(env)
    if not settings.enabled:
        return OcrSetup(settings=settings, engine=None)
    binary = which(TESSERACT)
    if binary is None:
        return OcrSetup(settings=settings, engine=None, missing_binary=True)
    engine = TesseractOcr(binary=binary, runner=runner if runner is not None else run_subprocess)
    return OcrSetup(settings=settings, engine=engine)


def tesseract_version(binary: str, *, runner: Runner = run_subprocess) -> str:
    """The first line of ``tesseract --version`` (``tesseract 5.5.1``)."""
    lines = _run(runner, [binary, "--version"], b"", PROBE_TIMEOUT_S).decode(
        "utf-8", errors="replace"
    ).splitlines()
    return lines[0].strip() if lines else f"{TESSERACT} (unknown version)"


def installed_languages(binary: str, *, runner: Runner = run_subprocess) -> frozenset[str]:
    """The codes ``tesseract --list-langs`` prints, after its ``List of available ...`` header."""
    output = _run(runner, [binary, "--list-langs"], b"", PROBE_TIMEOUT_S)
    lines = output.decode("utf-8", errors="replace").splitlines()[1:]
    return frozenset(line.strip() for line in lines if line.strip())
