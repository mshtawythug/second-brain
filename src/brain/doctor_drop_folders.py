"""`brain doctor`'s ``drop folders`` row (roadmap ticket RM-33): can the drop folders run?

Reads ``$BRAIN_HOME/drop-folders.toml`` (:mod:`brain.settings.drop_folders`). No file is an
OK that says so: drop folders are optional. A file that does not load, or image folders with
no way to OCR them, is a WARN with the fix. Otherwise the row counts the folders and says
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
from .settings.drop_folders import CONFIG_NAME, DEFAULT_EXTENSIONS, load_drop_folders

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
) -> list[_DoctorCheck]:
    """The ``drop folders`` row. ``brain_home``, ``env`` and ``which`` are test seams.

    A ``ConfigError`` from the settings leaf names the entry and key, never a value, so
    it is shown. ``env`` defaults to ``os.environ`` (filled from the dotenv chain).
    """
    home = brain_home if brain_home is not None else _brain_home_root()
    try:
        folders = load_drop_folders(home)
    except ConfigError as exc:
        return [_check(_NAME, "warn", str(exc), remedy=f"fix it; see {_GUIDE}")]
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
    auto = sum(1 for f in folders if f.auto)
    installed = (launchd_dir / f"{_DROPFOLDERS_LABEL}.plist").exists()
    schedule = (
        f"{_DROPFOLDERS_LABEL} installed"
        if installed
        else "not scheduled: brain-install-launchd --with-dropfolders"
    )
    return [_check(_NAME, "ok", f"{len(folders)} folder(s), {auto} run automatically; {schedule}")]
