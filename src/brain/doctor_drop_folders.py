"""`brain doctor`'s ``drop folders`` row (roadmap ticket RM-33): can the drop folders run?

Reads ``$BRAIN_HOME/drop-folders.toml`` (:mod:`brain.settings.drop_folders`). No file is an
OK that says so: drop folders are optional. A file that does not load, image folders with
no way to OCR them, or audio folders with no way to transcribe them (RM-38), is a WARN with
the fix. Otherwise the row counts the folders and says
whether the ``com.brain.dropfolders`` LaunchAgent is installed (the ``daemons`` row checks
that it is loaded).

A module of its own, as :mod:`brain.doctor_ocr` is, so the probe does not spend
:mod:`brain.doctor_runtime`'s line budget (roadmap spec §2.2); ``runtime_doctor_checks``
composes it in. It never imports ``brain.cli`` at module scope; ``_check`` defers that.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .bin.launchd import _DROPFOLDERS_LABEL
from .config import ConfigError, _brain_home_root
from .doctor_runtime import _check
from .ingest.image import ocr_unavailable_hint
from .ingest.ocr import Which, resolve_ocr
from .ingest.whisper import WhisperSetup, resolve_whisper
from .settings.drop_folders import (
    CONFIG_NAME,
    DEFAULT_EXTENSIONS,
    VOICE_EXTENSIONS,
    load_drop_folders,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck

_NAME = "drop folders"
_GUIDE = "docs/guides/drop-folders.md"


def drop_folders_doctor_check(
    *,
    launchd_dir: Path,
    brain_home: Path | None = None,
    env: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    whisper: WhisperSetup | None = None,
) -> list[_DoctorCheck]:
    """The ``drop folders`` row. ``brain_home``, ``env``, ``which`` and ``whisper`` are seams.

    A ``ConfigError`` from the settings leaf names the entry and key, never a value, so
    it is shown. ``env`` defaults to ``os.environ`` (filled from the dotenv chain).
    """
    home = brain_home if brain_home is not None else _brain_home_root()
    try:
        config = load_drop_folders(home)
    except ConfigError as exc:
        return [_check(_NAME, "warn", str(exc), remedy=f"fix it; see {_GUIDE}")]
    if config.missing:
        # By index, never by path (rev-105 F6): the other folders still run.
        entries = ", ".join(f"folder[{i}]" for i in config.missing)
        return [
            _check(
                _NAME,
                "warn",
                f"{entries} in {CONFIG_NAME} does not exist or is not a directory "
                "and is skipped; the other folders still run",
                remedy=f"reconnect it or remove the entry; see {_GUIDE}",
            )
        ]
    folders = config.folders
    if not folders:
        return [_check(_NAME, "ok", f"none configured ($BRAIN_HOME/{CONFIG_NAME})")]
    images = [f for f in folders if f.extensions & DEFAULT_EXTENSIONS]
    if images:
        try:
            setup = resolve_ocr(os.environ if env is None else env, which=which)
        except ConfigError as exc:
            return [_check(_NAME, "warn", str(exc), remedy="fix it in $BRAIN_HOME/.env")]
        hint = ocr_unavailable_hint(setup)
        if hint is not None:
            return [
                _check(
                    _NAME,
                    "warn",
                    f"{len(images)} folder(s) take images, which are skipped: {hint}",
                )
            ]
    memos = [f for f in folders if f.extensions & VOICE_EXTENSIONS]
    if memos:
        try:
            voice = whisper or resolve_whisper(os.environ if env is None else env)
        except ConfigError as exc:
            return [_check(_NAME, "warn", str(exc), remedy="fix it in $BRAIN_HOME/.env")]
        if voice.unavailable is not None:
            detail = f"{len(memos)} folder(s) take voice memos, which are skipped: "
            return [_check(_NAME, "warn", detail + voice.unavailable)]
    auto = sum(1 for f in folders if f.auto)
    installed = (launchd_dir / f"{_DROPFOLDERS_LABEL}.plist").exists()
    schedule = (
        f"{_DROPFOLDERS_LABEL} installed"
        if installed
        else "not scheduled: brain-install-launchd --with-dropfolders"
    )
    return [_check(_NAME, "ok", f"{len(folders)} folder(s), {auto} run automatically; {schedule}")]
