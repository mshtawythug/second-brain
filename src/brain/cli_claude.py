"""brain claude — install the Claude Code skill and the session-end capture hook."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from importlib.resources import files as resource_files
from pathlib import Path

import typer

from . import claude_hook, claude_settings
from .config import _brain_home_root
from .errors import BrainError, HookInstallError, SettingsFormatError
from .vault._atomic import atomic_write_text


class SkillInstallError(BrainError):
    """Raised when the Claude skill install can't proceed."""


_DEFAULT_TARGET_ROOT = Path.home() / ".claude" / "skills"
_SKILL_DIR_NAME = "brain"
_SKILL_FILENAME = "SKILL.md"

# --- Session-end capture hook (F1) ------------------------------------------

#: Default Claude Code config root. Note this differs from
#: ``_DEFAULT_TARGET_ROOT`` above: ``install-skill --target`` names the *skills*
#: root, while ``install-hooks --target`` names the ``.claude`` root, because two
#: artifacts (a script and settings.json) must land under one parent.
_DEFAULT_CLAUDE_ROOT = Path.home() / ".claude"
_HOOK_DIR_NAME = "hooks"
_HOOK_FILENAME = "brain-capture-hook.sh"
_SETTINGS_FILENAME = "settings.json"
#: Timestamped so repeated installs never overwrite an earlier safety copy.
_BACKUP_STAMP = "%Y%m%dT%H%M%SZ"
_HOOK_MODE = 0o755


def install_skill(
    target_root: Path | None = None,
    force: bool = False,
    uninstall: bool = False,
) -> None:
    """Install (or uninstall) the brain Claude Code skill."""
    root = target_root if target_root is not None else _DEFAULT_TARGET_ROOT
    skill_dir = root / _SKILL_DIR_NAME
    target = skill_dir / _SKILL_FILENAME

    if uninstall:
        _uninstall(skill_dir, target)
        return

    src_bytes = _read_skill_template()
    _install(target, skill_dir, src_bytes, force=force)


def _read_skill_template() -> bytes:
    """Read SKILL.md from package data."""
    res = resource_files("brain.templates.skill") / _SKILL_FILENAME
    return res.read_bytes()


def _install(target: Path, skill_dir: Path, src_bytes: bytes, *, force: bool) -> None:
    """Write src_bytes to target, creating skill_dir as needed."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if target.read_bytes() == src_bytes:
            print(f"skill up to date: {target}")
            return
        if not force:
            typer.confirm(
                f"{target} differs from the packaged SKILL.md. Overwrite?",
                abort=True,
            )
    target.write_bytes(src_bytes)
    print(f"skill installed: {target}")


def _uninstall(skill_dir: Path, target: Path) -> None:
    """Remove SKILL.md and rmdir the brain/ dir if empty."""
    target_removed = False
    if target.is_file():
        target.unlink()
        print(f"removed {target}")
        target_removed = True

    if skill_dir.is_dir():
        remaining = list(skill_dir.iterdir())
        if remaining:
            print(
                f"warning: {skill_dir} not empty — leaving it in place "
                f"({len(remaining)} other entries)",
                file=sys.stderr,
            )
        else:
            skill_dir.rmdir()
            print(f"removed {skill_dir}")

    if not target_removed and not skill_dir.is_dir():
        print("nothing to uninstall")


# ---------------------------------------------------------------------------
# brain claude install-hooks — the session-end capture nudge (F1)
# ---------------------------------------------------------------------------


def _read_hook_template() -> bytes:
    """Read the hook shim from package data."""
    res = resource_files("brain.templates.claude") / _HOOK_FILENAME
    return res.read_bytes()


def _sha256(data: bytes) -> str:
    """Lowercase hex SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def _backup_settings(settings_path: Path, *, now: datetime) -> Path | None:
    """Copy the ORIGINAL bytes of ``settings_path`` aside; None when there is nothing to copy.

    Unconditional on any change, because the rewrite reformats to ``indent=2``
    and a user is entitled to their original whitespace back.
    """
    try:
        original = settings_path.read_bytes()
    except FileNotFoundError:
        return None
    if not original.strip():
        return None

    backup = settings_path.with_name(
        f"{settings_path.name}.brain-backup-{now.strftime(_BACKUP_STAMP)}"
    )
    backup.write_bytes(original)
    return backup


def _install_hook_script(
    script_path: Path, src_bytes: bytes, *, force: bool, dry_run: bool
) -> None:
    """Install the shim, prompting on drift unless ``--force``.

    Mirrors ``ensure_shim`` (``bin/_launcher.py``) for sha256 drift detection and
    ``_install`` above for the confirm-unless-force prompt.
    """
    if script_path.is_file() and _sha256(script_path.read_bytes()) == _sha256(src_bytes):
        print(f"hook script up to date: {script_path}")
        return

    if dry_run:
        verb = "would update" if script_path.exists() else "would install"
        print(f"{verb} hook script: {script_path}")
        return

    if script_path.exists() and not force:
        typer.confirm(
            f"{script_path} differs from the packaged hook script. Overwrite?",
            abort=True,
        )

    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_bytes(src_bytes)
    os.chmod(script_path, _HOOK_MODE)
    print(f"hook script installed: {script_path}")


def _remove_hook_script(script_path: Path) -> None:
    """Unlink the shim and rmdir its directory only when empty.

    The exact contract of ``_uninstall`` above: never ``rm -rf``, and warn rather
    than delete when the directory holds someone else's hooks.
    """
    if script_path.is_file():
        script_path.unlink()
        print(f"removed {script_path}")

    hook_dir = script_path.parent
    if not hook_dir.is_dir():
        return
    remaining = list(hook_dir.iterdir())
    if remaining:
        print(
            f"warning: {hook_dir} not empty — leaving it in place "
            f"({len(remaining)} other entries)",
            file=sys.stderr,
        )
    else:
        hook_dir.rmdir()
        print(f"removed {hook_dir}")


def _apply_settings(
    settings_path: Path, merge: claude_settings.SettingsMerge, *, dry_run: bool
) -> None:
    """Back up then atomically write the merged document, when it changed."""
    if not merge.changed:
        return
    if dry_run:
        return

    backup = _backup_settings(settings_path, now=datetime.now(UTC))
    if backup is not None:
        print(f"backed up settings: {backup}")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(settings_path, claude_settings.serialize(merge.document))


def install_hooks(
    target_root: Path | None = None,
    force: bool = False,
    uninstall: bool = False,
    dry_run: bool = False,
) -> None:
    """Install (or uninstall) the Claude Code session-end capture hook.

    Two artifacts land under one root: the shim at
    ``<root>/hooks/brain-capture-hook.sh`` and a merged Stop entry in
    ``<root>/settings.json``. The settings file is the *user's own config*, so it
    is merged rather than written: an unparseable or unexpectedly-shaped document
    is a refusal (nothing written, no backup taken), and ``--force`` never
    overrides that — force means "overwrite a stale hook script", never "discard
    the user's editor config".
    """
    root = target_root if target_root is not None else _DEFAULT_CLAUDE_ROOT
    script_path = root / _HOOK_DIR_NAME / _HOOK_FILENAME
    settings_path = root / _SETTINGS_FILENAME

    # Parse BEFORE touching anything, so a malformed settings.json aborts the
    # whole command rather than leaving a half-installed hook behind.
    try:
        document = claude_settings.read_settings(settings_path)
        merge = (
            claude_settings.remove_stop_hook(document)
            if uninstall
            else claude_settings.merge_stop_hook(document, command=str(script_path))
        )
    except SettingsFormatError as exc:
        raise HookInstallError(str(exc)) from exc

    if uninstall:
        _apply_settings(settings_path, merge, dry_run=dry_run)
        if not merge.changed:
            print(f"no brain Stop hook found in {settings_path}")
        elif dry_run:
            print(f"would remove Stop hook entry from {settings_path}")
        else:
            print(f"removed Stop hook entry from {settings_path}")

        if dry_run:
            if script_path.is_file():
                print(f"would remove {script_path}")
            print("(dry run — nothing written)")
            return

        _remove_hook_script(script_path)
        return

    _install_hook_script(script_path, _read_hook_template(), force=force, dry_run=dry_run)
    _apply_settings(settings_path, merge, dry_run=dry_run)

    if merge.action == "added":
        print(
            f"would add Stop hook entry to {settings_path}"
            if dry_run
            else f"Stop hook added to {settings_path} (timeout 10s)"
        )
    elif merge.action == "updated":
        print(
            f"would update Stop hook entry in {settings_path}"
            if dry_run
            else f"updated Stop hook entry in {settings_path}"
        )
    else:
        print(f"Stop hook already present in {settings_path}")

    if dry_run:
        print("(dry run — nothing written)")
        return

    print(
        "capture nudge active — disable with BRAIN_HOOK_ENABLED=false or "
        "`brain claude install-hooks --uninstall`"
    )


def read_hook_stdin() -> bytes:
    """Read the Stop payload off stdin, yielding ``b""`` if that is impossible.

    Split out from the Typer command so the failure branch is reachable in a
    test: ``CliRunner`` installs its own ``sys.stdin``, so a test cannot inject a
    broken one through ``runner.invoke``.
    """
    try:
        return sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001 — see run_capture_hook.
        return b""


def run_capture_hook(raw_stdin: bytes) -> str:
    """Return the stdout the Stop hook should emit for ``raw_stdin`` (may be empty).

    Split out from the Typer command so the decision path is testable without a
    CliRunner. Never raises: the caller is a Stop hook, and an exception there is
    user-visible noise at best and a blocked session at worst.
    """
    try:
        decision = claude_hook.decide(
            raw_stdin,
            env=os.environ,
            run_root=_brain_home_root() / "run",
            now=datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001 — a Stop hook must never fail loudly.
        return ""

    if not decision.block:
        return ""
    return json.dumps({"decision": "block", "reason": decision.reason})


def register_claude_commands(claude_app: typer.Typer) -> None:
    """Attach ``install-hooks`` and the hidden ``capture-hook`` to ``claude_app``.

    A registrar rather than module-level decorators, matching the
    :mod:`brain.cli_registry` house pattern: ``cli.py`` owns the sub-app object
    and calls this once, so this module never imports ``brain.cli`` back.
    """

    @claude_app.command("install-hooks")
    def claude_install_hooks_cmd(
        target: Path | None = typer.Option(
            None,
            "--target",
            help=(
                "Override the Claude Code config root (default ~/.claude); installs "
                "<target>/hooks/brain-capture-hook.sh and merges <target>/settings.json"
            ),
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help=(
                "Overwrite a differing hook script without prompting. Never bypasses "
                "the malformed-settings.json refusal."
            ),
        ),
        uninstall: bool = typer.Option(
            False,
            "--uninstall",
            help=(
                "Remove the brain Stop hook entry and the hook script "
                "(settings.json is backed up first)."
            ),
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Print what would change and exit without writing anything.",
        ),
    ) -> None:
        """Install (or uninstall) the Claude Code session-end capture hook.

        Adds a Stop hook that nudges exactly one dedupe-then-capture pass after
        a session that did real work and wrote nothing back to the brain.
        Opt-in and separately reversible: it changes the behaviour of every
        Claude Code session on this machine, including in unrelated repos.

        ~/.claude/settings.json is merged, never clobbered — the original is
        backed up with a UTC timestamp first, and a malformed file is refused
        outright rather than rewritten. Disable the nudge at any time with
        BRAIN_HOOK_ENABLED=false, or remove it with --uninstall.
        """
        try:
            install_hooks(
                target_root=target, force=force, uninstall=uninstall, dry_run=dry_run
            )
        except HookInstallError as exc:
            typer.secho(f"error: {exc}", fg="red", err=True)
            raise typer.Exit(code=1) from exc

    @claude_app.command("capture-hook", hidden=True)
    def claude_capture_hook_cmd() -> None:
        """Plumbing: decide whether this Stop event earns a capture nudge.

        Reads one JSON object on stdin, writes at most one JSON line on stdout,
        and ALWAYS exits 0. Not for interactive use.
        """
        line = run_capture_hook(read_hook_stdin())
        if line:
            # Deliberately not `emit_json`: Rich soft-wraps, and Claude Code
            # needs exactly one parseable line.
            sys.stdout.write(line + "\n")
        raise typer.Exit(code=0)
