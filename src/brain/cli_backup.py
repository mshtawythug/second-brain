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

import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from .backup import (
    RESTORE_PHRASE,
    Preflight,
    PreparedRestore,
    RestoreReport,
    create_backup,
    discard_prepared_restore,
    latest_backup,
    preflight,
    prepare_restore,
    restore_backup,
    validate_label,
)
from .backup.create import dsn_parts
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


def _print_preflight(
    check: Preflight,
    archive: Path,
    *,
    vault_path: Path,
    target_database: str,
    db_leg: bool,
) -> None:
    """The pre-gate summary: what the archive holds versus what is here now.

    ``target_database`` is the database this restore replaces. It is not the
    archive's ``database_name``, which differs on a read replica (RM-47).
    ``db_leg`` is False for ``--vault-only``, which never touches the database,
    so the database is not listed as something that will be overwritten (F24).
    """
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
    if db_leg:
        typer.echo(f"  • database {target_database}")
        typer.echo(
            f"      currently holds {check.target_documents} documents / "
            f"{check.target_chunks} chunks / {check.target_sources} sources"
        )
    if check.target_vault_files:
        typer.echo(f"  • vault {vault_path}")
        typer.echo(f"      currently holds {check.target_vault_files} files")
    typer.echo("")


def _gate(check: Preflight, *, yes: bool, err: bool) -> None:
    """Confirm, then demand the typed phrase whenever the target is not empty.

    ``--yes`` skips the y/N prompt but can NEVER skip the typed phrase while
    something would be destroyed — a strict superset of the `brain uninstall`
    contract, which exists because this project already suffered one accidental
    production wipe. When both targets are empty (the fresh-machine
    disaster-recovery case) nothing is being destroyed, so ``--yes`` is honoured
    fully and the flow stays scriptable.

    ``err`` sends the prompts to stderr, which ``--json`` needs: the typed
    phrase cannot be skipped by any flag, so a ``--json`` run on a non-empty
    target always prompts, and stdout must carry nothing but the JSON (F23).
    """
    if not yes:
        if not err:
            typer.confirm("Proceed?", default=False, abort=True)
        elif _read_line_on_stderr("Proceed? [y/N]: ").strip().lower() not in {"y", "yes"}:
            raise typer.Abort()
    if not check.target_is_non_empty:
        return
    prompt = f'Type "{RESTORE_PHRASE}" to confirm'
    answer = (
        _read_line_on_stderr(f"{prompt}: ") if err else typer.prompt(prompt, default="")
    )
    if answer != RESTORE_PHRASE:
        typer.secho("Aborted — nothing was changed.", fg="yellow", err=err)
        raise typer.Abort()


def _read_line_on_stderr(prompt: str) -> str:
    """Prompt on stderr and read one line, writing NOTHING to stdout.

    Not ``typer.prompt(..., err=True)``: Click still writes the prompt's last
    character to stdout through ``input()`` (a readline workaround), which would
    put stray text ahead of the ``--json`` object. End of input aborts, as it
    does for Click's prompts.
    """
    typer.echo(prompt, nl=False, err=True)
    line = sys.stdin.readline()
    if not line:
        raise typer.Abort()
    return line.rstrip("\r\n")


#: The only DSN keys the drop hint may print. An allowlist, so a password,
#: an ``sslpassword`` or any key added later can never reach the terminal.
_HINT_DSN_FLAGS = (("host", "-h"), ("port", "-p"))


def drop_database_command(
    database: str, *, database_url: str, container: str | None
) -> str:
    """The shell command that drops the database a restore parked (F22).

    It targets the server the restore actually ran against, never an assumed
    one. ``container`` is the container ``pg_restore`` ran in, which
    :func:`brain.backup.pgtool.resolve_pg_tool` only picks after proving it
    publishes ``database_url``'s port. Without one, no container is known to
    serve this database, so the hint is ``psql`` against the DSN's host and port
    rather than a guessed ``docker exec``. Only host, port and user are read
    from the DSN, and every value is shell-quoted.
    """
    parts = dsn_parts(database_url)
    user = ["-U", parts["user"]] if parts.get("user") else []
    tail = [*user, "-d", "postgres", "-c", f"DROP DATABASE {database}"]
    if container is not None:
        argv = ["docker", "exec", container, "psql", *tail]
    else:
        where = [
            word
            for key, flag in _HINT_DSN_FLAGS
            if parts.get(key)
            for word in (flag, parts[key])
        ]
        argv = ["psql", *where, *tail]
    return shlex.join(argv)


def _print_report(report: RestoreReport, steps: list[str], *, database_url: str) -> None:
    for step in steps:
        _ok(step)
    typer.echo("")
    typer.secho("Done.", fg="green")
    if report.replaced_database:
        typer.echo(f"  The previous database is retained as {report.replaced_database}.")
        command = drop_database_command(
            report.replaced_database,
            database_url=database_url,
            container=report.restore_container,
        )
        typer.echo(f"  Drop it when satisfied:   {command}")
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

    # The unpack is a plaintext copy of the archive. Remove it however this
    # ends — declined, refused, blocked, failed or done (F27).
    try:
        _restore_prepared(
            archive,
            cfg,
            state,
            yes=yes,
            db_leg=db_leg,
            vault_leg=vault_leg,
            json_output=json_output,
        )
    finally:
        discard_prepared_restore(state, cfg)


def _restore_prepared(
    archive: Path,
    cfg: Config,
    state: PreparedRestore,
    *,
    yes: bool,
    db_leg: bool,
    vault_leg: bool,
    json_output: bool,
) -> None:
    """Everything after the archive is unpacked: preflight, gate, restore, report."""
    if not db_leg and not state.manifest.vault_included:  # i.e. --vault-only
        raise typer.BadParameter(
            f"{archive.name} contains no vault (it was taken with --no-vault)"
        )
    if state.sidecar_ok is None:
        typer.secho(
            f"  [warn]   {archive.name}.sha256 is missing — falling back to the "
            "manifest's per-member checksums.",
            fg="yellow",
            err=json_output,
        )

    embedder, check = _run_preflight(cfg, state, db_leg=db_leg, vault_leg=vault_leg)
    _show_preflight(check, archive, cfg, db_leg=db_leg, json_output=json_output)

    if not json_output:
        typer.echo("A pre-restore backup will be taken first, into:")
        typer.echo(f"  {cfg.backup_dir}")
        typer.echo("")
    try:
        _gate(check, yes=yes, err=json_output)
    except typer.Abort:
        if not json_output:
            raise
        # Without this a declined `--json` run printed a prompt and no JSON (F23).
        emit_json({"restored": False, "aborted": True})
        raise typer.Exit(1) from None

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
        emit_json(_result_payload(archive, state, report))
        return
    _print_report(report, steps, database_url=cfg.database_url)


def _run_preflight(
    cfg: Config, state: PreparedRestore, *, db_leg: bool, vault_leg: bool
) -> tuple[Embedder, Preflight]:
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
    return embedder, check


def _show_preflight(
    check: Preflight, archive: Path, cfg: Config, *, db_leg: bool, json_output: bool
) -> None:
    """Print the summary and its issues; exit 3 when a fatal issue blocks."""
    # In --json mode stdout must stay parseable, so the human summary is
    # suppressed and any blocking issues come back as JSON instead.
    if not json_output:
        _print_preflight(
            check,
            archive,
            vault_path=cfg.vault_path,
            target_database=dsn_parts(cfg.database_url).get("dbname", ""),
            db_leg=db_leg,
        )
        for issue in check.issues:
            colour = "red" if issue.fatal else "yellow"
            marker = "FATAL" if issue.fatal else "note "
            typer.secho(f"  [{marker}]  {issue.message}", fg=colour)
            typer.secho(f"            {issue.remedy}", fg=colour)

    if not check.blocked:
        return
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


def _result_payload(
    archive: Path, state: PreparedRestore, report: RestoreReport
) -> dict[str, Any]:
    return {
        "schema": state.manifest.schema,
        "restored": True,
        "db_restored": report.db_restored,
        "vault_restored": report.vault_restored,
        "archive_path": str(archive),
        "manifest": state.manifest.to_dict(),
        "pre_restore_backup": (
            str(report.pre_restore_backup) if report.pre_restore_backup else None
        ),
        "replaced_database": report.replaced_database,
        "replaced_vault_path": (
            str(report.replaced_vault_path) if report.replaced_vault_path else None
        ),
        "documents": report.documents,
        "chunks": report.chunks,
        "follow_up": list(report.follow_up),
    }


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
