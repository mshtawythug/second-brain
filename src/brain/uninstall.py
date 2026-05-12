"""brain uninstall — removes launchd plists, $BRAIN_HOME runtime state, and optionally the vault.

Does NOT remove the pipx installation itself; a CLI cannot safely uninstall
its own running process.  After running this command, the user should manually
run `pipx uninstall second-brain`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import typer

from ._compose import compose_cmd
from .config import DEFAULT_VAULT_PATH, _brain_home_root

# Files / dirs inside $BRAIN_HOME that are safe to remove even when --remove-db
# is NOT set (i.e. everything except the data/ subtree).
_BRAIN_HOME_SAFE_REMOVES = (
    ".env",
    ".shims",
    "Caddyfile",
    "docker-compose.yml",
    "bin",
    "logs",
    "build",
)


def _step(label: str) -> None:
    typer.echo(f"  {label}")


def _ok(label: str) -> None:
    typer.secho(f"  [ok]     {label}", fg="green")


def _skipped(label: str) -> None:
    typer.echo(f"  [skipped] {label}")


def run_uninstall(
    *,
    yes: bool = False,
    remove_db: bool = False,
    remove_vault: bool = False,
    brain_home: Path | None = None,
    vault_path: Path | None = None,
    _launchd_uninstall: object | None = None,
) -> None:
    """Core uninstall logic — separated from the Typer command for testability.

    Parameters
    ----------
    yes:
        Skip the interactive confirmation prompt.
    remove_db:
        Also remove ``$BRAIN_HOME/data/postgres/`` (requires extra typed
        confirmation regardless of ``yes``).
    remove_vault:
        Also remove the vault directory.
    brain_home:
        Override the resolved ``$BRAIN_HOME`` path (for tests).
    vault_path:
        Override the resolved ``$BRAIN_VAULT_PATH`` path (for tests).
    _launchd_uninstall:
        Dependency-injected callable that replaces ``brain.bin.launchd.uninstall_main``
        (for tests that don't want real launchctl calls).
    """
    home = brain_home if brain_home is not None else _brain_home_root()
    vault = vault_path if vault_path is not None else DEFAULT_VAULT_PATH

    # -----------------------------------------------------------------------
    # 1. Print summary of what will be removed.
    # -----------------------------------------------------------------------
    typer.echo("")
    typer.echo("🧠 brain uninstall")
    typer.echo("")
    typer.echo("The following will be removed:")
    typer.echo("  • launchd plists (macOS only)")
    typer.echo("  • Docker compose containers / networks (brain project)")
    for name in _BRAIN_HOME_SAFE_REMOVES:
        p = home / name
        if p.exists():
            typer.echo(f"  • {p}")
    if remove_db:
        typer.echo(f"  • {home / 'data' / 'postgres'}  ← ALL DATABASE DATA")
    if remove_vault:
        typer.echo(f"  • {vault}  ← YOUR NOTES")
    typer.echo("")

    # -----------------------------------------------------------------------
    # 2. Interactive confirmation (unless --yes).
    # -----------------------------------------------------------------------
    if not yes:
        typer.confirm("Proceed?", default=False, abort=True)

    # -----------------------------------------------------------------------
    # 3. Extra typed confirmation for --remove-db (NEVER bypassed by --yes).
    # -----------------------------------------------------------------------
    if remove_db:
        typer.echo("")
        typer.echo("WARNING: --remove-db will permanently delete all ingested documents.")
        answer = typer.prompt('Type "yes, delete my data" to confirm')
        if answer != "yes, delete my data":
            typer.secho("Aborted — database data was NOT removed.", fg="yellow")
            raise typer.Abort()

    # -----------------------------------------------------------------------
    # 4. Uninstall launchd (macOS only).
    # -----------------------------------------------------------------------
    if sys.platform == "darwin":
        _step("Uninstalling launchd plists …")
        try:
            if _launchd_uninstall is not None:
                _launchd_uninstall()  # type: ignore[operator]
            else:
                from .bin.launchd import uninstall_main
                uninstall_main()
            _ok("launchd plists removed")
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"  [warn]   launchd uninstall: {exc}", fg="yellow")
    else:
        _skipped("launchd (not macOS)")

    # -----------------------------------------------------------------------
    # 5. Stop Docker compose (fail-silent — user may have stopped it already).
    # -----------------------------------------------------------------------
    _step("Stopping Docker compose …")
    try:
        subprocess.run(
            compose_cmd("down", brain_home=home),
            check=False,
            capture_output=True,
            timeout=30,
        )
        _ok("docker compose down")
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"  [warn]   docker compose down: {exc}", fg="yellow")

    # -----------------------------------------------------------------------
    # 6. Remove safe $BRAIN_HOME entries.
    # -----------------------------------------------------------------------
    _step(f"Removing $BRAIN_HOME runtime files from {home} …")
    for name in _BRAIN_HOME_SAFE_REMOVES:
        p = home / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            _ok(f"removed dir  {p}")
        elif p.is_file():
            p.unlink(missing_ok=True)
            _ok(f"removed file {p}")

    # -----------------------------------------------------------------------
    # 7. Optionally remove the database data directory.
    # -----------------------------------------------------------------------
    if remove_db:
        pg_dir = home / "data" / "postgres"
        if pg_dir.exists():
            shutil.rmtree(pg_dir)
            _ok(f"removed database {pg_dir}")
        else:
            _skipped(f"data/postgres not found at {pg_dir}")

    # -----------------------------------------------------------------------
    # 8. Optionally remove the vault.
    # -----------------------------------------------------------------------
    if remove_vault:
        if vault.exists():
            shutil.rmtree(vault)
            _ok(f"removed vault {vault}")
        else:
            _skipped(f"vault not found at {vault}")
    else:
        _skipped(f"vault kept at {vault}  (pass --remove-vault to delete)")

    # -----------------------------------------------------------------------
    # 9. Final hint.
    # -----------------------------------------------------------------------
    typer.echo("")
    typer.secho("Done. To complete removal, run:", fg="cyan")
    typer.secho("  pipx uninstall second-brain", bold=True)
    typer.echo("")
