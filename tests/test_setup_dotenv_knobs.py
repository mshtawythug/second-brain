"""``brain setup`` re-runs must honour knobs already in ``$BRAIN_HOME/.env``.

Same bug class as ``brain-mcp``'s log level: setup never calls
``Config.load``, so nothing publishes the dotenv chain into ``os.environ``, and
a knob read only from ``os.environ`` ignores the ``.env`` setup itself wrote on
the first run. Every test is a dry run: no file is written, nothing is exported.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from brain.setup import run_setup


def _unset_for_test(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Remove *name* from the env with a restore registered either way."""
    monkeypatch.setenv(name, "")
    monkeypatch.delenv(name)


def _existing_brain_home(tmp_path: Path, env_body: str) -> Path:
    """A ``$BRAIN_HOME`` left behind by an earlier ``brain setup``."""
    brain_home = tmp_path / ".brain"
    brain_home.mkdir()
    (brain_home / ".env").write_text(env_body, encoding="utf-8")
    return brain_home


def _dry_run_setup(brain_home: Path, **kwargs: object) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
    ):
        run_setup(
            dry_run=True,
            non_interactive=True,
            brain_home_override=brain_home,
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
            **kwargs,  # type: ignore[arg-type]
        )


def test_rerun_takes_vault_from_existing_brain_home_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: a re-run without ``--vault`` resolved ``~/brain-vault``.

    It then exported that path and handed it to the wiki install and the
    launchd plists, silently repointing an existing install at the default
    vault while its ``.env`` still named another.
    """
    custom_vault = tmp_path / "custom-vault"
    brain_home = _existing_brain_home(tmp_path, f"BRAIN_VAULT_PATH={custom_vault}\n")
    _unset_for_test(monkeypatch, "BRAIN_VAULT_PATH")

    _dry_run_setup(brain_home)

    out = capsys.readouterr().out
    assert f"would: export BRAIN_VAULT_PATH={custom_vault}" in out
    assert f"vault:      {custom_vault}" in out


def test_vault_process_env_beats_brain_home_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Precedence is unchanged above the new fallback: process env still wins."""
    from_env = tmp_path / "from-env"
    brain_home = _existing_brain_home(
        tmp_path, f"BRAIN_VAULT_PATH={tmp_path / 'from-file'}\n"
    )
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(from_env))

    _dry_run_setup(brain_home)

    assert f"vault:      {from_env}" in capsys.readouterr().out


def test_vault_override_beats_brain_home_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--vault`` still outranks everything, the ``.env`` included."""
    override = tmp_path / "override"
    brain_home = _existing_brain_home(
        tmp_path, f"BRAIN_VAULT_PATH={tmp_path / 'from-file'}\n"
    )
    _unset_for_test(monkeypatch, "BRAIN_VAULT_PATH")

    _dry_run_setup(brain_home, vault_override=override)

    assert f"vault:      {override}" in capsys.readouterr().out


def test_rerun_accepts_voyage_key_from_existing_brain_home_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: the key setup itself appended to ``.env`` was not seen.

    ``--non-interactive`` refused with "requires VOYAGE_API_KEY"; an
    interactive re-run prompted again and appended a duplicate line.
    """
    brain_home = _existing_brain_home(tmp_path, "VOYAGE_API_KEY=synthetic-key\n")
    _unset_for_test(monkeypatch, "VOYAGE_API_KEY")

    _dry_run_setup(brain_home, embedder_choice="voyage")


def test_voyage_key_missing_everywhere_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fallback must not turn a genuinely missing key into a pass."""
    brain_home = _existing_brain_home(tmp_path, "DATABASE_URL=postgresql://x\n")
    _unset_for_test(monkeypatch, "VOYAGE_API_KEY")

    with pytest.raises(typer.Exit) as excinfo:
        _dry_run_setup(brain_home, embedder_choice="voyage")

    assert excinfo.value.exit_code == 1
    assert "requires VOYAGE_API_KEY" in capsys.readouterr().err
