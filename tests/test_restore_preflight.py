"""Restore preflight — every incompatibility refused before anything is touched."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from brain.backup.archive import DUMP_MEMBER, VAULT_MEMBER
from brain.backup.manifest import BackupManifest, FileEntry
from brain.backup.restore import Preflight, preflight
from brain.config import Config
from brain.db import migrations_dir
from tests.backup_fakes import repo_root_guard  # noqa: F401
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder
from tests.test_backup_manifest import EMBEDDER_NAME, EMBEDDING_DIM
from tests.test_backup_manifest import _manifest as _base_manifest

NOW = datetime(2026, 7, 25, 14, 12, 3, tzinfo=UTC)
DUMP_BYTES = b"PGDMP-synthetic-custom-format-payload"
VAULT_BYTES = b"synthetic-vault-tar"


def _installed_head() -> str:
    return sorted(migrations_dir().glob("*.sql"))[-1].name


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    """An unpacked archive whose members match their manifest checksums."""
    root = tmp_path / "unpacked"
    (root / "db").mkdir(parents=True)
    (root / DUMP_MEMBER).write_bytes(DUMP_BYTES)
    (root / VAULT_MEMBER).write_bytes(VAULT_BYTES)
    return root


@pytest.fixture
def manifest() -> BackupManifest:
    """A manifest compatible with the test database in every respect."""
    return _base_manifest(
        created_at=NOW,
        migration_head=_installed_head(),
        postgres_version_num=160014,
        vault_included=True,
        vault_file_count=1,
        files={
            DUMP_MEMBER: FileEntry(
                bytes=len(DUMP_BYTES), sha256=hashlib.sha256(DUMP_BYTES).hexdigest()
            ),
            VAULT_MEMBER: FileEntry(
                bytes=len(VAULT_BYTES), sha256=hashlib.sha256(VAULT_BYTES).hexdigest()
            ),
        },
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        embedder=EMBEDDER_NAME,
        vault_path=tmp_path / "vault",
        backup_dir=tmp_path / "backups",
        brain_home=tmp_path / "brain-home",
    )


def _plenty_of_disk(_path: str) -> SimpleNamespace:
    return SimpleNamespace(total=10**12, used=0, free=10**12)


def _no_disk(_path: str) -> SimpleNamespace:
    return SimpleNamespace(total=10**12, used=10**12, free=1)


def _run(
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
    conn: psycopg.Connection,
    *,
    disk_usage: Callable[[str], SimpleNamespace] = _plenty_of_disk,
) -> Preflight:
    return preflight(
        archive_dir,
        manifest,
        cfg,
        FakeEmbedder(dim=EMBEDDING_DIM),
        conn,
        vault_path=cfg.vault_path,
        db_leg=True,
        vault_leg=True,
        disk_usage=disk_usage,
    )


def _codes(result: Preflight) -> set[str]:
    return {issue.code for issue in result.issues}


def _snapshot(conn: psycopg.Connection) -> tuple[int, int]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks)"
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def test_clean_archive_has_no_fatal_issues(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    before = _snapshot(test_db)

    result = _run(archive_dir, manifest, cfg, test_db)

    assert result.blocked is False
    assert _snapshot(test_db) == before


def test_embedder_mismatch_is_fatal(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    before = _snapshot(test_db)

    result = _run(archive_dir, replace(manifest, embedder="voyage"), cfg, test_db)

    assert result.blocked is True
    issue = next(i for i in result.issues if i.code == "embedder")
    assert "cannot be re-projected" in issue.message
    assert "voyage" in issue.remedy
    assert _snapshot(test_db) == before


def test_dim_mismatch_is_fatal(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    """Same backend name, different dim — a repointed model still blocks."""
    before = _snapshot(test_db)

    result = _run(archive_dir, replace(manifest, embedding_dim=1024), cfg, test_db)

    assert result.blocked is True
    assert "dim" in _codes(result)
    assert _snapshot(test_db) == before


def test_newer_migration_head_is_fatal(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    before = _snapshot(test_db)

    result = _run(
        archive_dir, replace(manifest, migration_head="099_future.sql"), cfg, test_db
    )

    assert result.blocked is True
    issue = next(i for i in result.issues if i.code == "migration_head")
    assert issue.fatal is True
    assert "pipx upgrade secondbrain-py" in issue.remedy
    assert _snapshot(test_db) == before


def test_older_migration_head_is_a_note_not_fatal(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    before = _snapshot(test_db)

    result = _run(
        archive_dir, replace(manifest, migration_head="001_init.sql"), cfg, test_db
    )

    assert result.blocked is False
    issue = next(i for i in result.issues if i.code == "migration_head")
    assert issue.fatal is False
    assert "brain init" in issue.remedy
    assert _snapshot(test_db) == before


def test_checksum_mismatch_is_fatal(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    before = _snapshot(test_db)
    (archive_dir / DUMP_MEMBER).write_bytes(b"corrupted-payload")

    result = _run(archive_dir, manifest, cfg, test_db)

    assert result.blocked is True
    assert "checksum" in _codes(result)
    assert _snapshot(test_db) == before


def test_missing_member_is_fatal(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    (archive_dir / DUMP_MEMBER).unlink()

    result = _run(archive_dir, manifest, cfg, test_db)

    assert result.blocked is True
    assert "checksum" in _codes(result)


def test_newer_server_dump_is_fatal(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    """A PostgreSQL 17 dump cannot go into the 16 server."""
    before = _snapshot(test_db)

    result = _run(
        archive_dir, replace(manifest, postgres_version_num=170004), cfg, test_db
    )

    assert result.blocked is True
    assert "server_version" in _codes(result)
    assert _snapshot(test_db) == before


def test_older_server_dump_is_fine(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    result = _run(
        archive_dir, replace(manifest, postgres_version_num=150006), cfg, test_db
    )

    assert "server_version" not in _codes(result)


def test_insufficient_disk_is_fatal(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    before = _snapshot(test_db)

    result = _run(archive_dir, manifest, cfg, test_db, disk_usage=_no_disk)

    assert result.blocked is True
    assert "disk" in _codes(result)
    assert _snapshot(test_db) == before


def test_required_bytes_counts_dump_thrice_and_vault_twice(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    """Staging DB + parked DB + extracted dump coexist at peak (§5.8)."""
    result = _run(archive_dir, manifest, cfg, test_db)

    assert result.required_bytes == len(DUMP_BYTES) * 3 + len(VAULT_BYTES) * 2


def test_reports_live_target_counts(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
    seed_doc: Callable[..., str],
) -> None:
    seed_doc(title="Larkspur review", content="Synthetic body for preflight.")

    result = _run(archive_dir, manifest, cfg, test_db)

    assert result.target_documents == 1
    assert result.target_is_non_empty is True


def test_empty_target_is_reported_empty(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    """The fresh-machine disaster-recovery case: nothing would be destroyed."""
    result = _run(archive_dir, manifest, cfg, test_db)

    assert result.target_documents == 0
    assert result.target_vault_files == 0
    assert result.target_is_non_empty is False


def test_vault_files_are_counted_when_present(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    (cfg.vault_path / "notes").mkdir(parents=True)
    (cfg.vault_path / "notes" / "a.md").write_text("synthetic", encoding="utf-8")

    result = _run(archive_dir, manifest, cfg, test_db)

    assert result.target_vault_files == 1
    assert result.target_is_non_empty is True


def test_db_leg_disabled_skips_database_checks(
    test_db: psycopg.Connection,
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
) -> None:
    """`--vault-only` must not block on an embedder mismatch it will never touch."""
    result = preflight(
        archive_dir,
        replace(manifest, embedder="voyage", embedding_dim=1024),
        cfg,
        FakeEmbedder(dim=EMBEDDING_DIM),
        test_db,
        vault_path=cfg.vault_path,
        db_leg=False,
        vault_leg=True,
        disk_usage=_plenty_of_disk,
    )

    assert "embedder" not in _codes(result)
    assert "dim" not in _codes(result)
    assert result.blocked is False
