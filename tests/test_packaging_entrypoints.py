"""Regression: pyproject.toml [project.scripts] declares all 9 brain console scripts."""
import importlib
import os
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


def test_dev_backcompat_wrappers_exist_and_are_executable() -> None:
    """The 6 user-facing bin/ scripts still exist as thin wrappers (post-T1.8)."""
    bin_dir = Path(__file__).resolve().parent.parent / "bin"
    for name in [
        "brain-up",
        "brain-down",
        "brain-status",
        "brain-rebuild",
        "brain-install-launchd",
        "brain-uninstall-launchd",
    ]:
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
    for name in [
        "brain-up",
        "brain-down",
        "brain-status",
        "brain-rebuild",
        "brain-install-launchd",
        "brain-uninstall-launchd",
    ]:
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
