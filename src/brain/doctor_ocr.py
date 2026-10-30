"""`brain doctor`'s ``ocr`` row (roadmap ticket RM-32): can image-only PDFs be OCR'd?

OCR needs the ``tesseract`` binary (``brew install tesseract``), found on ``PATH``. Without it an
image-only PDF ingests as an empty document with a hint, so a missing binary is an OK that says
so, not a WARN: OCR is optional, as the reranker is. A tesseract that fails its ``--version``
probe, or lacks a language ``BRAIN_OCR_LANG`` names, is a WARN.

A module of its own, as :mod:`brain.doctor_reranker` is, so the probe does not spend
:mod:`brain.doctor_runtime`'s line budget (roadmap spec §2.2); ``runtime_doctor_checks`` composes
it in. It never imports ``brain.cli`` at module scope; ``_check`` defers that.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from typing import TYPE_CHECKING

from .config import ConfigError
from .doctor_runtime import _check
from .errors import describe_failure
from .ingest.ocr import (
    TESSERACT,
    OcrError,
    Runner,
    Which,
    installed_languages,
    run_subprocess,
    tesseract_version,
)
from .settings.ocr import load_ocr_settings

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck

_NAME = "ocr"


def ocr_doctor_check(
    *,
    env: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    runner: Runner = run_subprocess,
) -> list[_DoctorCheck]:
    """The ``ocr`` row. ``which`` and ``runner`` are test seams.

    ``env`` defaults to ``os.environ``, which ``Config.load()`` has already filled from the
    dotenv chain. A ``ConfigError``'s message names variables and never a value (the settings
    leaf's rule), so it is shown; a probe failure is shown by class only.
    """
    try:
        settings = load_ocr_settings(os.environ if env is None else env)
    except ConfigError as exc:
        return [_check(_NAME, "warn", str(exc), remedy="fix it in $BRAIN_HOME/.env")]
    if not settings.enabled:
        return [_check(_NAME, "ok", "off, BRAIN_OCR=off")]
    binary = which(TESSERACT)
    if binary is None:
        return [
            _check(
                _NAME,
                "ok",
                "tesseract not installed, so image-only PDFs ingest empty; "
                "brew install tesseract to OCR them",
            )
        ]
    try:
        version = tesseract_version(binary, runner=runner)
        languages = installed_languages(binary, runner=runner)
    except OcrError as exc:
        return [
            _check(
                _NAME,
                "warn",
                f"tesseract at {binary} failed its probe ({describe_failure(exc)})",
                remedy="brew reinstall tesseract",
            )
        ]
    missing = sorted(set(settings.lang.split("+")) - languages)
    if missing:
        return [
            _check(
                _NAME,
                "warn",
                f"{version} lacks BRAIN_OCR_LANG language(s): {', '.join(missing)}",
                remedy="brew install tesseract-lang",
            )
        ]
    detail = f"{version}, lang {settings.lang}, up to {settings.max_pages} pages"
    return [_check(_NAME, "ok", detail)]
