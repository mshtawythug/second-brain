"""`create_backup` against the test DB with a faked pg_dump (F3 §5.4)."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.backup.archive import sha256_file
from brain.backup.create import create_backup, tar_vault
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


# ---------------------------------------------------------------------------
# `<vault>/.quartz/` is left out (docs/plans/2026-10-22-backup-skip-quartz.md)
# ---------------------------------------------------------------------------

#: The regular files the fixture puts OUTSIDE the Quartz workspace: the two base
#: vault files, the user's Obsidian config (ruling B1), and a nested `.quartz`
#: the project never wrote (ruling B2).
_FILES_KEPT = {
    "index.md",
    "notes/review.md",
    ".obsidian/app.json",
    "notes/.quartz/keep.md",
}

#: The exact report line for a skipped workspace (ruling B7).
_QUARTZ_SKIP_STEP = (
    "left out .quartz (leftover Quartz wiki output, not backed up; safe to delete by hand)"
)


@pytest.fixture
def quartz_vault(vault: Path) -> Path:
    """The two-file vault plus a leftover Quartz workspace and user dot-config."""
    quartz = vault / ".quartz"
    for relative in (
        "node_modules/synthetic-pkg/index.js",
        "builds/20260501-000000-abc123/index.html",
        ".cache/fastpath/state.json",
        "docs/features/upcoming.md",
    ):
        target = quartz / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic build output\n", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "app.json").write_text("{}\n", encoding="utf-8")
    (vault / "notes" / ".quartz").mkdir()
    (vault / "notes" / ".quartz" / "keep.md").write_text(
        "A synthetic user note in a nested dot-dir.\n", encoding="utf-8"
    )
    return vault


def _inner_vault_tar(archive: Path, tmp_path: Path) -> tarfile.TarFile:
    """Open the inner `vault.tar` of a backup archive (the caller closes it)."""
    with tarfile.open(archive, "r:gz") as outer:
        handle = outer.extractfile("vault.tar")
        assert handle is not None
        inner_path = tmp_path / "inner-vault.tar"
        inner_path.write_bytes(handle.read())
    return tarfile.open(inner_path, "r:")


def _tar_names(vault_path: Path, tmp_path: Path) -> list[str]:
    """Member names of a `vault.tar` written straight from ``vault_path``."""
    destination = tmp_path / "direct-vault.tar"
    tar_vault(vault_path, destination)
    with tarfile.open(destination, "r:") as tar:
        return tar.getnames()


def test_vault_tar_leaves_out_the_quartz_workspace(quartz_vault: Path, tmp_path: Path) -> None:
    names = _tar_names(quartz_vault, tmp_path)

    leaked = [name for name in names if name == ".quartz" or name.startswith(".quartz/")]
    assert leaked == []


def test_vault_tar_keeps_other_dotted_user_config(quartz_vault: Path, tmp_path: Path) -> None:
    """`.obsidian/` is the user's editor config, so a backup must carry it (B1)."""
    names = _tar_names(quartz_vault, tmp_path)

    assert ".obsidian/app.json" in names


def test_vault_tar_keeps_a_nested_quartz_directory(quartz_vault: Path, tmp_path: Path) -> None:
    """Only the TOP-LEVEL workspace is the project's; a nested one is user data (B2)."""
    names = _tar_names(quartz_vault, tmp_path)

    assert "notes/.quartz/keep.md" in names


def _make_quartz_file(vault_path: Path, tmp_path: Path) -> None:
    (vault_path / ".quartz").write_text("synthetic stray file\n", encoding="utf-8")


def _make_quartz_symlink(vault_path: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-quartz-workspace"
    outside.mkdir()
    (outside / "index.js").write_text("synthetic build output\n", encoding="utf-8")
    (vault_path / ".quartz").symlink_to(outside, target_is_directory=True)


@pytest.mark.parametrize(
    "make_quartz",
    [_make_quartz_file, _make_quartz_symlink],
    ids=["regular-file", "symlink-to-outside-dir"],
)
def test_top_level_quartz_is_skipped_whatever_its_type(
    vault: Path, tmp_path: Path, make_quartz: Callable[[Path, Path], None]
) -> None:
    """A top-level `.quartz` that is a file or a symlink is skipped too (B3).

    `os.walk` lists a regular file under `filenames` and a symlink to a
    directory under `dirnames`, so each case reaches a different filter.
    """
    make_quartz(vault, tmp_path)
    destination = tmp_path / "typed-vault.tar"

    result = tar_vault(vault, destination)

    with tarfile.open(destination, "r:") as tar:
        names = tar.getnames()
    assert [name for name in names if name.split("/")[0] == ".quartz"] == []
    assert result.skipped == (".quartz",)


@pytest.fixture
def deep_vault(tmp_path: Path) -> Path:
    """`a.md` plus a two-level subtree, `notes/deep/b.md` (the review's repro)."""
    root = tmp_path / "deep-vault"
    (root / "notes" / "deep").mkdir(parents=True)
    (root / "a.md").write_text("Synthetic top note.\n", encoding="utf-8")
    (root / "notes" / "deep" / "b.md").write_text("Synthetic deep note.\n", encoding="utf-8")
    return root


def _scandir_failing_at(monkeypatch: pytest.MonkeyPatch, target: Path, error: OSError) -> None:
    """Make ``os.scandir`` raise ``error`` for ``target`` only.

    `monkeypatch` is a standard test double with automatic cleanup (rule 13).
    """
    real_scandir = os.scandir

    def _scandir(path: Any = ".") -> Any:
        if Path(path) == target:
            raise error
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _scandir)


def test_unlistable_directory_other_than_permission_fails_the_backup(
    deep_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EIO while listing a subtree raises; it never silently drops it (B10).

    Python 3.11's `rglob`, which `tar_vault` used to walk with, let every
    listing error but `PermissionError` propagate. `os.walk`'s default
    `onerror=None` swallows them all, which would back up a vault minus
    `notes/` and still exit 0.
    """
    _scandir_failing_at(
        monkeypatch,
        deep_vault / "notes",
        OSError(errno.EIO, "Input/output error", str(deep_vault / "notes")),
    )

    with pytest.raises(OSError) as raised:
        tar_vault(deep_vault, tmp_path / "eio.tar")

    assert raised.value.errno == errno.EIO


def test_permission_denied_directory_is_still_passed_over(
    deep_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory the user cannot list is passed over, as `rglob` did (B10)."""
    _scandir_failing_at(
        monkeypatch,
        deep_vault / "notes",
        PermissionError(errno.EACCES, "Permission denied", str(deep_vault / "notes")),
    )
    destination = tmp_path / "eacces.tar"

    try:
        result = tar_vault(deep_vault, destination)
    except PermissionError as exc:
        pytest.fail(f"a PermissionError subtree must be passed over, not raised: {exc}")

    with tarfile.open(destination, "r:") as tar:
        names = tar.getnames()
    assert names == ["a.md", "notes"]
    assert result.file_count == 1


def test_quartz_workspace_is_not_walked(
    quartz_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree is pruned, never listed and then filtered (B4).

    A membership test cannot tell a pruned walk from one that lists every
    `node_modules` entry and drops it afterwards. Recording every directory the
    walk opens can. `monkeypatch` is a standard test double with automatic
    cleanup (CLAUDE.md rule 13).
    """
    opened: list[Path] = []
    real_scandir = os.scandir

    def _recording_scandir(path: Any = ".") -> Any:
        opened.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _recording_scandir)

    tar_vault(quartz_vault, tmp_path / "walked.tar")

    quartz = quartz_vault / ".quartz"
    entered = [path for path in opened if path == quartz or quartz in path.parents]
    assert entered == []
    # The probe really observes the walk: a listing of `notes/` was recorded.
    assert quartz_vault / "notes" in opened


def test_vault_file_count_excludes_the_quartz_workspace(
    test_db: psycopg.Connection,
    cfg: Config,
    quartz_vault: Path,
    runner: StubDumpRunner,
    tmp_path: Path,
) -> None:
    """The manifest counts exactly the regular files `vault.tar` holds (B6)."""
    result = _backup(cfg, runner)

    archive = result.archive_path  # type: ignore[attr-defined]
    with _inner_vault_tar(archive, tmp_path) as inner:
        regular = {member.name for member in inner.getmembers() if member.isfile()}
    assert regular == _FILES_KEPT
    assert result.manifest.vault_file_count == len(_FILES_KEPT)  # type: ignore[attr-defined]


def test_quartz_workspace_skip_is_reported(
    test_db: psycopg.Connection,
    cfg: Config,
    quartz_vault: Path,
    runner: StubDumpRunner,
) -> None:
    steps: list[str] = []

    _backup(cfg, runner, on_step=steps.append)

    assert [step for step in steps if ".quartz" in step] == [_QUARTZ_SKIP_STEP]


def test_no_skip_is_reported_without_a_quartz_workspace(
    test_db: psycopg.Connection, cfg: Config, runner: StubDumpRunner
) -> None:
    steps: list[str] = []

    _backup(cfg, runner, on_step=steps.append)

    assert _QUARTZ_SKIP_STEP not in steps
    assert [step for step in steps if ".quartz" in step] == []
