"""Find the newest archive in a directory — backs `brain doctor`'s last-backup line."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..errors import BackupError
from .archive import read_manifest

#: Only files `brain backup` itself produces are considered — a stray tarball
#: dropped into the directory must never be reported as a usable backup.
ARCHIVE_GLOB = "brain-backup-*.tar.gz"


@dataclass(frozen=True)
class BackupSummary:
    """One discovered archive, cheap enough to list without unpacking it."""

    path: Path
    bytes: int
    created_at: datetime | None
    label: str
    documents: int | None

    @property
    def age_days(self) -> float | None:
        """Days since the backup was taken, or ``None`` if unknown."""
        if self.created_at is None:
            return None
        now = datetime.now(self.created_at.tzinfo)
        return (now - self.created_at).total_seconds() / 86400.0


def _summarize(path: Path) -> BackupSummary:
    """Read one archive's manifest, degrading gracefully if it is unreadable.

    A corrupt archive still deserves a row: reporting "there is a backup, but
    it cannot be read" is far more useful than silently skipping it, which
    would look identical to having no backup at all.
    """
    try:
        manifest = read_manifest(path)
    except BackupError:
        return BackupSummary(
            path=path,
            bytes=path.stat().st_size,
            created_at=None,
            label="",
            documents=None,
        )
    return BackupSummary(
        path=path,
        bytes=path.stat().st_size,
        created_at=manifest.created_at,
        label=manifest.label,
        documents=manifest.counts.get("documents"),
    )


def list_backups(directory: Path) -> list[BackupSummary]:
    """Every archive in ``directory``, newest filename first.

    Sorted by filename rather than mtime: the name carries a
    ``YYYYMMDD-HHMMSS`` stamp, which survives a copy that resets mtimes.
    """
    if not directory.is_dir():
        return []
    paths = sorted(directory.glob(ARCHIVE_GLOB), key=lambda p: p.name, reverse=True)
    return [_summarize(path) for path in paths]


def latest_backup(directory: Path) -> BackupSummary | None:
    """The newest archive in ``directory``, or ``None`` when there is none."""
    backups = list_backups(directory)
    return backups[0] if backups else None
