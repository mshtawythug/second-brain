"""Atomic file-write helper shared by every body-rewriter under ``vault/``."""
import os
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (sibling tempfile + ``os.replace``).

    The temp file is a sibling of ``path`` (same parent directory) so the
    rename never crosses a filesystem boundary — ``os.replace`` is atomic
    on POSIX in that case (it's ``rename(2)`` underneath). A crash between
    the write and the rename leaves the original file intact.

    On any failure during the write or rename, the temp file is removed so
    we don't leave stray ``*.tmp`` siblings cluttering the vault. Tempfile
    cleanup uses ``unlink(missing_ok=True)`` because the failure may have
    happened before the file was created.
    """
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
