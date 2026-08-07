"""`brain backup` — dump the database, tar the vault, seal it with checksums."""
from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from .._compose import postgres_container_name
from ..config import Config
from ..db import connect
from ..embeddings import make_embedder
from ..errors import BackupError
from ..ingest import Embedder
from .archive import (
    DUMP_MEMBER,
    MANIFEST_MEMBER,
    VAULT_MEMBER,
    sha256_file,
    write_archive,
    write_sidecar,
)
from .manifest import EXCLUDED_SCHEMAS, BackupManifest, FileEntry, collect_manifest
from .pgtool import (
    DUMP_TIMEOUT_S,
    PROBE_TIMEOUT_S,
    CommandRunner,
    PgToolPlan,
    SubprocessRunner,
    password_env_file,
    resolve_pg_tool,
    server_version,
)

#: A label is folded into a filename, so it is restricted to characters that
#: cannot traverse a path or need quoting.
_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

#: `YYYYMMDD-HHMMSS`, the archive filename's timestamp.
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

#: Backups hold the whole corpus in plaintext (§7), so the directory is
#: owner-only — matching the 0600 the archive itself gets.
_BACKUP_DIR_MODE = 0o700


@dataclass(frozen=True)
class BackupResult:
    """What `brain backup` produced."""

    archive_path: Path
    archive_bytes: int
    archive_sha256: str
    manifest: BackupManifest


def validate_label(label: str) -> str:
    """Return ``label`` unchanged, or raise if it cannot go into a filename."""
    if label and not _LABEL_RE.match(label):
        raise BackupError(
            f"invalid label {label!r}: use 1-40 characters from letters, digits, "
            "'-' and '_' only (the label becomes part of the archive filename)"
        )
    return label


def archive_name(now: datetime, label: str) -> str:
    """``brain-backup-<YYYYMMDD-HHMMSS>[-label].tar.gz``."""
    stamp = now.strftime(TIMESTAMP_FORMAT)
    suffix = f"-{label}" if label else ""
    return f"brain-backup-{stamp}{suffix}.tar.gz"


def dsn_parts(database_url: str) -> dict[str, str]:
    """Parse a DSN into psycopg's parameter dict — never split by hand."""
    parsed = psycopg.conninfo.conninfo_to_dict(database_url)
    return {key: str(value) for key, value in parsed.items() if value is not None}


def run_tool(
    runner: CommandRunner,
    argv: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    what: str,
) -> subprocess.CompletedProcess[str]:
    """Run one external command, translating every failure into ``BackupError``.

    Mirrors the boundary translation in ``demo._run_docker``: a missing binary,
    a dead daemon, a non-zero exit, or a timeout each surface as one actionable
    error carrying the tool's own stderr — never a raw traceback escaping the
    CLI's ``except BrainError``.
    """
    try:
        return runner.run(argv, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise BackupError(
            f"{what} failed: {argv[0]!r} not found. Is Docker installed and on PATH?"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or "<no stderr captured>"
        raise BackupError(f"{what} failed (exit {exc.returncode}):\n{detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"{what} timed out after {exc.timeout:.0f}s.") from exc


def _dump_database(
    plan: PgToolPlan,
    parts: dict[str, str],
    destination: Path,
    runner: CommandRunner,
) -> None:
    """Run ``pg_dump -Fc`` and land the result at ``destination`` on the host.

    Container leg: dump to a temp path *inside* the container, ``docker cp`` it
    out, then remove the in-container file in a ``finally``. Streaming to
    stdout was rejected — ``pg_restore`` of a custom-format archive degrades on
    non-seekable stdin, and ``docker cp`` keeps dump and restore symmetric.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    dump_args = [
        "-U",
        parts.get("user", "brain"),
        "-d",
        parts.get("dbname", ""),
        "-Fc",
        "--no-owner",
        "--no-privileges",
        *[f"--exclude-schema={schema}" for schema in EXCLUDED_SCHEMAS],
    ]
    password = parts.get("password")

    if plan.source != "container":
        argv = plan.argv(
            *dump_args,
            "-h",
            parts.get("host", "localhost"),
            "-p",
            parts.get("port", "5432"),
            "-f",
            str(destination),
        )
        run_tool(
            runner, argv, timeout=DUMP_TIMEOUT_S, env=plan.env(password), what="pg_dump"
        )
        return

    remote = f"/tmp/brain-backup-{uuid.uuid4().hex}.dump"  # noqa: S108 — in-container
    try:
        with password_env_file(password) as env_file:
            run_tool(
                runner,
                plan.argv(*dump_args, "-f", remote, env_file=env_file),
                timeout=DUMP_TIMEOUT_S,
                what="pg_dump",
            )
        run_tool(
            runner,
            ["docker", "cp", f"{plan.container}:{remote}", str(destination)],
            timeout=DUMP_TIMEOUT_S,
            what="docker cp of the dump",
        )
    finally:
        # Best-effort: a leftover temp file inside the container is untidy, not
        # dangerous, and must never mask the original failure.
        with contextlib.suppress(
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            runner.run(
                ["docker", "exec", str(plan.container), "rm", "-f", remote],
                timeout=PROBE_TIMEOUT_S,
            )


def tar_vault(vault_path: Path, destination: Path) -> int:
    """Write an uncompressed ``vault.tar`` and return the file count.

    Uncompressed on purpose: exactly two members then need checksums, and the
    outer gzip compresses it anyway. Per-file checksums over 1,200 vault files
    would bloat the manifest for no gain.
    """
    file_count = 0
    with tarfile.open(destination, "w") as tar:
        for path in sorted(vault_path.rglob("*")):
            name = path.relative_to(vault_path).as_posix()
            tar.add(path, arcname=name, recursive=False)
            if path.is_file():
                file_count += 1
    return file_count


def create_backup(
    cfg: Config,
    *,
    out_dir: Path | None = None,
    include_vault: bool = True,
    label: str = "",
    runner: CommandRunner | None = None,
    clock: Callable[[], datetime] | None = None,
    on_step: Callable[[str], None] | None = None,
    embedder: Embedder | None = None,
) -> BackupResult:
    """Produce one timestamped, checksummed archive of the database and vault.

    Everything is assembled inside ``<out>/.brain-backup-<ts>.partial/`` and
    only ``os.replace``d into place at the very end, so an interrupted run
    leaves no ``.tar.gz`` and no sidecar — a partial can never be mistaken for
    a usable backup.

    ``runner`` / ``clock`` / ``on_step`` / ``embedder`` are the
    dependency-injection seams; production passes ``None`` and gets the real
    implementations. ``embedder`` is injectable beyond the design sketch so the
    suite can supply a fake without a live Ollama — only ``.dim`` is read.
    """
    validate_label(label)
    active_runner = runner if runner is not None else SubprocessRunner()
    now = clock() if clock is not None else datetime.now(UTC)
    report = on_step if on_step is not None else (lambda _message: None)

    destination_dir = out_dir if out_dir is not None else cfg.backup_dir
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_dir.chmod(_BACKUP_DIR_MODE)

    stamp = now.strftime(TIMESTAMP_FORMAT)
    staging = destination_dir / f".brain-backup-{stamp}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    try:
        with connect(cfg.database_url) as conn:
            _display, version_num = server_version(conn)
            plan = resolve_pg_tool(
                "pg_dump",
                server_major=version_num // 10000,
                container=postgres_container_name(),
                runner=active_runner,
                database_url=cfg.database_url,
            )

            dump_path = staging / DUMP_MEMBER
            _dump_database(plan, dsn_parts(cfg.database_url), dump_path, active_runner)

            vault_present = include_vault and cfg.vault_path.is_dir()
            vault_file_count: int | None = None
            if vault_present:
                vault_file_count = tar_vault(cfg.vault_path, staging / VAULT_MEMBER)

            active_embedder = embedder if embedder is not None else make_embedder(cfg)
            manifest = collect_manifest(
                conn,
                cfg,
                active_embedder,
                label=label,
                pg_dump_plan=plan,
                vault_included=vault_present,
                vault_file_count=vault_file_count,
                now=now,
            )

        counts = manifest.counts
        report(
            f"dumped {counts['documents']} documents / {counts['chunks']} chunks "
            f"/ {counts['sources']} sources"
        )
        report(
            f"archived vault ({vault_file_count} files)"
            if vault_present
            else "vault skipped (not present in this backup)"
        )

        files = {
            DUMP_MEMBER: FileEntry(
                bytes=dump_path.stat().st_size, sha256=sha256_file(dump_path)
            )
        }
        if vault_present:
            vault_tar = staging / VAULT_MEMBER
            files[VAULT_MEMBER] = FileEntry(
                bytes=vault_tar.stat().st_size, sha256=sha256_file(vault_tar)
            )
        manifest = manifest.with_files(files)

        (staging / MANIFEST_MEMBER).write_text(
            json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
        )
        report(
            f"wrote manifest (head {manifest.migration_head}, "
            f"{manifest.embedder}/{manifest.embedding_dim})"
        )

        archive_path = destination_dir / archive_name(now, label)
        write_archive(staging, archive_path)
        write_sidecar(archive_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return BackupResult(
        archive_path=archive_path,
        archive_bytes=archive_path.stat().st_size,
        archive_sha256=sha256_file(archive_path),
        manifest=manifest,
    )
