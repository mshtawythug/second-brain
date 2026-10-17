"""``brain setup`` re-runs must honour knobs already in ``$BRAIN_HOME/.env``.

Same bug class as ``brain-mcp``'s log level: setup never calls
``Config.load``, so nothing publishes the dotenv chain into ``os.environ``, and
a knob read only from ``os.environ`` ignores the ``.env`` setup itself wrote on
the first run. Every test is a dry run: no file is written, nothing is exported.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from dotenv import dotenv_values

from brain.setup import _brain_home_dotenv_value, run_setup


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
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)

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
    brain_home = _existing_brain_home(tmp_path, f"BRAIN_VAULT_PATH={tmp_path / 'from-file'}\n")
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
    brain_home = _existing_brain_home(tmp_path, f"BRAIN_VAULT_PATH={tmp_path / 'from-file'}\n")
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)

    _dry_run_setup(brain_home, vault_override=override)

    assert f"vault:      {override}" in capsys.readouterr().out


def test_rerun_accepts_voyage_key_from_existing_brain_home_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: the key setup itself appended to ``.env`` was not seen.

    ``--non-interactive`` refused with "requires VOYAGE_API_KEY"; an
    interactive re-run prompted again and appended a duplicate line.
    """
    brain_home = _existing_brain_home(tmp_path, "VOYAGE_API_KEY=synthetic-key\n")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    _dry_run_setup(brain_home, embedder_choice="voyage")

    assert "requires VOYAGE_API_KEY" not in capsys.readouterr().err


def test_voyage_key_missing_everywhere_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fallback must not turn a genuinely missing key into a pass."""
    brain_home = _existing_brain_home(tmp_path, "DATABASE_URL=postgresql://x\n")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    with pytest.raises(typer.Exit) as excinfo:
        _dry_run_setup(brain_home, embedder_choice="voyage")

    assert excinfo.value.exit_code == 1
    assert "requires VOYAGE_API_KEY" in capsys.readouterr().err


def test_voyage_key_is_not_looked_up_for_other_embedders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a voyage run reads the key: no second parse of ``.env`` otherwise.

    A malformed line makes every parse observable, because python-dotenv warns
    once per parse. A non-voyage run parses ``.env`` once, for the vault.
    """
    brain_home = _existing_brain_home(tmp_path, "this line does not parse\n")
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    with caplog.at_level(logging.WARNING, logger="dotenv.main"):
        _dry_run_setup(brain_home, embedder_choice="none")

    parse_warnings = [r for r in caplog.records if "could not parse" in r.getMessage()]
    assert len(parse_warnings) == 1


def test_reset_keeps_the_vault_named_in_the_old_brain_home_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--reset`` without ``--vault`` keeps the vault the old ``.env`` named.

    Decided in review: the vault resolves BEFORE the reset deletes
    ``$BRAIN_HOME``, and reset never deletes the vault, so the fresh config is
    pointed back at the user's existing vault rather than at ``~/brain-vault``.
    """
    custom_vault = tmp_path / "custom-vault"
    brain_home = _existing_brain_home(tmp_path, f"BRAIN_VAULT_PATH={custom_vault}\n")
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)

    with patch("typer.prompt", return_value="yes, delete my data"):
        _dry_run_setup(brain_home, reset=True)

    out = capsys.readouterr().out
    assert f"would: shutil.rmtree({brain_home})" in out
    assert f"vault:      {custom_vault}" in out


def test_real_reset_writes_the_old_dotenv_vault_into_the_fresh_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A NON-dry ``--reset`` must read the old ``.env`` BEFORE deleting it.

    The dry-run pin above cannot see the ordering: a dry run never deletes
    ``$BRAIN_HOME``, so the old ``.env`` is still there whenever the vault is
    resolved. Here the reset really runs ``shutil.rmtree``; resolving the vault
    after it would find no ``.env`` and render the default into the fresh one.

    Every path is under ``tmp_path``: ``$BRAIN_HOME``, the vault, ``HOME``, and
    the module's ``DEFAULT_VAULT_PATH``. So even a regressed order resolves a
    throwaway directory, never the real ``~/brain-vault``. External commands
    are stubbed as ``tests/test_setup.py`` stubs a non-dry run. The minimal
    profile offers no daemons, and ``BRAIN_LAUNCHD_DIR`` points into
    ``tmp_path`` as a second fence in case it ever does.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    tmp_default_vault = tmp_path / "default-vault"
    monkeypatch.setattr("brain.setup.DEFAULT_VAULT_PATH", tmp_default_vault)
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    monkeypatch.setenv("BRAIN_LAUNCHD_DIR", str(tmp_path / "LaunchAgents"))
    custom_vault = tmp_path / "custom-vault"
    brain_home = _existing_brain_home(
        tmp_path, f"BRAIN_VAULT_PATH={custom_vault}\nOLD_DOTENV_MARKER=1\n"
    )

    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("brain.setup.ensure_shim"),
        patch(
            "brain.config._project_dotenv",
            return_value=tmp_path / "no-such-project.env",
        ),
        patch("typer.prompt", return_value="yes, delete my data"),
        patch("time.sleep"),
    ):
        run_setup(
            profile="minimal",
            dry_run=False,
            non_interactive=True,
            reset=True,
            brain_home_override=brain_home,
            pg_port=0,
            embedder_choice="none",
            skip_wiki=True,
            skip_skill=True,
        )

    fresh = (brain_home / ".env").read_text(encoding="utf-8")
    assert "OLD_DOTENV_MARKER" not in fresh  # the reset really deleted it
    assert dotenv_values(brain_home / ".env").get("BRAIN_VAULT_PATH") == str(custom_vault)
    assert os.environ["BRAIN_VAULT_PATH"] == str(custom_vault)
    assert Path(os.environ["BRAIN_HOME"]).is_relative_to(tmp_path)
    assert not tmp_default_vault.exists()


def _dotenv_as_directory(brain_home: Path) -> None:
    (brain_home / ".env").mkdir()


def _dotenv_unreadable(brain_home: Path) -> None:
    path = brain_home / ".env"
    path.write_text("BRAIN_VAULT_PATH=/synthetic/vault\n", encoding="utf-8")
    path.chmod(0)


def _dotenv_not_utf8(brain_home: Path) -> None:
    (brain_home / ".env").write_bytes(b"BRAIN_VAULT_PATH=/synthetic/\xff\xfe\n")


@pytest.mark.parametrize(
    "make_bad_dotenv",
    [_dotenv_as_directory, _dotenv_unreadable, _dotenv_not_utf8],
    ids=["directory", "unreadable", "not-utf8"],
)
def test_unusable_brain_home_dotenv_reads_as_unset(
    tmp_path: Path, make_bad_dotenv: Callable[[Path], None]
) -> None:
    """A ``.env`` that cannot be read falls back (``None``); it never raises.

    Setup must keep going and resolve the default, not crash on a broken file
    it is about to report on anyway.
    """
    if make_bad_dotenv is _dotenv_unreadable and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    brain_home = tmp_path / ".brain"
    brain_home.mkdir()
    make_bad_dotenv(brain_home)
    try:
        assert _brain_home_dotenv_value(brain_home, "BRAIN_VAULT_PATH") is None
    finally:
        (brain_home / ".env").chmod(0o700)
