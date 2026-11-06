"""Fail when a wheel built from this repository is missing a file from a shipped data tree.

The trees are :data:`SHIPPED_TREES`: ``ui/static`` and, since RM-53, ``resources``. The
``package-data`` globs do not cross ``/``, and every guard in the test suite reads
the source tree, so a nested asset no glob reaches passes them all until someone
installs the wheel. This builds a real one from a clean copy (an in-place build reuses a
stale ``build/``, which can hide exactly this) and compares it with the trees.

Exit codes: 0 every file is in the wheel; 1 some are missing (each is named); 2 the
check could not run. Runs in ``ci.yml``'s ``lint`` job and in ``bin/brain-ci``.

The build backend is code resolved from the index, so it runs with a minimal
environment, in its own session so a timeout can stop all of it. Its own session also
means a SIGTERM or SIGHUP to this script can leave a hung build and the temporary
directory behind. That is accepted: it is local, and neither holds a secret.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

WHEEL_STATIC_PREFIX = "brain/ui/static/"
#: Each data tree a wheel must carry whole: its path in the repository, its prefix in the wheel.
SHIPPED_TREES = (
    ("src/brain/ui/static", WHEEL_STATIC_PREFIX),
    ("src/brain/resources", "brain/resources/"),
)
BUILD_TIMEOUT_S = 300.0
TAIL_BYTES = 4000
_NOT_SHIPPED = {"__pycache__", ".DS_Store"}
_BUILD_INPUTS = ("pyproject.toml", "README.md", "LICENSE")

#: What the child inherits, and nothing else. ``LD_LIBRARY_PATH``: setup-python's
#: interpreter links a shared libpython. Deliberately absent: ``PIP_INDEX_URL`` and the
#: proxy variables, whose values can embed credentials. A mirror or proxy belongs in
#: ``pip.conf``, which pip finds through ``HOME`` or ``PIP_CONFIG_FILE``.
_CHILD_ENV_NAMES = (
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "XDG_CONFIG_HOME",
    "PIP_CONFIG_FILE", "PIP_CERT", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
)

#: Build output is not trusted: anything but printable ASCII and plain whitespace
#: becomes ``?``, so no escape sequence reaches the caller's terminal.
_UNPRINTABLE_RE = re.compile(r"[^\t\n\x20-\x7e]")


class WheelCheckError(Exception):
    """The check could not run, as distinct from finding a file missing."""


def child_env() -> dict[str, str]:
    """The parts of this process's environment the build may see."""
    return {name: os.environ[name] for name in _CHILD_ENV_NAMES if name in os.environ}


def log_tail(log: Path) -> str:
    """The last :data:`TAIL_BYTES` of ``log``, made safe to print."""
    try:
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - TAIL_BYTES))
            raw = handle.read(TAIL_BYTES)
    except OSError:
        return ""
    return _UNPRINTABLE_RE.sub("?", raw.decode("ascii", errors="replace"))


def kill_group(process: subprocess.Popen[bytes]) -> None:
    """Stop the whole build. Its session leader's pid is its group id.

    Gone is quiet, and so is a group of only unreaped exits: macOS answers ``killpg``
    on one with ``EPERM``. Same rule as ``brain.ingest.capped_process.kill_group``, which
    is not imported because importing it pulls in the whole ingest package.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):  # gone, or only zombies
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def run_build(argv: list[str], log: Path, *, timeout: float = BUILD_TIMEOUT_S) -> int:
    """Run ``argv`` with its output in ``log``; return its exit status."""
    with log.open("wb") as output:
        process = subprocess.Popen(
            argv,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=child_env(),
            start_new_session=True,
        )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_group(process)
        raise WheelCheckError(
            f"the wheel build timed out after {timeout:g}s; last output:\n{log_tail(log)}"
        ) from exc
    except KeyboardInterrupt:
        # Its own session means the terminal's Ctrl-C never reached it.
        kill_group(process)
        raise


def _static_files(static_root: Path) -> set[str]:
    """Every file under ``static_root`` that a wheel should carry, relative to it."""
    return {
        path.relative_to(static_root).as_posix()
        for path in static_root.rglob("*")
        if path.is_file() and not _NOT_SHIPPED & set(path.parts)
    }


def missing_from_wheel(
    static_root: Path, wheel: Path, prefix: str = WHEEL_STATIC_PREFIX
) -> list[str]:
    """Paths under ``static_root``, relative to it, that ``wheel`` lacks under ``prefix``."""
    tree = _static_files(static_root)
    if not tree:
        raise WheelCheckError("no static files in the tree")
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise WheelCheckError("the build output is not a readable wheel") from exc
    return sorted(name for name in tree if f"{prefix}{name}" not in names)


def build_wheel(repo_root: Path, workdir: Path, build: Callable[[list[str], Path], int]) -> Path:
    """Copy the build inputs into ``workdir``, build there, and return the one wheel."""
    source = workdir / "source"
    source.mkdir()
    for name in _BUILD_INPUTS:
        shutil.copy2(repo_root / name, source / name)
    ignore = shutil.ignore_patterns("*.egg-info", *_NOT_SHIPPED)
    shutil.copytree(repo_root / "src", source / "src", ignore=ignore)
    out = workdir / "out"
    log = workdir / "build.log"
    pip = [sys.executable, "-m", "pip", "wheel", "--no-input", "--no-color"]
    status = build([*pip, "--no-deps", "--wheel-dir", str(out), str(source)], log)
    if status != 0:
        tail = log_tail(log)
        raise WheelCheckError(f"the wheel build failed (exit {status}); last output:\n{tail}")
    wheels = sorted(out.glob("*.whl")) if out.is_dir() else []
    if len(wheels) != 1:
        raise WheelCheckError(f"the build did not produce exactly one wheel ({len(wheels)})")
    return wheels[0]


def main(repo_root: Path, build: Callable[[list[str], Path], int] = run_build) -> int:
    """Build, compare, report. See the module docstring for the exit codes."""
    try:
        with tempfile.TemporaryDirectory() as workdir:
            wheel = build_wheel(repo_root, Path(workdir), build)
            missing = [
                f"{prefix}{name}"
                for tree, prefix in SHIPPED_TREES
                for name in missing_from_wheel(repo_root / tree, wheel, prefix)
            ]
    except (WheelCheckError, OSError) as exc:
        message = str(exc) if isinstance(exc, WheelCheckError) else "the build could not run"
        print(f"check_wheel_static: {message}", file=sys.stderr)
        return 2
    if missing:
        print("check_wheel_static: the wheel is missing these files:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        print("Add a package-data glob for them in pyproject.toml.", file=sys.stderr)
        return 1
    count = sum(len(_static_files(repo_root / tree)) for tree, _prefix in SHIPPED_TREES)
    print(f"check_wheel_static: the wheel carries all {count} shipped files")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(__file__).resolve().parent.parent))
