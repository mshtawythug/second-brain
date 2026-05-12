"""Regression: pyproject.toml [project.scripts] declares all 9 brain console scripts."""
import importlib
import os
import subprocess
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

EXPECTED_ENTRY_POINTS = {
    "brain": "brain.cli:app",
    "brain-mcp": "brain.mcp_server:main",
    "brain-up": "brain.bin.up:main",
    "brain-down": "brain.bin.down:main",
    "brain-status": "brain.bin.status:main",
    "brain-rebuild": "brain.bin.rebuild:main",
    "brain-install-launchd": "brain.bin.launchd:install_main",
    "brain-uninstall-launchd": "brain.bin.launchd:uninstall_main",
    "brain-monitor": "brain.bin.monitor:cli_main",
}


def test_all_entry_points_declared() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    actual = data["project"]["scripts"]
    assert actual == EXPECTED_ENTRY_POINTS


def test_entry_point_targets_are_importable() -> None:
    """Every declared entry-point target resolves to a real callable."""
    for name, target in EXPECTED_ENTRY_POINTS.items():
        module_path, attr = target.split(":")
        mod = importlib.import_module(module_path)
        callable_obj = getattr(mod, attr)
        assert callable(callable_obj), f"{name} target {target} is not callable"


_USER_FACING_WRAPPERS = [
    "brain-up",
    "brain-down",
    "brain-status",
    "brain-rebuild",
    "brain-install-launchd",
    "brain-uninstall-launchd",
    "brain-monitor",
]


def test_dev_backcompat_wrappers_exist_and_are_executable() -> None:
    """All user-facing bin/ scripts still exist as thin wrappers (post-T1.8 + T1.9)."""
    bin_dir = Path(__file__).resolve().parent.parent / "bin"
    for name in _USER_FACING_WRAPPERS:
        path = bin_dir / name
        assert path.is_file(), f"missing dev-checkout wrapper: {path}"
        assert os.access(path, os.X_OK), f"not executable: {path}"


def test_dev_backcompat_wrappers_exec_venv_not_path() -> None:
    """Wrappers must exec the venv console script by absolute path, not via PATH.

    When bin/ precedes .venv/bin/ on PATH (the normal dev-checkout state),
    `exec brain-up` would re-exec bin/brain-up itself — an infinite loop.
    The correct form is `exec "$SCRIPT_DIR/../.venv/bin/brain-<name>"`.
    """
    bin_dir = Path(__file__).resolve().parent.parent / "bin"
    for name in _USER_FACING_WRAPPERS:
        text = (bin_dir / name).read_text(encoding="utf-8")
        # Must reference the venv-relative path, not bare `exec brain-*`
        assert f".venv/bin/{name}" in text, (
            f"bin/{name} execs via PATH (infinite-loop risk when bin/ is on PATH "
            f"before .venv/bin/); use: exec \"$SCRIPT_DIR/../.venv/bin/{name}\" \"$@\""
        )
        # Must NOT contain `exec brain-` without a path prefix
        import re
        bare_exec = re.search(r'\bexec\s+brain-', text)
        assert bare_exec is None, (
            f"bin/{name} contains a bare `exec brain-*` which loops when bin/ precedes "
            f".venv/bin/ on PATH"
        )


def test_python_m_brain_entry_point() -> None:
    """Regression for brain/__main__.py: `python -m brain --help` must exit 0.

    brain.setup._run_brain_init / _run_brain_doctor invoke:
        subprocess.run([sys.executable, "-m", "brain", "init"], ...)
        subprocess.run([sys.executable, "-m", "brain", "doctor"], ...)

    Without brain/__main__.py Python rejects the -m flag with:
        "No module named brain.__main__; 'brain' is a package and cannot
         be directly executed"

    This test catches any future deletion of __main__.py before the CI gate
    would; the fix is to restore src/brain/__main__.py.
    """
    result = subprocess.run(
        [sys.executable, "-m", "brain", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"`python -m brain --help` exited {result.returncode}.\n"
        f"stderr: {result.stderr}\n"
        "Likely cause: src/brain/__main__.py is missing."
    )
    assert "brain" in result.stdout.lower(), (
        f"Expected 'brain' in --help output; got:\n{result.stdout}"
    )
