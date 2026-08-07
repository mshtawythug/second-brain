"""brain wiki install — Quartz workspace + overlay installer."""
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.resources import files as resource_files
from pathlib import Path

from ..config import Config
from ..errors import BrainError
from ..vault.quartz_overlay import apply_overlay, plan_overlay
from . import QUARTZ_PINNED_COMMIT, QUARTZ_REPO_URL


class WikiInstallError(BrainError):
    """Raised when wiki install can't proceed."""


# Wall-clock ceilings for the external tools this installer shells out to.
# Without them a wedged git or npm hangs `brain wiki install` forever with no
# diagnostic — the same silent-hang class as the daemon faults this repo spent a
# day chasing. Deliberately generous: a cold full clone or a cold npm install on
# a slow link is legitimately slow, so these bound pathology, not normal use.
# (The `git rev-parse` probe in _check_quartz_pinned_commit keeps its own tight
# 10s ceiling — it reads one ref from an existing checkout and can never be slow.)
CLONE_TIMEOUT_S = 600.0
CHECKOUT_TIMEOUT_S = 120.0
NPM_INSTALL_TIMEOUT_S = 900.0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def wiki_install(
    vault: Path | None = None,
    port: int = 8080,
    force: bool = False,
    no_npm: bool = False,
    dry_run: bool = False,
) -> None:
    """Install Quartz workspace + apply brain overlay + render Caddyfile.

    On a fresh vault (no ``<vault>/.quartz/`` dir) this clones jackyzha0/quartz
    at :data:`brain.wiki.QUARTZ_PINNED_COMMIT`, applies the brain overlay via
    ``importlib.resources``, optionally runs ``npm install``, and renders
    ``$BRAIN_HOME/Caddyfile`` from the bundled ``Caddyfile.j2`` template.

    Re-running on an existing workspace re-applies the overlay and re-renders
    the Caddyfile without re-cloning (idempotent), unless ``--force`` is set
    (which wipes and re-clones the workspace first).
    """
    cfg = Config.load_minimal()
    vault_path = vault if vault is not None else cfg.vault_path
    brain_home = cfg.brain_home
    quartz_dir = vault_path / ".quartz"

    if dry_run:
        _print_planned_actions(vault_path, quartz_dir, brain_home, port, force)
        return

    fresh_install = force or not quartz_dir.exists()

    if force and quartz_dir.exists():
        print(f"--force: removing existing workspace {quartz_dir}", flush=True)
        shutil.rmtree(quartz_dir)
    elif quartz_dir.exists() and not _is_valid_quartz_workspace(quartz_dir):
        raise WikiInstallError(
            f"Quartz workspace at {quartz_dir} exists but appears incomplete "
            f"(missing package.json). The directory may be from a failed or "
            f"interrupted clone.\n"
            f"  Re-run with --force to wipe and re-clone:\n"
            f"    brain wiki install --force"
        )

    if fresh_install:
        print(f"Cloning Quartz into {quartz_dir} …", flush=True)
        _clone_quartz(quartz_dir)
        print(f"Checked out pinned commit {QUARTZ_PINNED_COMMIT[:12]}", flush=True)
    else:
        # Structural check already passed; now verify the pinned commit so a
        # workspace left at the wrong Quartz version (e.g. a failed checkout
        # step) is caught before the overlay is applied.
        _check_quartz_pinned_commit(quartz_dir)
        print(f"Existing workspace found at {quartz_dir} — refreshing overlay", flush=True)

    # Apply (or re-apply) the brain overlay.
    print("Applying brain overlay …", flush=True)
    plan = plan_overlay(quartz_dir)
    copied = apply_overlay(plan)
    print(f"  {len(copied)} file(s) copied", flush=True)

    # npm install.
    if not no_npm:
        print("Running npm install …", flush=True)
        _npm_install(quartz_dir)

    # Render Caddyfile.
    brain_home.mkdir(parents=True, exist_ok=True)
    caddyfile_path = brain_home / "Caddyfile"
    caddyfile_content = _render_caddyfile(brain_home, vault_path, port)
    _atomic_write_text(caddyfile_content, caddyfile_path, brain_home)
    print(f"Caddyfile written to {caddyfile_path}", flush=True)

    # Caddy availability check + instructions.
    caddy = _check_caddy()
    if caddy is None:
        print("", flush=True)
        print("warning: caddy not found on PATH", file=sys.stderr, flush=True)
        print("  Install with:", file=sys.stderr, flush=True)
        print("    macOS:   brew install caddy", file=sys.stderr, flush=True)
        print(
            "    Linux:   see https://caddyserver.com/docs/install",
            file=sys.stderr,
            flush=True,
        )
        print("  Then start it:", file=sys.stderr, flush=True)
        print(
            f"    caddy run --config {caddyfile_path}",
            file=sys.stderr,
            flush=True,
        )
    else:
        print("", flush=True)
        print("Caddy bootstrap (run one of):", flush=True)
        print(f"  caddy run --config {caddyfile_path}", flush=True)
        print("  brew services start caddy  (macOS — uses the Caddyfile above)", flush=True)

    if fresh_install:
        print("", flush=True)
        print("wiki workspace installed ✓", flush=True)
    else:
        print("", flush=True)
        print("wiki workspace refreshed ✓", flush=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_valid_quartz_workspace(quartz_dir: Path) -> bool:
    """Return True if *quartz_dir* has the minimum filesystem structure of a Quartz checkout.

    Two signals are required:

    * ``package.json`` — every Quartz clone ships one; absence means the clone
      was interrupted before files were written.
    * ``.git/`` directory — proves the directory was produced by ``git clone``,
      not by hand-placing a ``package.json``.  A dir that has ``package.json``
      but no ``.git/`` is not a git repository at all and cannot be refreshed.

    This is a pure filesystem check; it says nothing about which commit is
    checked out.  Use :func:`_check_quartz_pinned_commit` for the commit check.
    """
    return (
        (quartz_dir / "package.json").is_file()
        and (quartz_dir / ".git").is_dir()
    )


def _check_quartz_pinned_commit(quartz_dir: Path) -> None:
    """Verify the workspace HEAD matches QUARTZ_PINNED_COMMIT.

    ``git clone`` writes all files (including ``package.json``) before the
    subsequent ``git checkout <sha>`` step completes.  If that checkout step
    failed, the workspace is at the default branch HEAD rather than the pinned
    commit — the overlay is version-specific and will silently misbehave when
    applied to the wrong Quartz version.

    Runs ``git rev-parse HEAD`` inside *quartz_dir* and raises
    :class:`WikiInstallError` if the result does not match
    :data:`brain.wiki.QUARTZ_PINNED_COMMIT`.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(quartz_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        raise WikiInstallError(
            f"Could not read the Quartz workspace commit "
            f"(git rev-parse HEAD failed, exit {exc.returncode}).\n"
            f"  Re-run with --force to wipe and re-clone:\n"
            f"    brain wiki install --force"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WikiInstallError(
            "git rev-parse HEAD timed out (>10 s). "
            "Check that the .git/ directory is not corrupted."
        ) from exc

    head = result.stdout.strip()
    if head != QUARTZ_PINNED_COMMIT:
        raise WikiInstallError(
            f"Quartz workspace is at commit {head[:12]}, "
            f"expected {QUARTZ_PINNED_COMMIT[:12]}.\n"
            f"  The brain overlay is pinned to a specific Quartz version and\n"
            f"  may not work correctly at a different commit.  This can happen\n"
            f"  when the pinned-checkout step failed or a newer brain release\n"
            f"  bumped the pinned commit.\n"
            f"  Re-run with --force to wipe and re-clone at the correct commit:\n"
            f"    brain wiki install --force"
        )


def _clone_quartz(quartz_dir: Path) -> None:
    """Clone jackyzha0/quartz at the pinned commit.

    Full clone (not depth-1) so checking out a specific SHA always works —
    shallow clones require the commit to be reachable from a branch tip.
    """
    quartz_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", QUARTZ_REPO_URL, str(quartz_dir)],
            check=True,
            timeout=CLONE_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as exc:
        raise WikiInstallError(
            f"git clone failed (exit {exc.returncode}): {QUARTZ_REPO_URL} → {quartz_dir}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WikiInstallError(
            f"git clone timed out after {CLONE_TIMEOUT_S:g}s: "
            f"{QUARTZ_REPO_URL} → {quartz_dir}\n"
            f"  Check network connectivity, then re-run with --force to retry."
        ) from exc

    try:
        subprocess.run(
            ["git", "-C", str(quartz_dir), "checkout", QUARTZ_PINNED_COMMIT],
            check=True,
            timeout=CHECKOUT_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as exc:
        raise WikiInstallError(
            f"git checkout {QUARTZ_PINNED_COMMIT[:12]} failed (exit {exc.returncode})"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WikiInstallError(
            f"git checkout {QUARTZ_PINNED_COMMIT[:12]} timed out after "
            f"{CHECKOUT_TIMEOUT_S:g}s in {quartz_dir}\n"
            f"  The workspace may be left at the wrong commit; re-run with --force."
        ) from exc


def _npm_install(quartz_dir: Path) -> None:
    """Run ``npm install`` inside the Quartz workspace."""
    if shutil.which("npm") is None:
        raise WikiInstallError(
            "npm not found on PATH — install Node.js (https://nodejs.org)"
        )
    try:
        subprocess.run(
            ["npm", "install"],
            cwd=str(quartz_dir),
            check=True,
            timeout=NPM_INSTALL_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as exc:
        raise WikiInstallError(
            f"npm install failed (exit {exc.returncode}) in {quartz_dir}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WikiInstallError(
            f"npm install timed out after {NPM_INSTALL_TIMEOUT_S:g}s in {quartz_dir}\n"
            f"  Check network connectivity and the npm registry, then re-run\n"
            f"  `brain wiki install` (the clone is preserved, so this is cheap)."
        ) from exc


def _render_caddyfile(brain_home: Path, vault_path: Path, port: int) -> str:
    """Render Caddyfile.j2 → string via simple string substitution (no Jinja2)."""
    template = resource_files("brain.templates") / "Caddyfile.j2"
    text = template.read_text(encoding="utf-8")
    return (
        text
        .replace("{{ wiki_port }}", str(port))
        .replace("{{ vault_path }}", str(vault_path))
    )


def _check_caddy() -> Path | None:
    """Return the path to caddy on PATH, or None if missing."""
    caddy = shutil.which("caddy")
    return Path(caddy) if caddy else None


def _atomic_write_text(content: str, dest: Path, parent_dir: Path) -> None:
    """Write *content* to *dest* atomically via a unique same-dir tempfile."""
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent_dir, delete=False, suffix=".tmp"
        ) as tf:
            tmp_path = tf.name
            tf.write(content)
        os.replace(tmp_path, dest)
        tmp_path = None
    except Exception:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        raise


def _print_planned_actions(
    vault_path: Path,
    quartz_dir: Path,
    brain_home: Path,
    port: int,
    force: bool,
) -> None:
    """Print planned actions without touching the filesystem."""
    print("Dry-run — planned actions (nothing will be written):", flush=True)
    if force and quartz_dir.exists():
        print(f"  [rmtree]   {quartz_dir}", flush=True)
    if force or not quartz_dir.exists():
        print(f"  [git clone] {QUARTZ_REPO_URL} → {quartz_dir}", flush=True)
        print(f"  [git checkout] {QUARTZ_PINNED_COMMIT[:12]}", flush=True)
    else:
        print(f"  [skip clone] {quartz_dir} already exists", flush=True)
    print(f"  [overlay]  apply brain overlay → {quartz_dir}", flush=True)
    print(f"  [npm]      npm install in {quartz_dir}", flush=True)
    print(f"  [write]    {brain_home}/Caddyfile (port {port})", flush=True)
