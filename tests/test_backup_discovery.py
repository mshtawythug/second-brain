"""Backup discovery — what `brain doctor` reads (F3 §4)."""
from __future__ import annotations

import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brain.backup.discovery import BackupSummary, latest_backup, list_backups
from tests.backup_fakes import repo_root_guard  # noqa: F401


def _archive(directory: Path, name: str, *, manifest: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    staging = directory / f".staging-{name}"
    staging.mkdir()
    (staging / "manifest.json").write_text(manifest, encoding="utf-8")
    target = directory / name
    with tarfile.open(target, "w:gz") as tar:
        tar.add(staging / "manifest.json", arcname="manifest.json")
    return target


def test_no_directory_yields_no_backups(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "missing") == []
    assert latest_backup(tmp_path / "missing") is None


def test_empty_directory_yields_no_backups(tmp_path: Path) -> None:
    (tmp_path / "backups").mkdir()

    assert latest_backup(tmp_path / "backups") is None


def test_newest_by_filename_wins(tmp_path: Path) -> None:
    """Sorted by the embedded timestamp, which survives a copy that resets mtimes."""
    import json

    from tests.test_backup_manifest import _manifest

    root = tmp_path / "backups"
    payload = json.dumps(_manifest().to_dict())
    _archive(root, "brain-backup-20260101-090000.tar.gz", manifest=payload)
    _archive(root, "brain-backup-20260725-141203.tar.gz", manifest=payload)

    newest = latest_backup(root)

    assert newest is not None
    assert newest.path.name == "brain-backup-20260725-141203.tar.gz"
    assert [b.path.name for b in list_backups(root)] == [
        "brain-backup-20260725-141203.tar.gz",
        "brain-backup-20260101-090000.tar.gz",
    ]


def test_unreadable_archive_is_still_listed(tmp_path: Path) -> None:
    """'There is a backup but it cannot be read' beats silently showing none."""
    root = tmp_path / "backups"
    root.mkdir()
    (root / "brain-backup-20260725-141203.tar.gz").write_bytes(b"corrupt")

    newest = latest_backup(root)

    assert newest is not None
    assert newest.created_at is None
    assert newest.documents is None
    assert newest.age_days is None


def test_stray_tarballs_are_ignored(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    (root / "some-other-archive.tar.gz").write_bytes(b"not ours")

    assert latest_backup(root) is None


def test_age_days_measures_from_created_at() -> None:
    created = datetime.now(UTC) - timedelta(days=3)
    summary = BackupSummary(
        path=Path("/tmp/x.tar.gz"),
        bytes=10,
        created_at=created,
        label="",
        documents=1,
    )

    assert summary.age_days is not None
    assert 2.9 < summary.age_days < 3.1
