"""Python launchd plist generator for brain-install-launchd / brain-uninstall-launchd.

Renders the plist.j2 templates in brain.templates.launchd via manual str.replace
with xml.sax.saxutils.escape so paths containing &/</>  can't produce invalid
XML. Resolves PIPX_BIN_DIR via `pipx environment --value` with a ~/.local/bin
fallback. Resolves the Python interpreter via sys.executable — the running brain
process is by definition the right Python.

Entry points (registered in pyproject.toml by T1.8):
    brain-install-launchd  → install_main()
    brain-uninstall-launchd → uninstall_main()
"""

import contextlib
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from importlib.resources import files as resource_files
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from ..config import _brain_home_root
from ..errors import BrainError
from ..install_config import resolve_install_vault
from ._launcher import ensure_shim

# The launchd labels brain installs. watcher is the KeepAlive vault-sync daemon;
# brief (Plan 01) is a one-shot StartCalendarInterval job (07:00 daily).
_INSTALLED_LABELS = ("com.brain.watcher", "com.brain.brief")
# Retired at R2 (wiki-to-ui phase 8): the Quartz rebuild daemon. It is never
# installed again; install_plists boots out and deletes a leftover one, and
# uninstall still sweeps it. Its template and _brain-build-fg wrapper stay until R3.
_RETIRED_LABELS = ("com.brain.build",)
# Every label brain has ever installed: what uninstall_plists sweeps.
_ALL_LABELS = _INSTALLED_LABELS + _RETIRED_LABELS


class LaunchdError(BrainError):
    """Raised when a launchd plist render or launchctl operation fails."""


def resolve_launchctl() -> str:
    """The ``launchctl`` to run: ``$BRAIN_LAUNCHCTL``, else the bare name on ``PATH``.

    The one place this default is resolved. ``install_plists`` and
    ``uninstall_plists`` default to it rather than to a literal, so a direct call
    honours the seam exactly as the entry points, ``brain setup`` and
    ``brain doctor`` do. A literal default never read the environment, so a
    caller that omitted ``launchctl=`` ran the real binary whatever
    ``BRAIN_LAUNCHCTL`` said, including from inside the test suite.
    """
    return os.environ.get("BRAIN_LAUNCHCTL") or "launchctl"


# ---------------------------------------------------------------------------
# Public helpers (test-visible)
# ---------------------------------------------------------------------------


def resolve_pipx_bin_dir() -> Path:
    """Return the pipx binary directory.

    Calls ``pipx environment --value PIPX_BIN_DIR``; falls back to
    ``~/.local/bin`` if pipx is not on PATH or the command fails.
    """
    try:
        result = subprocess.run(
            ["pipx", "environment", "--value", "PIPX_BIN_DIR"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        raw = result.stdout.strip()
        if raw:
            return Path(raw)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return Path.home() / ".local" / "bin"


def resolve_brain_py() -> Path:
    """Resolve the Python interpreter that has ``brain`` importable.

    ``BRAIN_PY`` wins when set; ``sys.executable`` is the fallback. This is the
    same contract ``brain.bin._launcher.exec_shim`` already documents, which
    until now this function silently disagreed with.

    The override matters because ``sys.executable`` alone makes it impossible to
    generate plists for any interpreter except the installer's own. Running
    ``brain-install-launchd`` from a dev checkout would therefore repoint the
    user's live LaunchAgents at the checkout — swapping a released install for
    uncommitted code as a side effect of regenerating a plist. With the override
    you can render plists for the real target:

        BRAIN_PY=~/.local/share/uv/tools/secondbrain-py/bin/python \\
            brain-install-launchd
    """
    override = os.environ.get("BRAIN_PY", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(sys.executable)


def render_plist(
    label: str,
    brain_home: Path,
    vault_path: Path,
    pipx_bin_dir: Path,
    brain_py: Path,
) -> str:
    """Render the .plist.j2 template for *label* into a string.

    XML-escapes every substituted value so the resulting document is always
    valid XML, even when paths contain ``&``, ``<``, or ``>``.

    Raises :class:`LaunchdError` if the rendered XML does not parse.
    """
    template_ref = resource_files("brain.templates.launchd") / f"{label}.plist.j2"
    text: str = template_ref.read_text(encoding="utf-8")

    substitutions: dict[str, str] = {
        "brain_home": xml_escape(str(brain_home)),
        "user_home": xml_escape(str(Path.home())),
        "vault_path": xml_escape(str(vault_path)),
        "pipx_bin_dir": xml_escape(str(pipx_bin_dir)),
        "brain_py": xml_escape(str(brain_py)),
    }
    for key, value in substitutions.items():
        text = text.replace("{{ " + key + " }}", value)

    # Sanity-check: validate that the result is well-formed XML.
    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        raise LaunchdError(f"rendered plist for {label} is not valid XML: {exc}") from exc

    return text


def install_plists(
    brain_home: Path,
    launchd_dir: Path,
    launchctl: str | None = None,
    vault_path: Path | None = None,
) -> None:
    """Write the installed jobs' plists and load them into launchd. Idempotent.

    First, each retired label (``com.brain.build`` at R2) is booted out, errors
    suppressed because "not loaded" is its normal answer, and its plist deleted.
    That is the R2 migration, and it runs on every install.

    Then, for each installed label:
    1. Render the plist template.
    2. Write to *launchd_dir*/<label>.plist atomically.
    3. ``launchctl bootout`` (suppress errors — idempotent).
    4. ``launchctl bootstrap`` (fail loudly on error).

    ``launchctl`` defaults to :func:`resolve_launchctl`, never a literal. So
    does ``vault_path``: it defaults to
    :func:`brain.install_config.resolve_install_vault` (process env, then
    ``$BRAIN_HOME/.env``, then the default) -- the precedence ``brain setup``
    uses. The literal ``~/brain-vault`` it replaced read no config at all, and
    ``install_main`` (run by every ``brain-up``) passes no vault, so a custom
    vault was rewritten to the default on the next ``brain-up``.
    """
    launchctl = launchctl if launchctl is not None else resolve_launchctl()
    launchd_dir.mkdir(parents=True, exist_ok=True)

    # Install the foreground wrapper scripts the plists reference.
    # ensure_shim() is idempotent — skips the write if the shim is current.
    for wrapper in ("_brain-watcher-fg", "_brain-brief-fg"):
        ensure_shim(wrapper, brain_home)

    effective_vault_path = resolve_install_vault(brain_home, override=vault_path)
    pipx_bin_dir = resolve_pipx_bin_dir()
    brain_py = resolve_brain_py()
    uid: int = os.getuid()

    for label in _RETIRED_LABELS:
        subprocess.run(
            [launchctl, "bootout", f"gui/{uid}/{label}"],
            check=False,
            capture_output=True,
        )
        (launchd_dir / f"{label}.plist").unlink(missing_ok=True)

    for label in _INSTALLED_LABELS:
        plist_content = render_plist(
            label,
            brain_home=brain_home,
            vault_path=effective_vault_path,
            pipx_bin_dir=pipx_bin_dir,
            brain_py=brain_py,
        )
        plist_path = launchd_dir / f"{label}.plist"

        # Atomic write via same-dir tempfile + os.replace.
        _atomic_write_text(plist_content, plist_path, launchd_dir)

        domain = f"gui/{uid}/{label}"

        # Bootout any prior incarnation — suppress errors when not loaded.
        subprocess.run(
            [launchctl, "bootout", domain],
            check=False,
            capture_output=True,
        )

        # Bootstrap — fail loudly if this step fails. Retry once on the
        # "Bootstrap failed: 5: Input/output error" race that happens when
        # the prior service hasn't fully torn down before the new one tries
        # to load (observed on Apple Silicon under load).
        for attempt in range(2):
            result = subprocess.run(
                [launchctl, "bootstrap", f"gui/{uid}", str(plist_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            if attempt == 0:
                time.sleep(1.0)
                continue
            stderr = result.stderr.strip()
            raise LaunchdError(
                f"launchctl bootstrap failed for {label}"
                + (f": {stderr}" if stderr else "")
            )


def uninstall_plists(
    launchd_dir: Path,
    launchctl: str | None = None,
) -> None:
    """Bootout and remove every brain plist, retired ones included. Idempotent.

    For each label in ``_ALL_LABELS``:
    1. Probe ``launchctl print gui/<uid>/<label>`` — if loaded, bootout.
    2. Remove the plist file if it exists.
    3. Print a "nothing to remove" message if neither action was taken.

    ``launchctl`` defaults to :func:`resolve_launchctl`, never a literal.
    """
    launchctl = launchctl if launchctl is not None else resolve_launchctl()
    uid: int = os.getuid()

    for label in _ALL_LABELS:
        plist_path = launchd_dir / f"{label}.plist"
        domain = f"gui/{uid}/{label}"
        did_something = False

        # Check if the service is currently loaded.
        probe = subprocess.run(
            [launchctl, "print", domain],
            check=False,
            capture_output=True,
        )
        if probe.returncode == 0:
            subprocess.run(
                [launchctl, "bootout", domain],
                check=False,
                capture_output=True,
            )
            did_something = True

        if plist_path.exists():
            plist_path.unlink()
            did_something = True

        if not did_something:
            print(f"{label}: nothing to remove")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def install_main() -> None:
    """Entry point: brain-install-launchd."""
    launchd_dir = Path(
        os.environ.get("BRAIN_LAUNCHD_DIR") or Path.home() / "Library" / "LaunchAgents"
    )
    launchctl = resolve_launchctl()
    brain_home = _brain_home_root()
    install_plists(brain_home, launchd_dir, launchctl)
    print(f"🧠 brain LaunchAgents installed in {launchd_dir}")


def uninstall_main() -> None:
    """Entry point: brain-uninstall-launchd."""
    launchd_dir = Path(
        os.environ.get("BRAIN_LAUNCHD_DIR") or Path.home() / "Library" / "LaunchAgents"
    )
    launchctl = resolve_launchctl()
    uninstall_plists(launchd_dir, launchctl)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
