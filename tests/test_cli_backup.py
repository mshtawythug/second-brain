"""CLI-level tests for `brain backup` / `brain restore` (F3 §8).

No real pg_dump ever runs: `create_backup` is invoked through a wrapper that
injects the recording runner, a fixed clock, and the fake embedder.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.backup import create_backup
from brain.cli import app
from brain.cli_backup import backup_doctor_checks
from brain.config import Config
from tests.backup_fakes import (
    CONTAINER_VERSION,
    StubDumpRunner,
    repo_root_guard,  # noqa: F401 — autouse fixture
)
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder
from tests.test_backup_manifest import EMBEDDER_NAME, EMBEDDING_DIM

NOW = datetime(2026, 7, 25, 14, 12, 3, tzinfo=UTC)

#: Every key `brain backup --json` promises, with its documented type (§3).
EXPECTED_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "archive_path": str,
    "archive_bytes": int,
    "archive_sha256": str,
    "schema": int,
    "created_at": str,
    "label": str,
    "brain_version": str,
    "postgres_version": str,
    "postgres_version_num": int,
    "pg_dump_version": str,
    "pg_dump_source": str,
    "container_name": (str, type(None)),
    "database_name": str,
    "dump_format": str,
    "dump_excluded_schemas": list,
    "migration_head": str,
    "migration_count": int,
    "embedder": str,
    "embedding_dim": int,
    "embedding_column_type": str,
    "embedding_not_null": bool,
    "embedding_has_index": bool,
    "counts": dict,
    "graph_entities": (int, type(None)),
    "vault_included": bool,
    "vault_path": (str, type(None)),
    "vault_file_count": (int, type(None)),
    "files": dict,
}


def test_backup_command_is_registered() -> None:
    """`brain backup --help` resolves — the red-first test proving the gap.

    Before F3 this exited 2 with ``No such command 'backup'``.
    """
    result = CliRunner().invoke(app, ["backup", "--help"])

    assert result.exit_code == 0, result.output


def test_restore_command_is_registered() -> None:
    """`brain restore --help` resolves."""
    result = CliRunner().invoke(app, ["restore", "--help"])

    assert result.exit_code == 0, result.output


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("Synthetic vault note.\n", encoding="utf-8")
    return Config(
        database_url=TEST_DATABASE_URL,
        embedder=EMBEDDER_NAME,
        vault_path=vault,
        backup_dir=tmp_path / "backups",
        brain_home=tmp_path / "brain-home",
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    """Point `brain backup` at the test config with a faked pg_dump."""
    monkeypatch.setenv("DATABASE_URL", cfg.database_url)
    monkeypatch.setattr(Config, "load", classmethod(lambda _cls: cfg))

    def _fake_backup(config: Config, **kwargs: Any) -> Any:
        kwargs.setdefault(
            "runner", StubDumpRunner(responses={"--version": CONTAINER_VERSION})
        )
        kwargs.setdefault("clock", lambda: NOW)
        kwargs.setdefault("embedder", FakeEmbedder(dim=EMBEDDING_DIM))
        return create_backup(config, **kwargs)

    monkeypatch.setattr("brain.cli_backup.create_backup", _fake_backup)


def test_backup_json_has_every_documented_key_with_the_documented_type(
    test_db: psycopg.Connection, wired: None
) -> None:
    result = CliRunner().invoke(app, ["backup", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for key, expected in EXPECTED_JSON_TYPES.items():
        assert key in payload, f"missing key: {key}"
        assert isinstance(payload[key], expected), (
            f"{key}: expected {expected}, got {type(payload[key])}"
        )
    assert payload["schema"] == 1
    assert payload["dump_excluded_schemas"] == ["ag_catalog", "brain_graph"]
    assert set(payload["counts"]) == {"documents", "chunks", "sources"}


def test_backup_human_output_names_the_archive_and_checksum(
    test_db: psycopg.Connection, wired: None, cfg: Config
) -> None:
    result = CliRunner().invoke(app, ["backup"])

    assert result.exit_code == 0, result.output
    assert "brain-backup-20260725-141203.tar.gz" in result.output
    assert "sha256" in result.output
    assert "Restore with:" in result.output


def test_backup_no_vault_flag_is_reflected_in_the_manifest(
    test_db: psycopg.Connection, wired: None
) -> None:
    result = CliRunner().invoke(app, ["backup", "--no-vault", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["vault_included"] is False
    assert payload["vault_path"] is None
    assert "vault.tar" not in payload["files"]


def test_backup_label_reaches_the_filename(
    test_db: psycopg.Connection, wired: None
) -> None:
    result = CliRunner().invoke(app, ["backup", "--label", "pre-upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["label"] == "pre-upgrade"
    assert payload["archive_path"].endswith(
        "brain-backup-20260725-141203-pre-upgrade.tar.gz"
    )


@pytest.mark.parametrize("label", ["../escape", "has space", "semi;colon"])
def test_bad_label_rejected(
    test_db: psycopg.Connection, wired: None, label: str
) -> None:
    """A bad --label is a BadParameter (exit 2), not a crash."""
    result = CliRunner().invoke(app, ["backup", "--label", label])

    assert result.exit_code == 2


def test_out_pointing_at_a_file_is_bad_parameter(
    test_db: psycopg.Connection, wired: None, tmp_path: Path
) -> None:
    target = tmp_path / "not-a-dir"
    target.write_text("", encoding="utf-8")

    result = CliRunner().invoke(app, ["backup", "--out", str(target)])

    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# brain doctor — the `last backup` check
# ---------------------------------------------------------------------------


def test_doctor_check_warns_when_no_backup_exists(cfg: Config) -> None:
    checks = backup_doctor_checks(cfg)

    assert len(checks) == 1
    check = checks[0]
    assert check.check == "last backup"
    assert check.status == "warn"
    assert check.remedy == "brain backup"
    assert "no backup found" in check.detail
    assert "WARN" in check.lines[0].text


def test_doctor_check_is_ok_after_a_backup(
    test_db: psycopg.Connection, cfg: Config
) -> None:
    create_backup(
        cfg,
        runner=StubDumpRunner(responses={"--version": CONTAINER_VERSION}),
        clock=lambda: datetime.now(UTC),
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
    )

    checks = backup_doctor_checks(cfg)

    assert len(checks) == 1
    check = checks[0]
    assert check.check == "last backup"
    assert check.status == "ok"
    assert check.remedy is None
    assert "MiB" in check.detail
    assert "OK" in check.lines[0].text


def test_doctor_check_warns_when_the_newest_backup_is_stale(
    test_db: psycopg.Connection, cfg: Config
) -> None:
    """A backup from long ago is worse than no signal — say so (Open Question 4)."""
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    create_backup(
        cfg,
        runner=StubDumpRunner(responses={"--version": CONTAINER_VERSION}),
        clock=lambda: long_ago,
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
    )

    checks = backup_doctor_checks(cfg)

    assert checks[0].status == "warn"
    assert "days old" in checks[0].detail


def test_doctor_check_never_fails(test_db: psycopg.Connection, cfg: Config) -> None:
    """It is a nudge, not a gate — it must never flip doctor's exit code."""
    assert backup_doctor_checks(cfg)[0].status in {"ok", "warn"}
