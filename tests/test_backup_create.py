"""`create_backup` against the test DB with a faked pg_dump (F3 §5.4)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from brain.backup.archive import sha256_file
from brain.backup.create import create_backup
from brain.backup.manifest import BackupManifest
from brain.config import Config
from brain.errors import BackupError
from tests.backup_fakes import (
    CONTAINER_VERSION,
    STUB_DUMP,
    StubDumpRunner,
    repo_root_guard,  # noqa: F401 — autouse fixture
)
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder

NOW = datetime(2026, 7, 25, 14, 12, 3, tzinfo=UTC)
EMBEDDING_DIM = 4096



@pytest.fixture
def runner() -> StubDumpRunner:
    return StubDumpRunner(responses={"--version": CONTAINER_VERSION})


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A synthetic two-file vault."""
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "index.md").write_text("# Larkspur index\n", encoding="utf-8")
    (root / "notes" / "review.md").write_text(
        "Synthetic quarterly review note.\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def cfg(tmp_path: Path, vault: Path) -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        embedder="qwen3",
        vault_path=vault,
        backup_dir=tmp_path / "backups",
    )


def _backup(cfg: Config, runner: StubDumpRunner, **kwargs: object) -> object:
    """Invoke create_backup with the fixed clock and fake embedder."""
    return create_backup(
        cfg,
        runner=runner,
        clock=lambda: NOW,
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
        **kwargs,  # type: ignore[arg-type]
    )


def _members(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return sorted(tar.getnames())


def _manifest_in(archive: Path) -> dict[str, object]:
    with tarfile.open(archive, "r:gz") as tar:
        handle = tar.extractfile("manifest.json")
        assert handle is not None
        payload: dict[str, object] = json.loads(handle.read().decode("utf-8"))
    return payload


def test_create_backup_produces_readable_archive(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    result = _backup(cfg, runner)

    assert result.archive_path.exists()  # type: ignore[attr-defined]
    archive = result.archive_path  # type: ignore[attr-defined]
    assert _members(archive) == [
        "db",
        "db/second_brain.dump",
        "manifest.json",
        "vault.tar",
    ]
    assert result.archive_bytes == archive.stat().st_size  # type: ignore[attr-defined]
    assert result.archive_sha256 == sha256_file(archive)  # type: ignore[attr-defined]
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    digest = sidecar.read_text(encoding="utf-8").split("  ")[0]
    assert digest == result.archive_sha256  # type: ignore[attr-defined]


def test_manifest_records_member_checksums(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    result = _backup(cfg, runner)

    payload = _manifest_in(result.archive_path)  # type: ignore[attr-defined]
    files = payload["files"]
    assert isinstance(files, dict)
    assert files["db/second_brain.dump"]["sha256"] == hashlib.sha256(
        STUB_DUMP
    ).hexdigest()
    assert files["db/second_brain.dump"]["bytes"] == len(STUB_DUMP)
    assert "vault.tar" in files
    assert BackupManifest.from_dict(payload) == result.manifest  # type: ignore[attr-defined]


def test_no_vault_omits_vault_member(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    result = _backup(cfg, runner, include_vault=False)

    assert "vault.tar" not in _members(result.archive_path)  # type: ignore[attr-defined]
    assert result.manifest.vault_included is False  # type: ignore[attr-defined]
    assert result.manifest.vault_path is None  # type: ignore[attr-defined]
    assert list(result.manifest.files) == ["db/second_brain.dump"]  # type: ignore[attr-defined]


def test_label_appears_in_filename_and_manifest(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    result = _backup(cfg, runner, label="pre-upgrade")

    name = result.archive_path.name  # type: ignore[attr-defined]
    assert name == "brain-backup-20260725-141203-pre-upgrade.tar.gz"
    assert result.manifest.label == "pre-upgrade"  # type: ignore[attr-defined]


def test_unlabelled_archive_has_no_trailing_dash(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    result = _backup(cfg, runner)

    assert result.archive_path.name == "brain-backup-20260725-141203.tar.gz"  # type: ignore[attr-defined]


@pytest.mark.parametrize("label", ["../escape", "has space", "semi;colon", "a" * 41])
def test_bad_label_rejected(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner, label: str
) -> None:
    """A label reaches a filesystem path, so the core validates it too."""
    with pytest.raises(BackupError, match="label"):
        _backup(cfg, runner, label=label)


def test_dump_argv_excludes_age_schemas(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    """AGE's schemas cannot round-trip, so they are excluded by construction (§5.2)."""
    _backup(cfg, runner)

    dump_calls = [call for call in runner.calls if "-Fc" in call]
    assert len(dump_calls) == 1
    argv = dump_calls[0]
    assert "--exclude-schema=ag_catalog" in argv
    assert "--exclude-schema=brain_graph" in argv
    assert "--no-owner" in argv
    assert "--no-privileges" in argv


def test_password_never_reaches_dump_argv(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    _backup(cfg, runner)

    # The test DSN's password is "brain"; it must never be passed inline, and
    # the dump must be routed through an --env-file instead.
    dump_calls = [call for call in runner.calls if "-Fc" in call]
    assert dump_calls
    for call in dump_calls:
        assert "PGPASSWORD" not in " ".join(call)
        assert "--env-file" in call


def test_staging_directory_is_removed_on_success(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    result = _backup(cfg, runner)

    parent = result.archive_path.parent  # type: ignore[attr-defined]
    assert list(parent.glob(".brain-backup-*")) == []


def test_failed_dump_leaves_no_archive_and_no_staging(
    test_db: psycopg.Connection, cfg: Config
) -> None:
    """An aborted dump must not leave a plausible-looking partial backup (§6.2)."""
    failing = StubDumpRunner(
        responses={"--version": CONTAINER_VERSION},
        raises={
            "-Fc": subprocess.CalledProcessError(
                1, ["pg_dump"], stderr="pg_dump: error: connection failed"
            )
        },
    )

    with pytest.raises(BackupError, match="pg_dump"):
        _backup(cfg, failing)

    assert list(cfg.backup_dir.glob("*.tar.gz")) == []
    assert list(cfg.backup_dir.glob(".brain-backup-*")) == []


def test_in_container_dump_is_cleaned_up(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    """The temp dump inside the container is removed even on the happy path."""
    _backup(cfg, runner)

    assert any(
        call[:2] == ["docker", "exec"] and "rm" in call for call in runner.calls
    )


def test_backup_dir_is_created_owner_only(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    result = _backup(cfg, runner)

    parent = result.archive_path.parent  # type: ignore[attr-defined]
    assert (parent.stat().st_mode & 0o777) == 0o700


def test_out_dir_override_is_honoured(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"

    result = _backup(cfg, runner, out_dir=elsewhere)

    assert result.archive_path.parent == elsewhere  # type: ignore[attr-defined]


def test_vault_tar_contains_the_vault_tree(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner, tmp_path: Path
) -> None:
    result = _backup(cfg, runner)

    with tarfile.open(result.archive_path, "r:gz") as outer:  # type: ignore[attr-defined]
        handle = outer.extractfile("vault.tar")
        assert handle is not None
        inner_path = tmp_path / "extracted-vault.tar"
        inner_path.write_bytes(handle.read())
    with tarfile.open(inner_path, "r:") as inner:
        names = sorted(inner.getnames())

    assert "index.md" in names
    assert "notes/review.md" in names
    assert result.manifest.vault_file_count == 2  # type: ignore[attr-defined]


def test_missing_vault_directory_is_recorded_not_fatal(
    test_db: psycopg.Connection, tmp_path: Path, runner: StubDumpRunner
) -> None:
    """A brain with no vault yet still backs up its database."""
    cfg = Config(
        database_url=TEST_DATABASE_URL,
        embedder="qwen3",
        vault_path=tmp_path / "no-such-vault",
        backup_dir=tmp_path / "backups",
    )

    result = _backup(cfg, runner)

    assert result.manifest.vault_included is False  # type: ignore[attr-defined]
    assert "vault.tar" not in _members(result.archive_path)  # type: ignore[attr-defined]


def test_on_step_reports_progress(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    steps: list[str] = []

    _backup(cfg, runner, on_step=steps.append)

    joined = " ".join(steps)
    assert "dumped" in joined
    assert "vault" in joined
    assert "manifest" in joined
