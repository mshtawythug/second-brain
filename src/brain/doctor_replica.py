"""`brain doctor`'s ``replica`` row (roadmap ticket RM-47): is this machine a restored copy?

A second machine runs a read replica by restoring the primary's archives (the
recipe is ``docs/guides/second-machine.md``). Each successful ``brain restore``
writes ``$BRAIN_HOME/state/restored-from.json``; this row reads it back and says
which backup the machine holds, and that anything written here is lost at the
next restore. With no marker there is no row.

A primary recovered from its own backup also carries the marker, so the row
names the file to remove (RM-47.D1). The marker is untrusted on read; the row
never echoes its bytes or the resolved path (:mod:`brain.backup.replica_marker`).

A module of its own, as :mod:`brain.doctor_git` is, so the probe does not spend
:mod:`brain.doctor_runtime`'s line budget (roadmap spec §2.2). ``brain.backup``
(psycopg, the restore module) is imported inside the probe, not at import time.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from .config import _brain_home_root
from .doctor_runtime import _check

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck

_NAME = "replica"
_GUIDE = "docs/guides/second-machine.md"
_MARKER = "$BRAIN_HOME/state/restored-from.json"
_PRIMARY_NOTE = (
    f"a primary recovered from a backup is not a replica: remove {_MARKER} ({_GUIDE})"
)


def replica_doctor_check(*, brain_home: Path | None = None) -> list[_DoctorCheck]:
    """The ``replica`` row. ``brain_home`` is a test seam (default ``$BRAIN_HOME``)."""
    from .backup.replica_marker import ReplicaMarkerError, read_marker

    home = brain_home if brain_home is not None else _brain_home_root()
    try:
        marker = read_marker(home)
    except ReplicaMarkerError:
        return [
            _check(
                _NAME,
                "warn",
                "restored-from marker is unreadable",
                remedy=f'rm "{_MARKER}"',
                notes=(f"a replica writes it again at its next brain restore ({_GUIDE})",),
            )
        ]
    if marker is None:
        return []
    taken = f"{marker.created_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    detail = (
        f"read replica, restored from a backup taken at {taken} ({marker.archive_name}); "
        "writes here are lost at the next restore"
    )
    return [_check(_NAME, "ok", detail, notes=(_PRIMARY_NOTE,))]
