"""``brain.install_config``: the explicit vault leg and the shell entry point.

The precedence itself is covered end-to-end in
``tests/test_install_launchd_vault.py`` and ``tests/test_setup_dotenv_knobs.py``.
This file pins the two pieces the shell tooling depends on: an explicit
``--vault ~/x`` is expanded like the other legs, and
``python -m brain.install_config BRAIN_HOME`` prints the same vault the
installers bake into the plists. ``HOME``, ``BRAIN_HOME`` and the default vault
are fenced into ``tmp_path``; nothing reads the real vault.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from brain.install_config import main, resolve_install_vault


@pytest.fixture
def fenced_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Fence ``HOME``, the default vault and ``BRAIN_VAULT_PATH``; return a ``$BRAIN_HOME``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("brain.config.DEFAULT_VAULT_PATH", tmp_path / "default-vault")
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    brain_home = tmp_path / "brain-home"
    brain_home.mkdir()
    return brain_home


def test_an_explicit_tilde_override_is_expanded(fenced_home: Path, tmp_path: Path) -> None:
    """Regression: ``brain setup --vault '~/x'`` must not carry a literal ``~`` onward.

    The quoted form reaches Typer unexpanded. The env and dotenv legs already
    expanded; the explicit leg returned the override verbatim, so the literal
    ``~`` was written into ``.env`` and baked into the plists.
    """
    resolved = resolve_install_vault(fenced_home, override=Path("~/custom-vault"))

    assert resolved == tmp_path / "home" / "custom-vault"


def test_main_prints_the_brain_home_dotenv_vault(
    fenced_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shell entry point prints the vault ``$BRAIN_HOME/.env`` names, expanded."""
    (fenced_home / ".env").write_text("BRAIN_VAULT_PATH=~/from-file\n", encoding="utf-8")

    code = main([str(fenced_home)])

    assert code == 0
    assert capsys.readouterr().out == f"{tmp_path / 'home' / 'from-file'}\n"


def test_main_expands_a_tilde_brain_home(
    fenced_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``~/…`` ``BRAIN_HOME`` argument is expanded, as ``_brain_home_root`` does.

    Unexpanded, ``Path("~/brain-home") / ".env"`` is a relative path under the
    cwd, the ``.env`` is never found and the default is printed instead.
    """
    tilde_home = tmp_path / "home" / "brain-home"
    tilde_home.mkdir()
    (tilde_home / ".env").write_text("BRAIN_VAULT_PATH=/synthetic/tilde-vault\n", encoding="utf-8")

    code = main(["~/brain-home"])

    assert code == 0
    assert capsys.readouterr().out == "/synthetic/tilde-vault\n"


def test_main_prints_the_default_when_nothing_is_configured(
    fenced_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No env, no ``.env`` value: the packaged default, read at call time."""
    code = main([str(fenced_home)])

    assert code == 0
    assert capsys.readouterr().out == f"{tmp_path / 'default-vault'}\n"


@pytest.mark.parametrize("argv", [[], ["a", "b"]])
def test_main_rejects_a_wrong_argument_count(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Exactly one ``BRAIN_HOME`` argument; anything else is a usage error on stderr."""
    code = main(argv)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "usage: python -m brain.install_config BRAIN_HOME" in captured.err


def test_module_entry_point_runs_as_a_subprocess(tmp_path: Path) -> None:
    """``python -m brain.install_config`` is how the bash shims call it."""
    brain_home = tmp_path / "brain-home"
    brain_home.mkdir()
    vault = tmp_path / "configured-vault"
    (brain_home / ".env").write_text(f"BRAIN_VAULT_PATH={vault}\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}

    result = subprocess.run(  # noqa: S603 — list-form, fixed interpreter
        [sys.executable, "-m", "brain.install_config", str(brain_home)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{vault}\n"
