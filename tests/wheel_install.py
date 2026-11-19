"""Test helper: simulate a non-editable (wheel) install of ``brain`` under ``tmp_path`` (F25)."""
from dataclasses import dataclass
from pathlib import Path

import pytest

from brain import config as config_module
from brain import install_paths


@dataclass(frozen=True)
class WheelInstall:
    """Paths of one simulated wheel install, all under ``tmp_path``.

    ``venv`` is the virtualenv root; ``package_dotenv`` is where
    ``config._project_dotenv()`` resolves for an installed package
    (``<venv>/lib/python3.11/.env``); ``home`` is ``$BRAIN_HOME``.
    """

    venv: Path
    package_dir: Path
    package_dotenv: Path
    home: Path

    def venv_files(self) -> set[Path]:
        """Every path under the virtualenv: compare before/after to catch stray writes."""
        return set(self.venv.rglob("*"))


def simulate_wheel_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WheelInstall:
    """Make this process behave as a non-editable install whose ``$BRAIN_HOME`` is in tmp_path.

    The package dir is ``<venv>/lib/python3.11/site-packages/brain`` with no
    ``pyproject.toml`` above it, and the checkout seam is driven through the
    REAL predicate against that layout, so the test proves the layout is
    classified as "not a checkout" rather than just asserting ``None``.
    ``HOME`` is pointed into tmp_path too, so ``~/.brain`` can never be real.
    """
    venv = tmp_path / "venv"
    package_dir = venv / "lib" / "python3.11" / "site-packages" / "brain"
    package_dir.mkdir(parents=True)
    fake_module = package_dir / "install_paths.py"
    assert install_paths.checkout_root_for(fake_module) is None
    home = tmp_path / "brain_home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    package_dotenv = venv / "lib" / "python3.11" / ".env"

    monkeypatch.setattr(
        install_paths, "source_checkout_root", lambda: install_paths.checkout_root_for(fake_module)
    )
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: package_dotenv)
    monkeypatch.setenv("BRAIN_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "fake_user_home"))
    monkeypatch.setenv("BRAIN_IGNORE_CWD_DOTENV", "1")
    monkeypatch.delenv("BRAIN_OWNER_PARTICIPANTS", raising=False)
    monkeypatch.chdir(cwd)
    return WheelInstall(
        venv=venv, package_dir=package_dir, package_dotenv=package_dotenv, home=home
    )
