"""`brain backup` / `brain restore` — the CLI surface over :mod:`brain.backup`.

Rendering and gating only; every decision lives in the core package so it stays
testable without a Typer runner.

Like every extracted command module, this one must never import ``brain.cli``
at module scope — ``cli.py`` imports *it*. Anything owned by ``cli.py`` (the
embedder patch point, the doctor report's value types) is resolved through the
module object inside a function body, the same delegation pattern as
:mod:`brain.cli_search`.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from .backup import (
    RESTORE_PHRASE,
    Preflight,
    RestoreReport,
    create_backup,
    latest_backup,
    preflight,
    prepare_restore,
    restore_backup,
    validate_label,
)
from .config import Config
from .db import connect
from .errors import BackupError, BrainError, RestoreAborted, RestoreIncompatible
from .format import emit_json
from .ingest import Embedder

if TYPE_CHECKING:  # pragma: no cover — typing only; the real import is deferred
    from .cli import _DoctorCheck

#: Exit code for "this archive will not work here", distinct from a generic
#: failure so scripts can tell "won't work" from "broke". Mirrors the exit-3
#: convention `brain eval --fail-below` already uses.
EXIT_INCOMPATIBLE = 3

#: `brain doctor` nags when the newest backup is older than this.
STALE_BACKUP_DAYS = 30

_MIB = 1024 * 1024


def _build_embedder(cfg: Config) -> Embedder:
    """Build the configured embedder via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_embedder(cfg)  # type: ignore[attr-defined]


def _human_bytes(count: int) -> str:
    return f"{count / _MIB:,.1f} MiB"


def _ok(label: str) -> None:
    typer.secho(f"  [ok]     {label}", fg="green")


# ---------------------------------------------------------------------------
# brain backup
# ---------------------------------------------------------------------------


def backup_cmd(
    out: Path = typer.Option(
        None,
        "--out",
        "-o",
        help="Directory to write the archive into. Created if missing.",
    ),
    no_vault: bool = typer.Option(
        False, "--no-vault", help="Skip the vault tree; back up the database only."
    ),
    label: str = typer.Option(
        "",
        "--label",
        help=(
            "Short label folded into the filename and recorded in the manifest "
            "(e.g. 'pre-upgrade'). Letters, digits, '-' and '_' only."
        ),
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the manifest as JSON instead of the human summary."
    ),
) -> None:
    """Write a checksummed snapshot of the database and vault to one archive.

    The archive holds the full corpus in PLAINTEXT. It is a logical dump, not a
    copy of the data/postgres bind mount — and note that `docker compose down
    -v` does not wipe that mount either.
    """
    try:
        validate_label(label)
    except BackupError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if out is not None and out.exists() and not out.is_dir():
        raise typer.BadParameter(f"--out must be a directory, but {out} is a file")

    cfg = Config.load()
    steps: list[str] = []
    try:
        result = create_backup(
            cfg,
            out_dir=out,
            include_vault=not no_vault,
            label=label,
            on_step=steps.append,
        )
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc

    if json_output:
        payload: dict[str, Any] = {
            "archive_path": str(result.archive_path),
            "archive_bytes": result.archive_bytes,
            "archive_sha256": result.archive_sha256,
            **result.manifest.to_dict(),
        }
        emit_json(payload)
        return

    manifest = result.manifest
    typer.echo("")
    typer.echo("🧠 brain backup")
    typer.echo("")
    typer.echo(
        f"  database    {manifest.database_name} "
        f"(PostgreSQL {manifest.postgres_version})"
    )
    source = (
        f"in container {manifest.container_name}"
        if manifest.pg_dump_source == "container"
        else "on this host"
    )
    typer.echo(f"  pg_dump     {manifest.pg_dump_version} ({source})")
    if manifest.vault_included:
        typer.echo(
            f"  vault       {manifest.vault_path}  ({manifest.vault_file_count} files)"
        )
    typer.echo("")
    for step in steps:
        _ok(step)
    typer.echo("")
    typer.echo(f"  archive   {result.archive_path}")
    typer.echo(f"  size      {_human_bytes(result.archive_bytes)}")
    typer.echo(
        f"  sha256    {result.archive_sha256[:16]}…  (also written to "
        f"{result.archive_path.name}.sha256)"
    )
    typer.echo("")
    typer.echo("Restore with:")
    typer.echo(f"  brain restore {result.archive_path}")
    typer.echo("")


# ---------------------------------------------------------------------------
# brain restore
# ---------------------------------------------------------------------------


def _print_preflight(check: Preflight, archive: Path, *, vault_path: Path) -> None:
    """The pre-gate summary: what the archive holds versus what is here now."""
    manifest = check.manifest
    counts = manifest.counts
    typer.echo("")
    typer.echo("🧠 brain restore")
    typer.echo("")
    typer.echo(f"  archive     {archive.name}")
    typer.echo(
        f"  taken       {manifest.created_at:%Y-%m-%d %H:%M:%S}  by brain "
        f"{manifest.brain_version}"
    )
    vault_part = (
        f" / {manifest.vault_file_count} vault files" if manifest.vault_included else ""
    )
    typer.echo(
        f"  contains    {counts.get('documents', 0)} documents / "
        f"{counts.get('chunks', 0)} chunks / {counts.get('sources', 0)} sources"
        f"{vault_part}"
    )
    typer.echo(f"  schema      {manifest.migration_head}")
    typer.echo(f"  embedder    {manifest.embedder} / {manifest.embedding_column_type}")
    typer.echo(f"  postgres    dumped from {manifest.postgres_version}")
    typer.echo(
        f"  disk        needs {_human_bytes(check.required_bytes)}, "
        f"{_human_bytes(check.free_bytes)} free"
    )
    typer.echo("")
    typer.echo("WILL BE OVERWRITTEN:")
    typer.echo(f"  • database {manifest.database_name}")
    typer.echo(
        f"      currently holds {check.target_documents} documents / "
        f"{check.target_chunks} chunks / {check.target_sources} sources"
    )
    if check.target_vault_files:
        typer.echo(f"  • vault {vault_path}")
        typer.echo(f"      currently holds {check.target_vault_files} files")
    typer.echo("")


def _gate(check: Preflight, *, yes: bool) -> None:
    """Confirm, then demand the typed phrase whenever the target is not empty.

    ``--yes`` skips the y/N prompt but can NEVER skip the typed phrase while
    something would be destroyed — a strict superset of the `brain uninstall`
    contract, which exists because this project already suffered one accidental
    production wipe. When both targets are empty (the fresh-machine
    disaster-recovery case) nothing is being destroyed, so ``--yes`` is honoured
    fully and the flow stays scriptable.
    """
    if not yes:
        typer.confirm("Proceed?", default=False, abort=True)
    if not check.target_is_non_empty:
        return
    answer = typer.prompt(f'Type "{RESTORE_PHRASE}" to confirm', default="")
    if answer != RESTORE_PHRASE:
        typer.secho("Aborted — nothing was changed.", fg="yellow")
        raise typer.Abort()


def _print_report(report: RestoreReport, steps: list[str]) -> None:
    for step in steps:
        _ok(step)
    typer.echo("")
    typer.secho("Done.", fg="green")
    if report.replaced_database:
        typer.echo(f"  The previous database is retained as {report.replaced_database}.")
        typer.echo(
            "  Drop it when satisfied:   docker exec second-brain-postgres psql "
            f"-U brain -d postgres -c 'DROP DATABASE {report.replaced_database}'"
        )
    if report.replaced_vault_path:
        typer.echo(f"  The previous vault is retained at {report.replaced_vault_path}.")
    for item in report.follow_up:
        typer.echo(f"  Then run: {item}")
    typer.echo("")


def restore_cmd(
    archive: Path = typer.Argument(
        ..., help="Path to a brain-backup-*.tar.gz produced by 'brain backup'."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help=(
            "Skip the y/N confirmation. NEVER skips the typed-phrase gate when "
            "the target database or vault is non-empty."
        ),
    ),
    db_only: bool = typer.Option(
        False,
        "--db-only",
        help="Restore the database only; leave the vault on disk untouched.",
    ),
    vault_only: bool = typer.Option(
        False,
        "--vault-only",
        help="Restore the vault only; leave the database untouched.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a machine-readable result object instead of the human transcript.",
    ),
) -> None:
    """Restore an archive, keeping the replaced database and vault recoverable."""
    if db_only and vault_only:
        raise typer.BadParameter("--db-only and --vault-only are mutually exclusive")

    db_leg = not vault_only
    vault_leg = not db_only
    cfg = Config.load()

    try:
        state = prepare_restore(archive, cfg)
    except RestoreIncompatible as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(EXIT_INCOMPATIBLE) from exc
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc

    if vault_only and not state.manifest.vault_included:
        raise typer.BadParameter(
            f"{archive.name} contains no vault (it was taken with --no-vault)"
        )
    if state.sidecar_ok is None:
        typer.secho(
            f"  [warn]   {archive.name}.sha256 is missing — falling back to the "
            "manifest's per-member checksums.",
            fg="yellow",
        )

    try:
        embedder = _build_embedder(cfg)
        with connect(cfg.database_url) as conn:
            check = preflight(
                state.staging_dir,
                state.manifest,
                cfg,
                embedder,
                conn,
                vault_path=cfg.vault_path,
                db_leg=db_leg,
                vault_leg=vault_leg,
            )
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc

    # In --json mode stdout must stay parseable, so the human summary is
    # suppressed and any blocking issues come back as JSON instead.
    if not json_output:
        _print_preflight(check, archive, vault_path=cfg.vault_path)
        for issue in check.issues:
            colour = "red" if issue.fatal else "yellow"
            marker = "FATAL" if issue.fatal else "note "
            typer.secho(f"  [{marker}]  {issue.message}", fg=colour)
            typer.secho(f"            {issue.remedy}", fg=colour)

    if check.blocked:
        if json_output:
            emit_json(
                {
                    "restored": False,
                    "blocked": True,
                    "issues": [
                        {
                            "code": issue.code,
                            "fatal": issue.fatal,
                            "message": issue.message,
                            "remedy": issue.remedy,
                        }
                        for issue in check.issues
                    ],
                }
            )
        else:
            typer.secho("\nRestore refused — nothing was changed.", fg="red", err=True)
        raise typer.Exit(EXIT_INCOMPATIBLE)

    if not json_output:
        typer.echo("A pre-restore backup will be taken first, into:")
        typer.echo(f"  {cfg.backup_dir}")
        typer.echo("")
    _gate(check, yes=yes)

    steps: list[str] = []
    try:
        report = restore_backup(
            archive,
            cfg,
            db_leg=db_leg,
            vault_leg=vault_leg,
            on_step=steps.append,
            embedder=embedder,
            prepared=state,
        )
    except RestoreAborted as exc:
        typer.secho(str(exc), fg="red", err=True)
        if json_output:
            emit_json({"restored": False, "recovery_sql": exc.recovery_sql})
        raise typer.Exit(1) from exc
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc

    if json_output:
        emit_json(
            {
                "schema": state.manifest.schema,
                "restored": True,
                "db_restored": report.db_restored,
                "vault_restored": report.vault_restored,
                "archive_path": str(archive),
                "manifest": state.manifest.to_dict(),
                "pre_restore_backup": (
                    str(report.pre_restore_backup)
                    if report.pre_restore_backup
                    else None
                ),
                "replaced_database": report.replaced_database,
                "replaced_vault_path": (
                    str(report.replaced_vault_path)
                    if report.replaced_vault_path
                    else None
                ),
                "documents": report.documents,
                "chunks": report.chunks,
                "follow_up": list(report.follow_up),
            }
        )
        return

    _print_report(report, steps)


# ---------------------------------------------------------------------------
# brain doctor hook
# ---------------------------------------------------------------------------


def backup_doctor_checks(cfg: Config) -> list[_DoctorCheck]:
    """One soft ``last backup`` check for the doctor report.

    Never fails (status is only ``ok`` or ``warn``), so a brain that has simply
    never been backed up still passes `brain doctor` — the line is a nudge, not
    a gate.
    """
    from .cli import _DoctorCheck, _DoctorLine

    def _warn(detail: str) -> list[_DoctorCheck]:
        return [
            _DoctorCheck(
                check="last backup",
                status="warn",
                detail=detail,
                remedy="brain backup",
                lines=(
                    _DoctorLine(
                        text=f"last backup     WARN — {detail}. Run: brain backup",
                        fg="yellow",
                    ),
                ),
            )
        ]

    newest = latest_backup(cfg.backup_dir)
    if newest is None:
        return _warn(f"no backup found in {cfg.backup_dir}")

    when = f"{newest.created_at:%Y-%m-%d}" if newest.created_at else "unknown date"
    size = _human_bytes(newest.bytes)
    age = newest.age_days
    if age is not None and age > STALE_BACKUP_DAYS:
        return _warn(f"newest backup is {age:.0f} days old ({when}, {size})")

    detail = f"{when}, {size}"
    return [
        _DoctorCheck(
            check="last backup",
            status="ok",
            detail=detail,
            remedy=None,
            lines=(_DoctorLine(text=f"last backup     OK ({detail})"),),
        )
    ]


def register_backup_commands(app: typer.Typer) -> None:
    """Attach ``backup`` and ``restore`` to ``app``.

    Called from ``cli.py``; Typer lists commands in registration order, so the
    position of that call determines where they appear in ``brain --help``.
    """
    app.command(name="backup")(backup_cmd)
    app.command(name="restore")(restore_cmd)
