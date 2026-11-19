"""brain uninstall — removes launchd plists, $BRAIN_HOME runtime state, and optionally the vault.

Does NOT remove the pipx installation itself; a CLI cannot safely uninstall
its own running process.  After running this command, the user should manually
run `pipx uninstall secondbrain-py`.

The vault (F16) is resolved the way the installers resolve it
(:func:`brain.install_config.resolve_install_vault_with_source`), and before
anything is removed: step 6 deletes ``$BRAIN_HOME/.env``, which may be the only
place the path is set. Removing it needs that resolved path typed back exactly,
and ``--yes`` never skips the prompt. Unsafe targets are refused up front
(:func:`_vault_refusal`).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from ._compose import compose_cmd
from .config import _brain_home_root
from .install_config import resolve_install_vault_with_source

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


def _warn(label: str) -> None:
    typer.secho(f"  [warn]   {label}", fg="yellow")


#: Source label for the test-only ``vault_path`` override of :func:`run_uninstall`.
_VAULT_SOURCE_OVERRIDE = "explicit override"
#: How much of a failed ``docker compose down``'s stderr the warning repeats (F41).
_STDERR_TAIL_LINES = 3
_STDERR_TAIL_CHARS = 300


@dataclass(frozen=True)
class _Vault:
    """The vault ``--remove-vault`` would delete, resolved before anything is removed.

    ``configured`` is the absolute path as configured (only used to spot a
    symlink); ``target`` is its canonical form, which is what the summary
    prints, what the user must type, and what ``rmtree`` receives.
    """

    configured: Path
    target: Path
    source: str

    @property
    def shown(self) -> str:
        return f"{self.target}  (from {self.source})"


def _resolve_vault(home: Path, override: Path | None) -> _Vault:
    if override is not None:
        raw, source = override, _VAULT_SOURCE_OVERRIDE
    else:
        raw, source = resolve_install_vault_with_source(home)
    configured = raw.expanduser().absolute()
    return _Vault(configured=configured, target=configured.resolve(), source=source)


def _is_same_or_ancestor(candidate: Path, path: Path) -> bool:
    resolved = path.resolve()
    return candidate == resolved or candidate in resolved.parents


def _vault_refusal(vault: _Vault, home: Path) -> str | None:
    """Why ``rmtree`` must not touch this vault, or ``None`` when it may.

    A symlink would be refused by ``rmtree`` only mid-run (after the runtime
    files are gone), as would a non-directory. The home directory, ``/`` (an
    ancestor of it), ``$BRAIN_HOME`` and its ancestors hold far more than notes;
    the last two would also take ``data/postgres`` without ``--remove-db``.
    """
    if vault.configured.is_symlink():
        return f"{vault.configured} is a symlink"
    if not vault.target.is_dir():
        return f"{vault.target} is not a directory"
    if _is_same_or_ancestor(vault.target, Path.home()):
        return f"{vault.target} is your home directory or a parent of it"
    if _is_same_or_ancestor(vault.target, home):
        return f"{vault.target} is $BRAIN_HOME or a parent of it"
    return None


def _confirm_vault_removal(vault: _Vault) -> None:
    """Typed confirmation of the resolved path. Never bypassed by ``--yes``.

    At EOF (no TTY, no input) click raises ``Abort``, so a script without the
    exact path on stdin stops here with nothing removed.
    """
    typer.echo("")
    typer.echo("WARNING: --remove-vault will permanently delete your notes at:")
    typer.echo(f"  {vault.shown}")
    answer = typer.prompt("Type the vault path exactly as shown above to confirm")
    if answer != str(vault.target):
        typer.secho("Aborted — the vault was NOT removed (nothing was removed).", fg="yellow")
        raise typer.Abort()


def _compose_down_failure(returncode: int, stderr: str | None) -> str:
    """The F41 warning: the exit code plus the last few non-empty stderr lines."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    tail = " | ".join(lines[-_STDERR_TAIL_LINES:])[-_STDERR_TAIL_CHARS:]
    message = f"docker compose down exited {returncode}"
    return f"{message}: {tail}" if tail else message


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
        Override the resolved vault path (for tests). It still goes through the
        refusal guards and the typed confirmation.
    _launchd_uninstall:
        Dependency-injected callable that replaces ``brain.bin.launchd.uninstall_main``
        (for tests that don't want real launchctl calls).
    """
    home = brain_home if brain_home is not None else _brain_home_root()
    # Resolve now: step 6 deletes $BRAIN_HOME/.env, which may hold the path.
    vault = _resolve_vault(home, vault_path)
    removing_vault = remove_vault and vault.configured.exists()
    if removing_vault:
        refusal = _vault_refusal(vault, home)
        if refusal is not None:
            typer.secho(f"Refusing --remove-vault: {refusal}. Nothing was removed.", fg="red")
            raise typer.Abort()

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
    if removing_vault:
        typer.echo(f"  • {vault.shown}  ← YOUR NOTES")
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
    # 3b. Typed confirmation of the resolved vault path (NEVER bypassed by --yes).
    # -----------------------------------------------------------------------
    if removing_vault:
        _confirm_vault_removal(vault)

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
    # 5. Stop Docker compose (never fatal — user may have stopped it already),
    #    but only an exit 0 is reported as [ok] (F41).
    # -----------------------------------------------------------------------
    _step("Stopping Docker compose …")
    try:
        result = subprocess.run(
            compose_cmd("down", brain_home=home),
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        _warn(f"docker compose down: {exc}")
    else:
        if result.returncode == 0:
            _ok("docker compose down")
        else:
            _warn(_compose_down_failure(result.returncode, result.stderr))

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
    if removing_vault:
        shutil.rmtree(vault.target)
        _ok(f"removed vault {vault.target}")
    elif remove_vault:
        _skipped(f"vault not found at {vault.target}, nothing to remove")
    else:
        _skipped(f"vault kept at {vault.shown}  (pass --remove-vault to delete)")

    # -----------------------------------------------------------------------
    # 9. Final hint.
    # -----------------------------------------------------------------------
    typer.echo("")
    typer.secho("Done. To complete removal, run:", fg="cyan")
    typer.secho("  pipx uninstall secondbrain-py", bold=True)
    typer.echo("")
