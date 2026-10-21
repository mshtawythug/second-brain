"""``brain setup --vault '~/x'`` must persist an absolute vault path.

Quoting the argument stops the shell expanding ``~``, so Typer hands setup a
literal ``Path("~/x")``. Before the fix that literal reached ``.env`` verbatim:
every consumer that does not call ``expanduser`` itself — a shell ``[[ -d ]]``,
a raw ``os.environ`` read — then looked for a directory named ``~``.

Both ``.env`` write paths are covered: the fresh render from ``env.example`` and
the append to an existing ``.env`` that lacks the key. ``HOME`` is fenced into
``tmp_path`` so the expansion lands there; every external call is mocked with
the same shape as ``tests/test_setup.py``'s state-creation helper.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brain.setup import run_setup


def _run_setup_with_vault(brain_home: Path, vault_override: Path) -> None:
    """Run setup past ``.env`` provisioning with every external call mocked."""
    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("brain.setup.ensure_shim"),
        # Fresh-install shape: no project .env, so setup renders a real
        # $BRAIN_HOME/.env rather than symlinking at the checkout's.
        patch(
            "brain.config._project_dotenv",
            return_value=brain_home.parent / "no-such-project.env",
        ),
    ):
        run_setup(
            dry_run=False,
            non_interactive=True,
            brain_home_override=brain_home,
            vault_override=vault_override,
            pg_port=0,
            skip_skill=True,
        )


@pytest.fixture
def fenced_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``HOME`` at ``tmp_path/home`` and return it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    return home


def _vault_lines(env_file: Path) -> list[str]:
    """Every active ``BRAIN_VAULT_PATH=`` line in *env_file*."""
    return [
        line
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("BRAIN_VAULT_PATH=")
    ]


def test_a_quoted_tilde_vault_is_rendered_absolute_into_a_fresh_env(
    fenced_home: Path, tmp_path: Path
) -> None:
    """Fresh ``.env`` render: the activated line carries the expanded path."""
    brain_home = tmp_path / ".brain"

    _run_setup_with_vault(brain_home, Path("~/my-vault"))

    assert _vault_lines(brain_home / ".env") == [f"BRAIN_VAULT_PATH={fenced_home / 'my-vault'}"]


def test_a_quoted_tilde_vault_is_appended_absolute_to_an_existing_env(
    fenced_home: Path, tmp_path: Path
) -> None:
    """Existing ``.env`` without the key: the appended line is expanded too."""
    brain_home = tmp_path / ".brain"
    brain_home.mkdir()
    (brain_home / ".env").write_text("DATABASE_URL=postgresql://x\n", encoding="utf-8")

    _run_setup_with_vault(brain_home, Path("~/my-vault"))

    assert _vault_lines(brain_home / ".env") == [f"BRAIN_VAULT_PATH={fenced_home / 'my-vault'}"]
