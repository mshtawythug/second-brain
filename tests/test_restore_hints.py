"""`brain restore` QA fixes that need no database (F22 hint, F27 staging cleanup).

F22: the "drop it when satisfied" line named the production container whatever
server the restore ran against. :func:`brain.cli_backup.drop_database_command`
now builds it from what the restore actually used, and never prints a secret.

F27: a declined or failed restore left ``$BRAIN_HOME/run/restore-<ts>/`` — a
plaintext copy of the dump and vault — behind.
:func:`brain.backup.restore.discard_prepared_restore` removes it, and refuses to
remove anything that is not exactly that directory.

Every DSN, host, user and container name here is synthetic.
"""
from __future__ import annotations

import io
import json
import shlex
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.backup.archive import MANIFEST_MEMBER
from brain.backup.restore import (
    PreparedRestore,
    discard_prepared_restore,
    prepare_restore,
    restore_backup,
)
from brain.cli_backup import drop_database_command
from brain.config import Config
from brain.errors import BackupError
from tests.backup_fakes import repo_root_guard  # noqa: F401 — autouse fixture
from tests.test_backup_manifest import _manifest

pytestmark = pytest.mark.nodb

PARKED = "synthetic_brain_replaced_20260725_181500"
SECRET = "Synthetic-Secret-7f3e"
SSL_SECRET = "Synthetic-SslKeyPass-91c2"
NOW = datetime(2026, 7, 25, 18, 15, 0, tzinfo=UTC)


def _cfg(tmp_path: Path) -> Config:
    return Config(
        database_url="postgresql://synthetic:synthetic@localhost:5999/synthetic_brain",
        embedder="qwen3",
        vault_path=tmp_path / "vault",
        backup_dir=tmp_path / "backups",
        brain_home=tmp_path / "brain-home",
    )


# ---------------------------------------------------------------------------
# F22 — drop_database_command
# ---------------------------------------------------------------------------


def test_container_leg_names_the_container_the_restore_used() -> None:
    command = drop_database_command(
        PARKED,
        database_url="postgresql://qa_user@localhost:5999/synthetic_brain",
        container="synthetic-qa-postgres",
    )

    assert shlex.split(command) == [
        "docker", "exec", "synthetic-qa-postgres",
        "psql", "-U", "qa_user", "-d", "postgres",
        "-c", f"DROP DATABASE {PARKED}",
    ]
    assert "second-brain-postgres" not in command


def test_host_leg_is_psql_against_the_dsn_with_no_docker() -> None:
    command = drop_database_command(
        PARKED,
        database_url="postgresql://qa_user@db.synthetic.test:5999/synthetic_brain",
        container=None,
    )

    assert shlex.split(command) == [
        "psql", "-h", "db.synthetic.test", "-p", "5999", "-U", "qa_user",
        "-d", "postgres", "-c", f"DROP DATABASE {PARKED}",
    ]
    assert "docker" not in command


@pytest.mark.parametrize(
    "database_url",
    [
        f"postgresql://qa_user:{SECRET}@localhost:5999/synthetic_brain"
        f"?sslpassword={SSL_SECRET}&sslmode=require",
        f"host=localhost port=5999 user=qa_user password={SECRET} "
        f"sslpassword={SSL_SECRET} dbname=synthetic_brain",
    ],
    ids=["url", "keyword"],
)
@pytest.mark.parametrize("container", [None, "synthetic-qa-postgres"])
def test_no_credential_ever_reaches_the_hint(
    database_url: str, container: str | None
) -> None:
    command = drop_database_command(
        PARKED, database_url=database_url, container=container
    )

    assert SECRET not in command
    assert SSL_SECRET not in command
    assert "password" not in command
    assert "postgresql://" not in command


def test_socket_dsn_omits_host_and_port() -> None:
    command = drop_database_command(
        PARKED, database_url="dbname=synthetic_brain user=qa_user", container=None
    )

    assert shlex.split(command) == [
        "psql", "-U", "qa_user", "-d", "postgres", "-c", f"DROP DATABASE {PARKED}",
    ]


@pytest.mark.parametrize(
    ("database_url", "container", "hostile"),
    [
        ("host=localhost port=5999 user='qa; rm -rf ~' dbname=synthetic_brain",
         None, "qa; rm -rf ~"),
        ("host='/tmp/pg sock $(id)' user=qa_user dbname=synthetic_brain",
         None, "/tmp/pg sock $(id)"),
        ("host=localhost port=5999 user=qa_user dbname=synthetic_brain",
         "synthetic`id`-postgres", "synthetic`id`-postgres"),
    ],
    ids=["user", "host", "container"],
)
def test_hostile_values_are_shell_quoted(
    database_url: str, container: str | None, hostile: str
) -> None:
    """A metacharacter-laden value must come back as ONE literal shell word."""
    command = drop_database_command(
        PARKED, database_url=database_url, container=container
    )

    assert hostile in shlex.split(command)
    assert shlex.quote(hostile) in command


# ---------------------------------------------------------------------------
# F27 — discard_prepared_restore
# ---------------------------------------------------------------------------


def _write_archive(tmp_path: Path, *, hostile: bool) -> Path:
    """A tar.gz with a parseable manifest and, optionally, an escaping member."""
    archive = tmp_path / "in" / "brain-backup-20260725-181500.tar.gz"
    archive.parent.mkdir(parents=True)
    manifest = json.dumps(_manifest().to_dict()).encode("utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(MANIFEST_MEMBER)
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        if hostile:
            payload = b"synthetic escape payload"
            escape = tarfile.TarInfo("../escape.txt")
            escape.size = len(payload)
            tar.addfile(escape, io.BytesIO(payload))
    return archive


def _staging_dirs(cfg: Config) -> list[Path]:
    return sorted((cfg.brain_home / "run").glob("restore-*"))


def test_discard_removes_a_real_prepare_restore_dir(tmp_path: Path) -> None:
    """Built by ``prepare_restore`` itself, so a TIMESTAMP_FORMAT drift fails here."""
    cfg = _cfg(tmp_path)
    state = prepare_restore(_write_archive(tmp_path, hostile=False), cfg, clock=lambda: NOW)
    assert state.staging_dir.is_dir()

    discard_prepared_restore(state, cfg)

    assert not state.staging_dir.exists()
    assert _staging_dirs(cfg) == []


def _sentinel_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "keep.txt").write_text("synthetic sentinel", encoding="utf-8")
    return path


def test_discard_leaves_a_lookalike_outside_the_run_dir(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    outside = _sentinel_dir(tmp_path / "elsewhere" / "restore-20260725-181500")

    discard_prepared_restore(
        PreparedRestore(staging_dir=outside, manifest=_manifest(), sidecar_ok=None), cfg
    )

    assert (outside / "keep.txt").is_file()


@pytest.mark.parametrize(
    "name", ["restore-latest", "restore-20260725-181500-extra", "backup-20260725-181500", "."]
)
def test_discard_leaves_a_non_matching_name_under_run(tmp_path: Path, name: str) -> None:
    cfg = _cfg(tmp_path)
    run_dir = cfg.brain_home / "run"
    target = run_dir / name
    if name == ".":
        _sentinel_dir(run_dir)
    else:
        _sentinel_dir(target)

    discard_prepared_restore(
        PreparedRestore(staging_dir=target, manifest=_manifest(), sidecar_ok=None), cfg
    )

    assert (target / "keep.txt").is_file()


def test_discard_leaves_a_symlink_and_its_target(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    victim = _sentinel_dir(tmp_path / "victim")
    link = cfg.brain_home / "run" / "restore-20260725-181500"
    link.parent.mkdir(parents=True)
    link.symlink_to(victim, target_is_directory=True)

    discard_prepared_restore(
        PreparedRestore(staging_dir=link, manifest=_manifest(), sidecar_ok=None), cfg
    )

    assert link.is_symlink()
    assert (victim / "keep.txt").is_file()


def test_failed_extraction_leaves_no_staging_dir(tmp_path: Path) -> None:
    """F27: a hostile member is refused AFTER ``extract_archive`` made the dir."""
    cfg = _cfg(tmp_path)
    archive = _write_archive(tmp_path, hostile=True)

    with pytest.raises(BackupError):
        prepare_restore(archive, cfg, clock=lambda: NOW)

    assert _staging_dirs(cfg) == []


def test_failed_restore_backup_removes_its_own_unpack(tmp_path: Path) -> None:
    """F27, direct API: ``restore_backup`` cleans up even with no CLI around it.

    A vault-only restore whose safety backup fails: nothing reaches a database,
    and the unpack ``restore_backup`` made itself must still be gone.
    """
    cfg = _cfg(tmp_path)
    archive = _write_archive(tmp_path, hostile=False)

    def _failing_pre_backup() -> Path:
        assert _staging_dirs(cfg), "the archive should be unpacked by now"
        raise BackupError("synthetic pre-restore backup failure")

    with pytest.raises(BackupError, match="synthetic pre-restore"):
        restore_backup(
            archive,
            cfg,
            db_leg=False,
            vault_leg=True,
            clock=lambda: NOW,
            pre_backup=_failing_pre_backup,
        )

    assert _staging_dirs(cfg) == []
