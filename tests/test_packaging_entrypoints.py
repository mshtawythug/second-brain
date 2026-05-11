"""Regression: pyproject.toml [project.scripts] declares all 8 brain console scripts."""
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
