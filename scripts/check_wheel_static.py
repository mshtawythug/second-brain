"""Fail when a wheel built from this repository is missing a file from ``ui/static``.

The ``package-data`` globs do not cross ``/``, and every guard in the test suite reads
the source tree, so a nested asset no glob reaches passes them all until someone
installs the wheel. This builds a real one from a clean copy (an in-place build reuses a
stale ``build/``, which can hide exactly this) and compares it with the tree.

Exit codes: 0 every file is in the wheel; 1 some are missing (each is named); 2 the
check could not run. Runs in ``ci.yml``'s ``lint`` job and in ``bin/brain-ci``.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

WHEEL_STATIC_PREFIX = "brain/ui/static/"
BUILD_TIMEOUT_S = 300
_NOT_SHIPPED = {"__pycache__", ".DS_Store"}
_BUILD_INPUTS = ("pyproject.toml", "README.md", "LICENSE")


class WheelCheckError(Exception):
    """The check could not run, as distinct from finding a file missing."""


def _static_files(static_root: Path) -> set[str]:
    """Every file under ``static_root`` that a wheel should carry, relative to it."""
    return {
        path.relative_to(static_root).as_posix()
        for path in static_root.rglob("*")
        if path.is_file() and not _NOT_SHIPPED & set(path.parts)
    }


def missing_from_wheel(static_root: Path, wheel: Path) -> list[str]:
    """Paths under ``static_root``, relative to it, that ``wheel`` does not carry."""
    tree = _static_files(static_root)
    if not tree:
        raise WheelCheckError("no static files in the tree")
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise WheelCheckError("the build output is not a readable wheel") from exc
    return sorted(name for name in tree if f"{WHEEL_STATIC_PREFIX}{name}" not in names)


def build_wheel(repo_root: Path, workdir: Path, run: Callable[..., Any]) -> Path:
    """Copy the build inputs into ``workdir``, build there, and return the one wheel."""
    source = workdir / "source"
    source.mkdir()
    for name in _BUILD_INPUTS:
        shutil.copy2(repo_root / name, source / name)
    ignore = shutil.ignore_patterns("*.egg-info", *_NOT_SHIPPED)
    shutil.copytree(repo_root / "src", source / "src", ignore=ignore)
    out = workdir / "out"
    argv = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(out), str(source)]
    try:
        result = run(argv, capture_output=True, timeout=BUILD_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        raise WheelCheckError(f"the wheel build timed out after {BUILD_TIMEOUT_S}s") from exc
    if result.returncode != 0:
        raise WheelCheckError(f"the wheel build failed (exit {result.returncode})")
    wheels = sorted(out.glob("*.whl")) if out.is_dir() else []
    if len(wheels) != 1:
        raise WheelCheckError(f"the build did not produce exactly one wheel ({len(wheels)})")
    return wheels[0]


def main(repo_root: Path, run: Callable[..., Any] = subprocess.run) -> int:
    """Build, compare, report. See the module docstring for the exit codes."""
    static_root = repo_root / "src" / "brain" / "ui" / "static"
    try:
        with tempfile.TemporaryDirectory() as workdir:
            wheel = build_wheel(repo_root, Path(workdir), run)
            missing = missing_from_wheel(static_root, wheel)
    except (WheelCheckError, OSError) as exc:
        message = str(exc) if isinstance(exc, WheelCheckError) else "the build inputs are missing"
        print(f"check_wheel_static: {message}", file=sys.stderr)
        return 2
    if missing:
        print("check_wheel_static: the wheel is missing these static files:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        print("Add a package-data glob for them in pyproject.toml.", file=sys.stderr)
        return 1
    count = len(_static_files(static_root))
    print(f"check_wheel_static: the wheel carries all {count} static files")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(__file__).resolve().parent.parent))
