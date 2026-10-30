"""The drop folders, read from ``$BRAIN_HOME/drop-folders.toml`` (roadmap ticket RM-33).

A list of ``[[folder]]`` tables; a missing file means no folders::

    [[folder]]
    path = "~/Pictures/Screenshots"   # required; must exist; stored resolved
    kind = "screenshot"               # default; any VALID_SOURCE_KINDS member
    extensions = [".png", ".jpg", ".jpeg"]  # default; each served by an extractor
    sensitivity = "confidential"      # default: a screenshot can show anything
    auto = true                       # false: only `brain ingest-folder --dir` uses it
    mode = "ingest"                   # the only mode until RM-39 adds "capture"

The folders are a file rather than ``BRAIN_*`` variables because each is a table;
the settings-leaf rule (§2.3) still holds: one loader, one frozen value type
(:class:`brain.drop_folder.DropFolder`), and :class:`~brain.config.ConfigError`
messages that name the entry and key (``folder[1].kind``), never a value.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from brain.config import ConfigError
from brain.drop_folder import DropFolder
from brain.sensitivity import CONFIDENTIAL, VALID_SENSITIVITY_LEVELS
from brain.source_kinds import VALID_SOURCE_KINDS

CONFIG_NAME = "drop-folders.toml"
DEFAULT_KIND = "screenshot"
DEFAULT_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg"})
DEFAULT_SENSITIVITY = CONFIDENTIAL
_KEYS = frozenset({"path", "mode", "kind", "extensions", "sensitivity", "auto"})


def config_path(brain_home: Path) -> Path:
    """Where the folders are configured: ``$BRAIN_HOME/drop-folders.toml``."""
    return brain_home / CONFIG_NAME


def load_drop_folders(brain_home: Path) -> tuple[DropFolder, ...]:
    """Every configured folder, in file order; ``()`` when the file does not exist."""
    path = config_path(brain_home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"{CONFIG_NAME} could not be read") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # ``from None``: the decode error quotes the offending line.
        raise ConfigError(f"{CONFIG_NAME} is not valid TOML") from None
    if set(data) - {"folder"}:
        raise ConfigError(f"{CONFIG_NAME} has a top-level key other than [[folder]]")
    tables = data.get("folder", [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise ConfigError(f"{CONFIG_NAME}: folder must be [[folder]] tables")
    folders: list[DropFolder] = []
    for index, table in enumerate(tables):
        folder = _folder(index, table)
        if any(f.path == folder.path for f in folders):
            raise ConfigError(f"{CONFIG_NAME}: folder[{index}].path is configured twice")
        folders.append(folder)
    return tuple(folders)


def _folder(index: int, table: dict[str, Any]) -> DropFolder:
    where = f"{CONFIG_NAME}: folder[{index}]"
    if set(table) - _KEYS:
        raise ConfigError(f"{where} has an unknown key (allowed: {', '.join(sorted(_KEYS))})")
    mode = table.get("mode", "ingest")
    if mode == "capture":
        raise ConfigError(f"{where}.mode: 'capture' is not available until RM-39")
    if mode != "ingest":
        raise ConfigError(f"{where}.mode must be 'ingest'")
    kind = table.get("kind", DEFAULT_KIND)
    if not isinstance(kind, str) or kind not in VALID_SOURCE_KINDS:
        raise ConfigError(f"{where}.kind must be one of {', '.join(sorted(VALID_SOURCE_KINDS))}")
    sensitivity = table.get("sensitivity", DEFAULT_SENSITIVITY)
    if not isinstance(sensitivity, str) or sensitivity not in VALID_SENSITIVITY_LEVELS:
        levels = ", ".join(sorted(VALID_SENSITIVITY_LEVELS))
        raise ConfigError(f"{where}.sensitivity must be one of {levels}")
    auto = table.get("auto", True)
    if not isinstance(auto, bool):
        raise ConfigError(f"{where}.auto must be true or false")
    return DropFolder(
        path=_path(where, table.get("path")),
        mode="ingest",
        kind=kind,
        extensions=_extensions(where, table.get("extensions")),
        sensitivity=sensitivity,
        auto=auto,
    )


def _path(where: str, raw: object) -> Path:
    if not isinstance(raw, str):
        raise ConfigError(f"{where}.path is required: an absolute path, or one starting with ~")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{where}.path must be absolute (or start with ~)")
    if not path.is_dir():
        raise ConfigError(f"{where}.path does not exist or is not a directory")
    return path.resolve(strict=True)


def _extensions(where: str, raw: object) -> frozenset[str]:
    if raw is None:
        return DEFAULT_EXTENSIONS
    # Lazy: the registry is only needed when a folder names its own extensions.
    from brain.ingest.dispatch import supported_extensions

    served = set(supported_extensions())
    if not isinstance(raw, list) or not raw or not all(isinstance(e, str) for e in raw):
        raise ConfigError(f"{where}.extensions must be a non-empty list like [\".png\"]")
    extensions = frozenset(e.lower() for e in raw)
    if not extensions <= served:
        raise ConfigError(
            f"{where}.extensions must each be one of {', '.join(sorted(served))}"
        )
    return extensions
