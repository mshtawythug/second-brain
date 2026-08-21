"""`brain restore` — preflight, gate, staging-database swap, vault swap."""
from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from .._compose import postgres_container_name
from ..config import Config
from ..db import (
    bootstrap_age,
    connect,
    ensure_embedding_column,
    migrations_dir,
    run_migrations,
)
from ..embeddings import make_embedder
from ..errors import BackupError, RestoreAborted, RestoreIncompatible
from ..ingest import Embedder
from ..queries import analyze_tables, summary_counts
from .archive import (
    DUMP_MEMBER,
    VAULT_MEMBER,
    _safe_members,
    extract_archive,
    read_manifest,
    sha256_file,
    verify_sidecar,
)
from .create import TIMESTAMP_FORMAT, create_backup, dsn_parts, run_tool
from .manifest import BackupManifest
from .pgtool import (
    RESTORE_TIMEOUT_S,
    CommandRunner,
    PgToolPlan,
    SubprocessRunner,
    password_env_file,
    resolve_pg_tool,
    server_version,
)

#: The phrase a user must type when the target is not empty. No flag can supply
#: it — see §5.9 and `uninstall.py:103-109`, which exists for the same reason.
RESTORE_PHRASE = "restore and overwrite my brain"

#: Generated database names are additionally regex-checked before being wrapped
#: in `sql.Identifier` — belt and braces, mirroring how `brain analyze`
#: validates against `list_public_tables` before quoting.
_DB_NAME_RE = re.compile(r"^[a-z0-9_]+$")

#: Postgres truncates identifiers at ``NAMEDATALEN - 1`` = 63 BYTES, and does
#: it SILENTLY: the over-long name is accepted, cut server-side, and every
#: later reference to the name Python still holds fails with
#: `database "..." does not exist` — pointing at nothing.
PG_IDENTIFIER_MAX_BYTES = 63

#: The longest name a restore DERIVES from the live database name.
#: :func:`_restore_database` builds both ``{live}_restore_{suffix}`` and
#: ``{live}_replaced_{suffix}``, where ``suffix`` is a ``TIMESTAMP_FORMAT``
#: stamp with its dash swapped for an underscore (same length either way).
#: ``_replaced_`` is the binding one. Spelled as an expression over the real
#: format rather than a literal, so it re-derives if either half moves.
RESTORE_DERIVED_SUFFIX_BYTES = len("_replaced_") + len(
    datetime(2026, 1, 1, tzinfo=UTC).strftime(TIMESTAMP_FORMAT)
)

#: Longest live database name `brain restore` can carry through a swap.
MAX_RESTORABLE_DB_NAME_BYTES = PG_IDENTIFIER_MAX_BYTES - RESTORE_DERIVED_SUFFIX_BYTES

#: Peak disk: the staging database, the parked database, and the extracted dump
#: all coexist, so the dump counts three times and the vault twice.
_DUMP_DISK_FACTOR = 3
_VAULT_DISK_FACTOR = 2

#: Printed, not run: the graph rebuild takes minutes on a 1,200-doc corpus and
#: is not needed for `brain search` to work (§5.2).
FOLLOW_UP = ("brain graphrag build --force", "brain doctor")


@dataclass(frozen=True)
class PreflightIssue:
    """One reason a restore is refused, or one thing the user should know."""

    code: str
    fatal: bool
    message: str
    remedy: str


@dataclass(frozen=True)
class Preflight:
    """Everything checked before anything is touched."""

    manifest: BackupManifest
    target_documents: int
    target_chunks: int
    target_sources: int
    target_vault_files: int
    required_bytes: int
    free_bytes: int
    issues: tuple[PreflightIssue, ...]

    @property
    def blocked(self) -> bool:
        return any(issue.fatal for issue in self.issues)

    @property
    def target_is_non_empty(self) -> bool:
        """True when the restore would destroy something that already exists."""
        return self.target_documents > 0 or self.target_vault_files > 0


@dataclass(frozen=True)
class PreparedRestore:
    """The read-only phase's output: an unpacked, parsed, checksum-verified archive."""

    staging_dir: Path
    manifest: BackupManifest
    sidecar_ok: bool | None


@dataclass(frozen=True)
class RestoreReport:
    """What `brain restore` actually did."""

    db_restored: bool
    vault_restored: bool
    pre_restore_backup: Path | None
    replaced_database: str | None
    replaced_vault_path: Path | None
    documents: int
    chunks: int
    follow_up: tuple[str, ...]


def _validated_db_name(name: str) -> str:
    if not _DB_NAME_RE.match(name):
        raise BackupError(
            f"refusing to use {name!r} as a database name: expected only "
            "lowercase letters, digits and underscores"
        )
    return name


def _validated_restorable_db_name(name: str) -> str:
    """Character class AND length — the live name a restore derives from.

    :func:`_validated_db_name` checks the character class only. That is enough
    for a name Postgres will merely quote, and NOT enough for this one: a
    restore appends ``_replaced_<stamp>`` to it, and if the result crosses
    :data:`PG_IDENTIFIER_MAX_BYTES` Postgres truncates it without saying so.
    The failure then surfaces much later, as `database "..." does not exist`
    for a name that is right there in the command — the length never appears in
    the error at all. Refusing here turns that into one actionable sentence.
    """
    validated = _validated_db_name(name)
    # BYTES, not characters: the limit is NAMEDATALEN-1 bytes. `_DB_NAME_RE`
    # admits only ASCII today, so the two are equal — this is written for the
    # byte limit anyway so that widening that character class cannot quietly
    # turn a byte budget into a character one.
    size = len(validated.encode("utf-8"))
    if size > MAX_RESTORABLE_DB_NAME_BYTES:
        raise BackupError(
            f"database name {validated!r} is {size} bytes, which is "
            f"{size - MAX_RESTORABLE_DB_NAME_BYTES} over what `brain restore` "
            f"can handle. A restore derives "
            f"'{validated}_replaced_<stamp>' from it "
            f"(+{RESTORE_DERIVED_SUFFIX_BYTES} bytes), and Postgres truncates "
            f"identifiers at {PG_IDENTIFIER_MAX_BYTES} bytes SILENTLY — so "
            f"this would otherwise surface later as `database \"...\" does "
            f"not exist` with nothing pointing at the length. The budget for "
            f"the database name itself is "
            f"{MAX_RESTORABLE_DB_NAME_BYTES} bytes."
        )
    return validated


def _count_vault_files(vault_path: Path) -> int:
    if not vault_path.is_dir():
        return 0
    return sum(1 for path in vault_path.rglob("*") if path.is_file())


def _installed_migration_head() -> str:
    """Newest migration shipped inside this installed package."""
    files = sorted(migrations_dir().glob("*.sql"))
    if not files:
        raise BackupError(
            "no migrations are packaged with this brain — the installation is "
            "broken; reinstall with: pipx reinstall secondbrain-py"
        )
    return files[-1].name


def extract_vault_tar(tar_path: Path, dest: Path) -> None:
    """Unpack the inner uncompressed ``vault.tar`` through the safe filter."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tar_path, "r:") as tar:
            members: Sequence[tarfile.TarInfo] = list(_safe_members(tar, dest))
            tar.extractall(dest, members=members)  # noqa: S202 — vetted above
    except tarfile.TarError as exc:
        raise BackupError(f"{tar_path.name} is not a readable tar: {exc}") from exc


def prepare_restore(
    archive: Path,
    cfg: Config,
    *,
    clock: Callable[[], datetime] | None = None,
) -> PreparedRestore:
    """Read-only phase: verify the sidecar, unpack, and parse the manifest.

    Nothing outside ``$BRAIN_HOME/run/restore-<ts>/`` is written, so this is
    safe to run before the gate — the user sees the archive's real contents
    before deciding anything.
    """
    if not archive.is_file():
        raise BackupError(f"no such archive: {archive}")
    now = clock() if clock is not None else datetime.now(UTC)
    sidecar_ok = verify_sidecar(archive)
    if sidecar_ok is False:
        raise RestoreIncompatible(
            f"{archive.name} does not match its .sha256 sidecar — the archive is "
            "corrupt or truncated (typically a bad copy to a USB stick or cloud "
            "drive). Restore refused; nothing was changed.",
            issues=(
                PreflightIssue(
                    code="checksum",
                    fatal=True,
                    message=f"{archive.name} fails its whole-archive checksum",
                    remedy="Re-copy the archive from its original location.",
                ),
            ),
        )
    manifest = read_manifest(archive)
    staging_dir = cfg.brain_home / "run" / f"restore-{now.strftime(TIMESTAMP_FORMAT)}"
    shutil.rmtree(staging_dir, ignore_errors=True)
    extract_archive(archive, staging_dir)
    return PreparedRestore(
        staging_dir=staging_dir, manifest=manifest, sidecar_ok=sidecar_ok
    )


def _checksum_issues(
    archive_dir: Path, manifest: BackupManifest
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for name, entry in manifest.files.items():
        member = archive_dir / name
        if not member.exists():
            issues.append(
                PreflightIssue(
                    code="checksum",
                    fatal=True,
                    message=f"archive member {name} is missing",
                    remedy="Re-create the backup; this archive is incomplete.",
                )
            )
        elif sha256_file(member) != entry.sha256:
            issues.append(
                PreflightIssue(
                    code="checksum",
                    fatal=True,
                    message=f"archive member {name} fails its checksum",
                    remedy="Re-copy the archive from its original location.",
                )
            )
    return issues


def _embedding_issues(
    manifest: BackupManifest, cfg: Config, embedder: Embedder
) -> list[PreflightIssue]:
    """Embedder / dim mismatches — the single most important abort.

    A silent restore here leaves every vector meaningless, and hybrid search
    degrades in a way that looks like a ranking bug rather than corruption.
    Both checks fire even when the names match: a user can repoint the model
    behind a backend name.
    """
    issues: list[PreflightIssue] = []
    if manifest.embedder != cfg.embedder:
        issues.append(
            PreflightIssue(
                code="embedder",
                fatal=True,
                message=(
                    f"Archive was embedded with '{manifest.embedder}' but "
                    f"BRAIN_EMBEDDER is '{cfg.embedder}'. Embeddings cannot be "
                    "re-projected across models; restoring would leave every "
                    "vector meaningless."
                ),
                remedy=(
                    f"Set BRAIN_EMBEDDER={manifest.embedder} and retry, or restore "
                    "into a database you will re-embed with 'brain reembed'."
                ),
            )
        )
    if manifest.embedding_dim != embedder.dim:
        issues.append(
            PreflightIssue(
                code="dim",
                fatal=True,
                message=(
                    f"Archive holds {manifest.embedding_dim}-dimensional vectors "
                    f"but the active embedder produces {embedder.dim}. Embeddings "
                    "cannot be re-projected across models."
                ),
                remedy=(
                    "Point the embedder config back at the model this archive was "
                    "built with, or re-embed after restoring."
                ),
            )
        )
    return issues


def _migration_issues(manifest: BackupManifest) -> list[PreflightIssue]:
    installed_head = _installed_migration_head()
    if manifest.migration_head == installed_head:
        return []
    if manifest.migration_head > installed_head:
        return [
            PreflightIssue(
                code="migration_head",
                fatal=True,
                message=(
                    f"Archive was created by a newer brain (head "
                    f"{manifest.migration_head}, installed head {installed_head})."
                ),
                remedy="Upgrade first: pipx upgrade secondbrain-py",
            )
        ]
    pending = [
        path.name
        for path in sorted(migrations_dir().glob("*.sql"))
        if path.name > manifest.migration_head
    ]
    return [
        PreflightIssue(
            code="migration_head",
            fatal=False,
            message=(
                f"Archive head {manifest.migration_head} is older than the "
                f"installed head {installed_head}."
            ),
            remedy=(
                f"'brain init' will apply {len(pending)} newer migration(s) after "
                "the restore. This runs automatically."
            ),
        )
    ]


def preflight(
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
    embedder: Embedder,
    conn: psycopg.Connection[Any] | None,
    *,
    vault_path: Path,
    db_leg: bool,
    vault_leg: bool,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
) -> Preflight:
    """Check every incompatibility *before* anything is modified.

    Returns findings rather than raising, so the caller can print all of them
    at once — a user fixing one blocker should not discover the next one only
    on the following run.
    """
    issues: list[PreflightIssue] = _checksum_issues(archive_dir, manifest)

    if db_leg:
        issues += _embedding_issues(manifest, cfg, embedder)
        issues += _migration_issues(manifest)
        if conn is not None:
            _display, target_version_num = server_version(conn)
            target_major = target_version_num // 10000
            if manifest.server_major > target_major:
                issues.append(
                    PreflightIssue(
                        code="server_version",
                        fatal=True,
                        message=(
                            f"Archive was dumped from PostgreSQL "
                            f"{manifest.server_major} but the target server is "
                            f"{target_major}. A newer dump cannot be restored "
                            "into an older server."
                        ),
                        remedy="Upgrade the Postgres container to match the archive.",
                    )
                )

    counts = summary_counts(conn) if conn is not None else None
    target_vault_files = _count_vault_files(vault_path) if vault_leg else 0

    dump_entry = manifest.files.get(DUMP_MEMBER)
    vault_entry = manifest.files.get(VAULT_MEMBER)
    required_bytes = (dump_entry.bytes if dump_entry else 0) * _DUMP_DISK_FACTOR + (
        vault_entry.bytes if vault_entry else 0
    ) * _VAULT_DISK_FACTOR

    data_dir = cfg.brain_home / "data" / "postgres"
    probe_target = data_dir if data_dir.is_dir() else archive_dir
    free_bytes = int(disk_usage(str(probe_target)).free)
    if free_bytes < required_bytes:
        issues.append(
            PreflightIssue(
                code="disk",
                fatal=True,
                message=(
                    f"Restore needs about {required_bytes:,} bytes free on "
                    f"{probe_target} but only {free_bytes:,} are available."
                ),
                remedy="Free up disk space and retry.",
            )
        )

    return Preflight(
        manifest=manifest,
        target_documents=counts.documents if counts else 0,
        target_chunks=counts.chunks if counts else 0,
        target_sources=counts.sources if counts else 0,
        target_vault_files=target_vault_files,
        required_bytes=required_bytes,
        free_bytes=free_bytes,
        issues=tuple(issues),
    )


def _maintenance_dsn(database_url: str) -> str:
    """The same server, connected to the ``postgres`` maintenance database.

    ``CREATE`` / ``ALTER`` / ``DROP DATABASE`` cannot run inside a transaction
    block, nor from a session connected to the database being renamed.
    """
    parts = dsn_parts(database_url)
    parts["dbname"] = "postgres"
    return psycopg.conninfo.make_conninfo(**parts)


def _restore_into(
    plan: PgToolPlan,
    parts: dict[str, str],
    dump_path: Path,
    target_db: str,
    runner: CommandRunner,
) -> None:
    """``pg_restore`` the dump into ``target_db`` with ``--exit-on-error``.

    ``--exit-on-error`` is the whole reason for the custom dump format: a
    half-applied restore fails loudly instead of printing errors and exiting 0.
    """
    restore_args = [
        "-U",
        parts.get("user", "brain"),
        "-d",
        target_db,
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
    ]
    password = parts.get("password")

    if plan.source != "container":
        run_tool(
            runner,
            plan.argv(
                *restore_args,
                "-h",
                parts.get("host", "localhost"),
                "-p",
                parts.get("port", "5432"),
                str(dump_path),
            ),
            timeout=RESTORE_TIMEOUT_S,
            env=plan.env(password),
            what="pg_restore",
        )
        return

    remote = f"/tmp/brain-restore-{uuid.uuid4().hex}.dump"  # noqa: S108 — in-container
    try:
        run_tool(
            runner,
            ["docker", "cp", str(dump_path), f"{plan.container}:{remote}"],
            timeout=RESTORE_TIMEOUT_S,
            what="docker cp of the dump",
        )
        with password_env_file(password) as env_file:
            run_tool(
                runner,
                plan.argv(*restore_args, remote, env_file=env_file),
                timeout=RESTORE_TIMEOUT_S,
                what="pg_restore",
            )
    finally:
        # Best-effort cleanup: a leftover temp file inside the container is
        # untidy, not dangerous, and must never mask the original failure.
        with contextlib.suppress(
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            runner.run(
                ["docker", "exec", str(plan.container), "rm", "-f", remote],
                timeout=RESTORE_TIMEOUT_S,
            )


def _terminate_other_sessions(conn: psycopg.Connection[Any], live_db: str) -> list[str]:
    """Terminate every other backend on ``live_db``; return their app names.

    A ``brain vault sync --watch`` daemon or ``brain-mcp`` holding a connection
    would make ``ALTER DATABASE ... RENAME`` fail with "database is being
    accessed by other users".
    """
    rows = conn.execute(
        """
        SELECT coalesce(nullif(application_name, ''), '?')
        FROM pg_stat_activity
        WHERE datname = %s AND pid <> pg_backend_pid()
        """,
        (live_db,),
    ).fetchall()
    if rows:
        conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
            """,
            (live_db,),
        )
    return [str(row[0]) for row in rows]


def _swap_databases(
    conn: psycopg.Connection[Any], live_db: str, staging_db: str, parked_db: str
) -> None:
    """Rename live → parked, then staging → live.

    ``ALTER DATABASE`` cannot be transactional, so the window between the two
    statements genuinely exists and is handled rather than wished away: if the
    second rename fails the live name is briefly absent, and the user gets the
    literal SQL that puts it back. Both databases still exist; no data is lost.
    """
    conn.execute(
        sql.SQL("ALTER DATABASE {old} RENAME TO {parked}").format(
            old=sql.Identifier(live_db), parked=sql.Identifier(parked_db)
        )
    )
    try:
        conn.execute(
            sql.SQL("ALTER DATABASE {staging} RENAME TO {live}").format(
                staging=sql.Identifier(staging_db), live=sql.Identifier(live_db)
            )
        )
    except psycopg.Error as exc:
        recovery = f"ALTER DATABASE {parked_db} RENAME TO {live_db};"
        raise RestoreAborted(
            "The restore failed between the two database renames. Your data is "
            f"intact but the database is currently named '{parked_db}'. Put it "
            f"back with:\n  {recovery}\nUnderlying error: {exc}",
            recovery_sql=recovery,
        ) from exc


def _verify_staging(
    conn: psycopg.Connection[Any], manifest: BackupManifest
) -> tuple[int, int]:
    """Confirm the restored staging DB matches the manifest before any swap."""
    try:
        counts = summary_counts(conn)
    except psycopg.Error as exc:
        # A pg_restore that exits 0 but leaves no schema (a truncated or
        # empty custom-format archive) would otherwise surface as a raw
        # UndefinedTable escaping the CLI's `except BrainError`.
        raise BackupError(
            "Restore verification failed: the restored database has no readable "
            f"brain schema ({exc}). Nothing was changed; your database is "
            "untouched."
        ) from exc
    expected_documents = manifest.counts.get("documents", 0)
    expected_chunks = manifest.counts.get("chunks", 0)
    if counts.documents != expected_documents or counts.chunks != expected_chunks:
        raise BackupError(
            "Restore verification failed: the restored data does not match the "
            f"manifest (expected {expected_documents} documents / "
            f"{expected_chunks} chunks, found {counts.documents} / "
            f"{counts.chunks}). Nothing was changed; your database is untouched."
        )
    try:
        row = conn.execute("SELECT max(name) FROM schema_migrations").fetchone()
    except psycopg.Error as exc:
        raise BackupError(
            "Restore verification failed: the restored database has no "
            f"schema_migrations table ({exc}). Nothing was changed; your "
            "database is untouched."
        ) from exc
    head = str(row[0]) if row is not None and row[0] is not None else ""
    if head != manifest.migration_head:
        raise BackupError(
            f"Restore verification failed: the restored schema head is {head!r} "
            f"but the manifest records {manifest.migration_head!r}. Nothing was "
            "changed; your database is untouched."
        )
    return counts.documents, counts.chunks


def _move_vault_aside(vault_path: Path, stamp: str) -> Path | None:
    """Rename an existing vault out of the way — never ``rmtree`` (§7)."""
    if not vault_path.is_dir() or not any(vault_path.iterdir()):
        return None
    aside = vault_path.with_name(f"{vault_path.name}.replaced-{stamp}")
    vault_path.rename(aside)
    return aside


def _restore_database(
    cfg: Config,
    state: PreparedRestore,
    *,
    stamp: str,
    runner: CommandRunner,
    report: Callable[[str], None],
) -> tuple[int, int, str]:
    """Restore into a staging DB, verify it, then swap it in. Returns counts + parked."""
    manifest = state.manifest
    parts = dsn_parts(cfg.database_url)
    live_db = _validated_restorable_db_name(parts.get("dbname", ""))
    suffix = stamp.replace("-", "_")
    staging_db = _validated_db_name(f"{live_db}_restore_{suffix}")
    parked_db = _validated_db_name(f"{live_db}_replaced_{suffix}")
    owner = parts.get("user", "brain")

    with connect(cfg.database_url) as probe:
        _display, version_num = server_version(probe)
    plan = resolve_pg_tool(
        "pg_restore",
        server_major=version_num // 10000,
        container=postgres_container_name(),
        runner=runner,
        database_url=cfg.database_url,
    )

    with connect(_maintenance_dsn(cfg.database_url)) as maintenance:
        maintenance.autocommit = True
        maintenance.execute(
            sql.SQL("CREATE DATABASE {name} OWNER {owner}").format(
                name=sql.Identifier(staging_db), owner=sql.Identifier(owner)
            )
        )
        report(f"created staging database {staging_db}")
        try:
            staging_parts = dict(parts)
            staging_parts["dbname"] = staging_db
            _restore_into(
                plan, staging_parts, state.staging_dir / DUMP_MEMBER, staging_db, runner
            )
            with connect(psycopg.conninfo.make_conninfo(**staging_parts)) as staged:
                documents, chunks = _verify_staging(staged, manifest)
            report(f"pg_restore ({documents} documents / {chunks} chunks)")
        except BaseException:
            # Nothing outside the staging database has been touched yet, so the
            # live database is provably untouched when this propagates.
            maintenance.execute(
                sql.SQL("DROP DATABASE IF EXISTS {name}").format(
                    name=sql.Identifier(staging_db)
                )
            )
            raise

        terminated = _terminate_other_sessions(maintenance, live_db)
        if terminated:
            report(
                f"terminated {len(terminated)} other connection(s) "
                f"({', '.join(sorted(set(terminated)))})"
            )
        _swap_databases(maintenance, live_db, staging_db, parked_db)
        report(f"swapped: {live_db} → {parked_db}")

    return documents, chunks, parked_db


def restore_backup(
    archive: Path,
    cfg: Config,
    *,
    db_leg: bool = True,
    vault_leg: bool = True,
    runner: CommandRunner | None = None,
    clock: Callable[[], datetime] | None = None,
    pre_backup: Callable[[], Path] | None = None,
    on_step: Callable[[str], None] | None = None,
    embedder: Embedder | None = None,
    prepared: PreparedRestore | None = None,
) -> RestoreReport:
    """Put an archive back, without ever dropping what is already there.

    The database is restored into ``<live>_restore_<ts>``, verified against the
    manifest, and only then swapped in by renaming the live database aside to
    ``<live>_replaced_<ts>``. The previous database survives the command; the
    user drops it manually once satisfied. The vault is renamed aside the same
    way — never deleted.

    ``prepared`` lets the CLI reuse the read-only phase it already ran to build
    the pre-gate summary, instead of unpacking the archive a second time.
    """
    active_runner = runner if runner is not None else SubprocessRunner()
    now = clock() if clock is not None else datetime.now(UTC)
    report = on_step if on_step is not None else (lambda _message: None)
    stamp = now.strftime(TIMESTAMP_FORMAT)

    # Length-check the live database name BEFORE the pre-restore backup below,
    # not just where the derived names are built. `_restore_database` validates
    # it too — that is the enforcement point for a direct caller — but by the
    # time it runs, a full pg_dump has already been taken. An over-budget name
    # cannot be restored at all, so making the user wait for a backup first
    # buys nothing.
    if db_leg:
        _validated_restorable_db_name(dsn_parts(cfg.database_url).get("dbname", ""))

    state = (
        prepared
        if prepared is not None
        else prepare_restore(archive, cfg, clock=lambda: now)
    )
    manifest = state.manifest

    # Safety net first: a restore without one is exactly the scenario the
    # never-destroy-prod rule exists to prevent.
    if pre_backup is not None:
        pre_restore_path = pre_backup()
    else:
        pre_restore_path = create_backup(
            cfg,
            out_dir=cfg.backup_dir,
            include_vault=vault_leg,
            label="pre-restore",
            runner=active_runner,
            clock=lambda: now,
            embedder=embedder,
        ).archive_path
    report(f"pre-restore backup  ← undo with: brain restore {pre_restore_path}")

    documents = 0
    chunks = 0
    replaced_database: str | None = None
    db_restored = False
    if db_leg:
        documents, chunks, replaced_database = _restore_database(
            cfg, state, stamp=stamp, runner=active_runner, report=report
        )
        db_restored = True

    replaced_vault_path: Path | None = None
    vault_restored = False
    if vault_leg and manifest.vault_included:
        replaced_vault_path = _move_vault_aside(cfg.vault_path, stamp)
        extract_vault_tar(state.staging_dir / VAULT_MEMBER, cfg.vault_path)
        vault_restored = True
        report(f"restored vault ({manifest.vault_file_count} files)")
    elif vault_leg:
        report("vault SKIPPED (not present in archive)")

    if db_restored:
        # Exactly what `brain init` does, plus the ANALYZE the existing doctor
        # warning ("chunks stats WARN — never analyzed ... can happen after
        # pg_restore") demands. Both are idempotent and non-destructive.
        with connect(cfg.database_url) as conn:
            conn.autocommit = True
            applied = run_migrations(conn)
            active_embedder = embedder if embedder is not None else make_embedder(cfg)
            ensure_embedding_column(conn, active_embedder)
            bootstrap_age(conn)
            report(f"brain init ({len(applied)} new migrations)")
            analyze_tables(conn, ["chunks", "documents"])
            report("brain analyze (chunks, documents)")

    shutil.rmtree(state.staging_dir, ignore_errors=True)

    return RestoreReport(
        db_restored=db_restored,
        vault_restored=vault_restored,
        pre_restore_backup=pre_restore_path,
        replaced_database=replaced_database,
        replaced_vault_path=replaced_vault_path,
        documents=documents,
        chunks=chunks,
        follow_up=FOLLOW_UP,
    )
