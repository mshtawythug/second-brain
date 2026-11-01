"""The ``$BRAIN_HOME/state/restored-from.json`` marker a restore leaves (roadmap RM-47).

A second machine runs a **read replica**: ``brain restore`` of an archive taken on
the primary, refreshed by restoring again. ``brain restore`` otherwise persists
nothing from the archive's ``manifest.json``, so after a successful restore it
records two facts here, the manifest's ``created_at`` and the archive's file
name, and ``brain doctor``'s ``replica`` row reads them back.

The file is small, but it is written and read under ``$BRAIN_HOME``, which other
tools can touch, so both directions are defensive:

* **write**: the ``state`` directory is opened ``O_NOFOLLOW`` and every later
  call goes through that descriptor, the payload goes to an ``O_EXCL`` temp file
  (0600) and is renamed into place. A symlink at the marker's name is replaced,
  never followed.
* **read**: ``O_NOFOLLOW`` and ``O_NONBLOCK`` (a FIFO cannot hang ``brain
  doctor``), a regular-file check and a 4 KiB cap, then a schema check.
* **names**: only the archive's basename is stored (a full path can carry a user
  name), with control and format characters (ANSI escapes, bidi overrides)
  stripped on write and again on read, since the doctor prints it.
* **errors**: fixed strings, never a path or the file's bytes.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..errors import BackupError
from .manifest import BackupManifest

MARKER_NAME = "restored-from.json"
#: A real marker is about 150 bytes; anything near this is not ours.
MARKER_MAX_BYTES = 4096
#: The longest archive name the marker keeps (a filesystem name limit).
ARCHIVE_NAME_MAX = 255
MARKER_SCHEMA = 1

_STATE_DIR = "state"
_UNREADABLE = "restored-from marker is unreadable"
_UNWRITABLE = "could not write the restored-from marker"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class ReplicaMarkerError(BackupError):
    """The marker could not be written or read. The message is fixed, with no path."""


@dataclass(frozen=True)
class RestoredFrom:
    """What the last successful ``brain restore`` on this machine restored."""

    created_at: datetime
    archive_name: str
    restored_at: datetime


def marker_path(brain_home: Path) -> Path:
    """Where the marker lives for ``brain_home``."""
    return brain_home / _STATE_DIR / MARKER_NAME


def _display_name(name: str) -> str:
    """Drop control and format characters (category C*) and cap the length."""
    kept = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    return kept[:ARCHIVE_NAME_MAX]


def write_marker(
    brain_home: Path, manifest: BackupManifest, archive: Path, *, now: datetime
) -> Path:
    """Record that ``archive`` (taken at ``manifest.created_at``) was restored at ``now``."""
    payload = json.dumps(
        {
            "schema": MARKER_SCHEMA,
            "created_at": manifest.created_at.isoformat(),
            "archive": _display_name(archive.name),
            "restored_at": now.isoformat(),
        },
        sort_keys=True,
    ).encode("utf-8")
    state = brain_home / _STATE_DIR
    tmp_name = f".{MARKER_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        dir_fd = os.open(state, _DIR_FLAGS)
    except OSError as exc:
        raise ReplicaMarkerError(_UNWRITABLE) from exc
    try:
        _write_through(dir_fd, tmp_name, payload)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=dir_fd)
        raise ReplicaMarkerError(_UNWRITABLE) from exc
    finally:
        os.close(dir_fd)
    return marker_path(brain_home)


def _write_through(dir_fd: int, tmp_name: str, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_name, MARKER_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def read_marker(brain_home: Path) -> RestoredFrom | None:
    """The marker's contents, or ``None`` when this machine was never restored."""
    try:
        dir_fd = os.open(brain_home / _STATE_DIR, _DIR_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReplicaMarkerError(_UNREADABLE) from exc
    try:
        raw = _read_capped(dir_fd)
    finally:
        os.close(dir_fd)
    if raw is None:
        return None
    try:
        return _parse(json.loads(raw.decode("utf-8")))
    except (ValueError, TypeError, KeyError) as exc:
        raise ReplicaMarkerError(_UNREADABLE) from exc


def _read_capped(dir_fd: int) -> bytes | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(MARKER_NAME, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReplicaMarkerError(_UNREADABLE) from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MARKER_MAX_BYTES:
        os.close(fd)
        raise ReplicaMarkerError(_UNREADABLE)
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MARKER_MAX_BYTES + 1)
    if len(raw) > MARKER_MAX_BYTES:
        raise ReplicaMarkerError(_UNREADABLE)
    return raw


def _aware(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp is not a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no offset")
    return parsed


def _parse(payload: Any) -> RestoredFrom:
    if not isinstance(payload, dict):
        raise TypeError("marker is not an object")
    archive = payload["archive"]
    if not isinstance(archive, str):
        raise TypeError("archive is not a string")
    return RestoredFrom(
        created_at=_aware(payload["created_at"]),
        archive_name=_display_name(archive),
        restored_at=_aware(payload["restored_at"]),
    )
