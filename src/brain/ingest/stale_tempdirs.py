"""Remove this user's temporary directories that a killed run left behind.

A run that holds private copies in a ``mkdtemp`` directory removes it in ``finally``;
a run killed outright (SIGKILL, a power cut) cannot. The next run calls
:func:`sweep_stale_tempdirs` with its own prefix and the longest time a live run can
hold such a directory, so a directory still in use is never swept. Lifted out of
:mod:`brain.ingest.voice` (RM-38, sec-107 F4) when RM-41's history snapshots needed
the same rule (sec-114 F10).
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

__all__ = ["sweep_stale_tempdirs"]


def sweep_stale_tempdirs(
    prefix: str,
    *,
    max_age_s: float,
    root: Path | None = None,
    now: float | None = None,
    uid: int | None = None,
) -> int:
    """Remove ``prefix`` directories older than ``max_age_s``; the count removed.

    Only real directories (``lstat``) owned by ``uid`` (default: this process's) are
    removed. ``root`` (default: the temp directory), ``now`` and ``uid`` are test seams.
    """
    base = Path(tempfile.gettempdir()) if root is None else root
    cutoff = (time.time() if now is None else now) - max_age_s
    owner = os.getuid() if uid is None else uid
    try:
        entries = [e for e in os.scandir(base) if e.name.startswith(prefix)]
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue  # removed since the listing (another run's cleanup): nothing to sweep
        if stat.S_ISDIR(st.st_mode) and st.st_uid == owner and st.st_mtime < cutoff:
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
    return removed
