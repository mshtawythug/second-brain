"""Python launchd plist generator for brain-install-launchd / brain-uninstall-launchd.

Renders the plist.j2 templates in brain.templates.launchd via manual str.replace
with xml.sax.saxutils.escape so paths containing &/</>  can't produce invalid
XML. Resolves PIPX_BIN_DIR via `pipx environment --value` with a ~/.local/bin
fallback. Resolves the Python interpreter via sys.executable — the running brain
process is by definition the right Python.

Entry points (registered in pyproject.toml by T1.8):
    brain-install-launchd  → install_main()   [--with-reranker]
    brain-uninstall-launchd → uninstall_main()

Optional labels (RM-14b): ``com.brain.reranker`` is installed only by
``--with-reranker``, then kept by every later install, flagless included,
because ``brain-up`` runs this installer without flags on every start.
"""

import argparse
import contextlib
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from importlib.resources import files as resource_files
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from ..config import ConfigError, _brain_home_root
from ..errors import BrainError
from ..install_config import brain_home_dotenv_value, resolve_install_vault
from ..settings.rerank import (
    DEFAULT_RERANK_GGUF_NAME,
    MAX_LAUNCHD_PORT,
    MIN_LAUNCHD_PORT,
    launchd_rerank_port,
)
from ._launcher import ensure_shim

# The launchd labels brain installs. watcher is the KeepAlive vault-sync daemon;
# brief (Plan 01) is a one-shot StartCalendarInterval job (07:00 daily).
_INSTALLED_LABELS = ("com.brain.watcher", "com.brain.brief")
# Retired at R2 (wiki-to-ui phase 8): the Quartz rebuild daemon. It is never
# installed again; install_plists boots out and deletes a leftover one, and
# uninstall still sweeps it. Its template and _brain-build-fg wrapper were deleted
# with the wiki at R3 (phase 9); the sweep stays for upgraded machines (R9-1).
_RETIRED_LABELS = ("com.brain.build",)
# Opt-in agents (RM-14b). Each is installed only by its flag, then STICKY: a
# flagless install re-renders and re-bootstraps one whose plist exists, so a
# template change reaches it, and never removes it; only uninstall does. Not in
# _INSTALLED_LABELS, which doctor treats as agents that must be installed: doctor
# checks an optional one only when its plist exists.
_RERANKER_LABEL = "com.brain.reranker"
_OPTIONAL_LABELS = (_RERANKER_LABEL,)
# Every label brain has ever installed: what uninstall_plists sweeps.
_ALL_LABELS = _INSTALLED_LABELS + _OPTIONAL_LABELS + _RETIRED_LABELS

_GUIDE = "docs/guides/local-reranker.md"
_LEFTOVER_MARKER = re.compile(r"\{\{ *(\w+) *\}\}")


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


def resolve_rerank_model(brain_home: Path) -> Path:
    """The GGUF ``com.brain.reranker`` serves (RM-14b).

    ``BRAIN_RERANK_GGUF`` from the process env, then from ``$BRAIN_HOME/.env``
    (the installer never calls ``Config.load``; see
    :func:`brain.install_config.brain_home_dotenv_value`), then
    ``$BRAIN_HOME/models/<DEFAULT_RERANK_GGUF_NAME>``, where the guide puts it.
    """
    raw = os.environ.get("BRAIN_RERANK_GGUF") or brain_home_dotenv_value(
        brain_home, "BRAIN_RERANK_GGUF"
    )
    if raw:
        return Path(raw).expanduser()
    return brain_home / "models" / DEFAULT_RERANK_GGUF_NAME


def resolve_rerank_port(brain_home: Path) -> int:
    """The port ``com.brain.reranker`` binds: ``BRAIN_RERANK_URL``'s (RM-14b).

    Same precedence as :func:`resolve_rerank_model`: the process env, then
    ``$BRAIN_HOME/.env``, then the default URL (port 8081). A non-loopback host
    or a port outside [1024, 65535] is a :class:`LaunchdError` whose message
    names the variable, never its value. The host itself is not taken from the
    URL: the template binds a literal ``127.0.0.1``.
    """
    raw = os.environ.get("BRAIN_RERANK_URL") or brain_home_dotenv_value(
        brain_home, "BRAIN_RERANK_URL"
    )
    env = {"BRAIN_RERANK_URL": raw} if raw else {}
    try:
        return launchd_rerank_port(env)
    except ConfigError as exc:
        raise LaunchdError(f"com.brain.reranker: {exc}") from None


def render_plist(
    label: str,
    brain_home: Path,
    vault_path: Path,
    pipx_bin_dir: Path,
    brain_py: Path,
    rerank_model: Path | None = None,
    rerank_port: int | None = None,
) -> str:
    """Render the .plist.j2 template for *label* into a string.

    XML-escapes every substituted value so the resulting document is always
    valid XML, even when paths contain ``&``, ``<``, or ``>``.

    Raises :class:`LaunchdError` if the rendered XML does not parse, or if a
    ``{{ marker }}`` is left unsubstituted (``com.brain.reranker`` rendered
    without *rerank_model* would otherwise hand launchd a literal marker as
    the model path), or if *rerank_port* is outside [1024, 65535].
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
    if rerank_model is not None:
        substitutions["rerank_model_path"] = xml_escape(str(rerank_model))
    if rerank_port is not None:
        if not MIN_LAUNCHD_PORT <= rerank_port <= MAX_LAUNCHD_PORT:
            raise LaunchdError(
                f"rerank_port must be in [{MIN_LAUNCHD_PORT}, {MAX_LAUNCHD_PORT}]"
            )
        substitutions["rerank_port"] = str(rerank_port)
    for key, value in substitutions.items():
        text = text.replace("{{ " + key + " }}", value)
    leftover = _LEFTOVER_MARKER.search(text)
    if leftover:
        raise LaunchdError(f"rendered plist for {label} leaves {leftover.group(0)} unset")

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
    with_reranker: bool = False,
) -> None:
    """Write the installed jobs' plists and load them into launchd. Idempotent.

    ``with_reranker`` also installs the opt-in ``com.brain.reranker``, and is
    refused (before any launchctl call) when its GGUF is missing. Without it,
    an optional label is re-rendered and reloaded only when its plist already
    exists (sticky), with no file check: ``brain-up`` must not fail because a
    model moved, and ``brain doctor`` reports the dead agent.

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
    rerank_model = resolve_rerank_model(brain_home)
    if with_reranker and not rerank_model.is_file():
        raise LaunchdError(
            f"--with-reranker: no model at {rerank_model}. Download it, or set "
            f"BRAIN_RERANK_GGUF; see {_GUIDE}"
        )
    optional = tuple(
        label
        for label in _OPTIONAL_LABELS
        if (with_reranker and label == _RERANKER_LABEL)
        or (launchd_dir / f"{label}.plist").exists()
    )
    # Read only when the reranker job is rendered, so a bad URL cannot break an
    # install that never opted in; refused here, before any launchctl call.
    rerank_port = resolve_rerank_port(brain_home) if _RERANKER_LABEL in optional else None
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

    for label in (*_INSTALLED_LABELS, *optional):
        plist_content = render_plist(
            label,
            brain_home=brain_home,
            vault_path=effective_vault_path,
            pipx_bin_dir=pipx_bin_dir,
            brain_py=brain_py,
            rerank_model=rerank_model,
            rerank_port=rerank_port,
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


def install_main(argv: Sequence[str] | None = None) -> None:
    """Entry point: brain-install-launchd [--with-reranker].

    *argv* defaults to ``sys.argv[1:]``; tests pass a list (pytest's own
    arguments are in ``sys.argv``). ``bin/brain-install-launchd`` passes its
    arguments through, and ``brain-up`` passes none.
    """
    parser = argparse.ArgumentParser(
        prog="brain-install-launchd",
        description="Install brain's LaunchAgents. Idempotent; keeps opt-in agents installed.",
    )
    parser.add_argument(
        "--with-reranker",
        action="store_true",
        help=(
            "also install com.brain.reranker (llama-server on 127.0.0.1, at "
            f"BRAIN_RERANK_URL's port, 8081 by default); see {_GUIDE}"
        ),
    )
    args = parser.parse_args(argv)
    launchd_dir = Path(
        os.environ.get("BRAIN_LAUNCHD_DIR") or Path.home() / "Library" / "LaunchAgents"
    )
    launchctl = resolve_launchctl()
    brain_home = _brain_home_root()
    install_plists(brain_home, launchd_dir, launchctl, with_reranker=args.with_reranker)
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
