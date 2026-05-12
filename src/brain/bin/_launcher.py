"""Shared shim-install drift-protection and execvpe helper for brain-* console scripts."""
import contextlib
import hashlib
import os
import sys
import tempfile
from collections.abc import Sequence
from importlib.resources import files as resource_files
from pathlib import Path
from typing import NoReturn

from ..config import _brain_home_root


def _sha256(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def ensure_shim(name: str, brain_home: Path) -> Path:
    """Ensure $BRAIN_HOME/.shims/<name> is installed and up-to-date.

    Copies from package data brain.templates.bin/<name>.sh, stripping the
    .sh suffix at the installed path. Idempotent — safe to re-run.

    Drift protection: if the installed shim's sha256 differs from the
    package-data source, atomically replace via unique same-dir tmpfile
    + os.replace. Concurrent-launcher races are safe because tempfile
    names are unique.

    Shims live under .shims/ (not bin/) to avoid colliding with the
    dev-checkout bin/ wrappers when $BRAIN_HOME resolves to the repo
    root. A pipx-installed user has $BRAIN_HOME = ~/.brain so .shims/
    is fresh under that root; either way the shim location is
    repo-bin-independent.

    Returns the installed shim path.
    """
    # Read source bytes from package data (brain.templates.bin/<name>.sh).
    src = resource_files("brain.templates.bin") / f"{name}.sh"
    src_bytes: bytes = src.read_bytes()
    src_hash = _sha256(src_bytes)

    # Target path: <brain_home>/.shims/<name>  (no .sh suffix).
    bin_dir = brain_home / ".shims"
    bin_dir.mkdir(parents=True, exist_ok=True)
    installed_path = bin_dir / name

    # Check if the installed shim already exists and is up-to-date.
    if installed_path.exists():
        installed_bytes = installed_path.read_bytes()
        if _sha256(installed_bytes) == src_hash:
            # Already up-to-date — no-op (preserves mtime for dev workflows).
            return installed_path
        # Stale — atomically replace.
        _atomic_write(src_bytes, installed_path, bin_dir)
        print(f"shim updated: {name} (sha mismatch)", file=sys.stderr)
    else:
        # Fresh install.
        _atomic_write(src_bytes, installed_path, bin_dir)
        print(f"shim installed: {name}", file=sys.stderr)

    return installed_path


def _atomic_write(data: bytes, dest: Path, parent_dir: Path) -> None:
    """Write *data* to *dest* atomically via a unique same-dir tmpfile.

    Uses ``tempfile.NamedTemporaryFile(dir=parent_dir, delete=False)`` so the
    temp file is on the same filesystem as *dest* — required for ``os.replace``
    to be atomic. The random suffix from NamedTemporaryFile makes concurrent
    launchers race-safe (each gets its own tmpfile; last ``os.replace`` wins
    with the correct content).

    On any failure after the tmpfile is created, the tmpfile is unlinked to
    avoid leaving stale files in *parent_dir*.
    """
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=parent_dir, delete=False) as tf:
            tmp_path = tf.name
            tf.write(data)
        Path(tmp_path).chmod(0o755)
        os.replace(tmp_path, dest)
        tmp_path = None  # os.replace succeeded — no cleanup needed.
    except Exception:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        raise


def exec_shim(name: str, args: Sequence[str]) -> NoReturn:
    """Resolve $BRAIN_HOME, ensure the shim is installed/up-to-date, then
    os.execvpe it with BRAIN_PY set in the env.

    BRAIN_PY defaults to ``sys.executable`` (the interpreter running this
    launcher — by construction the one that has ``brain`` importable).
    An existing BRAIN_PY env var is preserved untouched so tests can stub
    the interpreter (see ``tests/test_bin_scripts.py``) and advanced users
    can pin a specific Python without editing the launcher.
    """
    brain_home = _brain_home_root()
    shim = ensure_shim(name, brain_home)
    env = dict(os.environ)
    env.setdefault("BRAIN_PY", sys.executable)
    os.execvpe(str(shim), [str(shim), *args], env)
