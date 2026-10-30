"""`brain doctor`'s ``voice memos`` row (roadmap ticket RM-38): can memos be transcribed?

Voice memos need ``BRAIN_WHISPER_MODEL`` (a whisper.cpp model file; none is downloaded),
macOS's ``afinfo`` and ``afconvert``, and ``whisper-cli`` (``brew install whisper-cpp``).
Unset is an OK that says how to turn it on: voice memos are optional, as OCR is. A model
set but unusable (no file there, no ``whisper-cli``, not macOS) is a WARN with the fix, and
so is a model file that is not safe to load (another user's, group- or world-writable, or
inside a drop folder: :func:`brain.ingest.whisper.model_file_problem`).
The model is named by its file name only, never its directory.

A module of its own, as :mod:`brain.doctor_ocr` is, so the probe does not spend
:mod:`brain.doctor_runtime`'s line budget (roadmap spec §2.2); ``runtime_doctor_checks``
composes it in. No tool is run: the probe only looks at files.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ConfigError
from .doctor_runtime import _check
from .ingest.whisper import (
    IsFile,
    ModelCheck,
    Which,
    _is_regular_file,
    model_file_problem,
    resolve_whisper,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck

_NAME = "voice memos"


def _file_size(path: Path) -> int:
    return path.stat().st_size


def voice_doctor_check(
    *,
    env: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    is_file: IsFile = _is_regular_file,
    file_size: Callable[[Path], int] = _file_size,
    model_problem: ModelCheck = model_file_problem,
) -> list[_DoctorCheck]:
    """The ``voice memos`` row. ``which``, ``is_file``, ``file_size`` and ``model_problem``
    are test seams.

    ``env`` defaults to ``os.environ``, filled from the dotenv chain by ``Config.load()``.
    A ``ConfigError`` names the variable and never the value, so it is shown.
    """
    try:
        setup = resolve_whisper(
            os.environ if env is None else env,
            which=which,
            is_file=is_file,
            model_problem=model_problem,
        )
    except ConfigError as exc:
        return [_check(_NAME, "warn", str(exc), remedy="fix it in $BRAIN_HOME/.env")]
    model = setup.settings.model
    if model is None:
        return [_check(_NAME, "ok", "off; set BRAIN_WHISPER_MODEL to transcribe voice memos")]
    if setup.unavailable is not None:
        return [_check(_NAME, "warn", setup.unavailable)]
    megabytes = round(file_size(model) / 1_000_000)
    detail = (
        f"whisper-cli at {setup.binary}, model {model.name} ({megabytes} MB), "
        f"memos up to {setup.settings.max_minutes} min"
    )
    return [_check(_NAME, "ok", detail)]
