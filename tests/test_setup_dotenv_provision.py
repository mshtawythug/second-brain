"""Regression tests for `$BRAIN_HOME/.env` provisioning (brain setup / brain init).

Bug (2026-08-07): nothing in `brain setup` / `brain init` ever created
``$BRAIN_HOME/.env``, the ONLY config location a pip / uvx install can see.
``~/.brain/`` itself already existed (``.shims/``, ``logs/``, ``run/``), so
directory existence looked like a healthy install while the config link of the
chain was dead — every ``brain`` invocation outside the source checkout failed.

Contract pinned here (:func:`brain.setup.provision_brain_home_dotenv`):

* fresh install  → a REAL file is written
* dev checkout   → a SYMLINK at the repo ``.env`` (never a copy: a copy would
  duplicate ``VOYAGE_API_KEY`` / ``DATABASE_URL`` on disk and silently drift)
* second run     → no-op, whatever is already there is left untouched
* broken link    → reported, never silently replaced

Everything runs against ``tmp_path``; no test touches the real ``~/.brain/`` or
the repo ``.env``.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brain import config as config_module
from brain.setup import (
    PROVISION_DANGLING,
    PROVISION_PRESENT,
    PROVISION_SYMLINKED,
    PROVISION_UNPROVISIONED,
    PROVISION_WRITTEN,
    provision_brain_home_dotenv,
)

_BODY = "DATABASE_URL=postgresql://u:p@localhost:55432/db\n"


@pytest.fixture
def no_project_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Fresh-install shape: the repo ``.env`` does not exist anywhere."""
    absent = tmp_path / "no-checkout" / ".env"
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: absent)
    return absent


@pytest.fixture
def project_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Dev-checkout shape: a repo ``.env`` exists at its own path."""
    repo_env = tmp_path / "checkout" / ".env"
    repo_env.parent.mkdir(parents=True)
    repo_env.write_text(_BODY)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: repo_env)
    return repo_env


# ---------------------------------------------------------------------------
# Fresh install — a real file
# ---------------------------------------------------------------------------


def test_fresh_install_writes_a_real_file(tmp_path: Path, no_project_dotenv: Path):
    """REGRESSION: no repo .env → setup writes a REAL $BRAIN_HOME/.env."""
    brain_home = tmp_path / "brain_home"

    result = provision_brain_home_dotenv(brain_home, body_factory=lambda: _BODY)

    assert result.action == PROVISION_WRITTEN
    assert result.path == brain_home / ".env"
    assert result.path.is_file() and not result.path.is_symlink()
    assert result.path.read_text() == _BODY


def test_fresh_install_creates_missing_brain_home(
    tmp_path: Path, no_project_dotenv: Path
):
    """A brain home that does not exist yet is created, not an error."""
    brain_home = tmp_path / "deep" / "brain_home"

    provision_brain_home_dotenv(brain_home, body_factory=lambda: _BODY)

    assert (brain_home / ".env").read_text() == _BODY


def test_no_body_factory_writes_nothing(tmp_path: Path, no_project_dotenv: Path):
    """`brain init` (body_factory=None) must never invent a config file.

    The template's DATABASE_URL may point at a different database than the one
    init was just told to migrate; a guessed file would then win over the
    environment in the next shell.
    """
    brain_home = tmp_path / "brain_home"

    result = provision_brain_home_dotenv(brain_home)

    assert result.action == PROVISION_UNPROVISIONED
    assert not (brain_home / ".env").exists()


# ---------------------------------------------------------------------------
# Dev checkout — a symlink
# ---------------------------------------------------------------------------


def test_dev_checkout_creates_symlink_not_copy(tmp_path: Path, project_dotenv: Path):
    """REGRESSION: repo .env present → $BRAIN_HOME/.env is a LINK to it."""
    brain_home = tmp_path / "brain_home"

    result = provision_brain_home_dotenv(brain_home, body_factory=lambda: "WRONG=1\n")

    assert result.action == PROVISION_SYMLINKED
    assert result.target == project_dotenv
    link = brain_home / ".env"
    assert link.is_symlink(), "must be a link — a copy duplicates secrets on disk"
    assert Path(os.readlink(link)) == project_dotenv
    assert link.read_text() == _BODY


def test_dev_checkout_symlink_created_without_a_body_factory(
    tmp_path: Path, project_dotenv: Path
):
    """`brain init` can link an existing checkout config even though it never writes."""
    brain_home = tmp_path / "brain_home"

    result = provision_brain_home_dotenv(brain_home)

    assert result.action == PROVISION_SYMLINKED
    assert (brain_home / ".env").is_symlink()


def test_never_self_links_when_brain_home_is_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """$BRAIN_HOME == the repo root: $BRAIN_HOME/.env IS the repo .env.

    The dev-backcompat branch of ``config._brain_home_root`` resolves BRAIN_HOME
    to the checkout, so there is nothing to link and a link would be a
    self-reference. Guarded by the ``pyproject.toml`` marker.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='brain'\n")
    elsewhere = tmp_path / "elsewhere" / ".env"
    elsewhere.parent.mkdir()
    elsewhere.write_text(_BODY)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: elsewhere)

    result = provision_brain_home_dotenv(checkout)

    assert result.action == PROVISION_UNPROVISIONED
    assert not (checkout / ".env").exists()


def test_repo_checkout_still_gets_a_real_env_when_setup_renders_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The checkout guard blocks LINKING only — `brain setup` can still write."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='brain'\n")
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: checkout / ".env")

    result = provision_brain_home_dotenv(checkout, body_factory=lambda: _BODY)

    assert result.action == PROVISION_WRITTEN
    assert (checkout / ".env").read_text() == _BODY


# ---------------------------------------------------------------------------
# Idempotence — never clobber
# ---------------------------------------------------------------------------


def test_second_run_leaves_a_real_file_untouched(
    tmp_path: Path, no_project_dotenv: Path
):
    """REGRESSION: re-running setup must be a no-op, not an overwrite."""
    brain_home = tmp_path / "brain_home"
    provision_brain_home_dotenv(brain_home, body_factory=lambda: _BODY)
    (brain_home / ".env").write_text("DATABASE_URL=postgresql://hand:edited@h:1/d\n")

    result = provision_brain_home_dotenv(brain_home, body_factory=lambda: _BODY)

    assert result.action == PROVISION_PRESENT
    assert "hand:edited" in (brain_home / ".env").read_text()


def test_second_run_leaves_an_existing_symlink_untouched(
    tmp_path: Path, project_dotenv: Path
):
    """A link created by the first run survives the second run unchanged."""
    brain_home = tmp_path / "brain_home"
    first = provision_brain_home_dotenv(brain_home)

    second = provision_brain_home_dotenv(brain_home)

    assert first.action == PROVISION_SYMLINKED
    assert second.action == PROVISION_PRESENT
    assert Path(os.readlink(brain_home / ".env")) == project_dotenv


def test_dangling_symlink_is_reported_not_replaced(
    tmp_path: Path, project_dotenv: Path
):
    """A moved checkout leaves a broken link — report it, never overwrite it.

    Overwriting would hide the move and strand the user's real config.
    """
    brain_home = tmp_path / "brain_home"
    brain_home.mkdir()
    gone = tmp_path / "moved-away" / ".env"
    (brain_home / ".env").symlink_to(gone)

    result = provision_brain_home_dotenv(brain_home, body_factory=lambda: _BODY)

    assert result.action == PROVISION_DANGLING
    assert result.target == gone
    assert (brain_home / ".env").is_symlink()
    assert Path(os.readlink(brain_home / ".env")) == gone


# ---------------------------------------------------------------------------
# End-to-end through run_setup
# ---------------------------------------------------------------------------


def _run_setup_state_creation(brain_home: Path) -> None:
    """Run a real (non-dry) setup with every external call mocked out."""
    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("brain.setup.ensure_shim"),
    ):
        from brain.setup import run_setup

        run_setup(
            profile="minimal",
            dry_run=False,
            non_interactive=True,
            brain_home_override=brain_home,
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )


def test_run_setup_provisions_brain_home_dotenv_on_fresh_install(
    tmp_path: Path, no_project_dotenv: Path
):
    """REGRESSION (end to end): `brain setup` leaves a usable $BRAIN_HOME/.env."""
    brain_home = tmp_path / "brain_home"

    _run_setup_state_creation(brain_home)

    env_file = brain_home / ".env"
    assert env_file.is_file()
    assert "DATABASE_URL=" in env_file.read_text()


def test_run_setup_links_dev_checkout_env(tmp_path: Path, project_dotenv: Path):
    """`brain setup` in a dev checkout links rather than duplicating the config."""
    brain_home = tmp_path / "brain_home"

    _run_setup_state_creation(brain_home)

    link = brain_home / ".env"
    assert link.is_symlink()
    assert Path(os.readlink(link)) == project_dotenv
    # The checkout's own config must be byte-identical — setup never edits it.
    assert project_dotenv.read_text() == _BODY


# ---------------------------------------------------------------------------
# `brain init` wiring
#
# init reaches its body only when Config.load() already succeeded, so a working
# config exists SOMEWHERE — but possibly only where the console script on PATH
# can never see it. init links the canonical $BRAIN_HOME/.env at it.
#
# DB-free by construction: the database layer is mocked out entirely. These
# assert the WIRING (init actually calls the provisioner and reports the
# outcome); the provisioning behaviour itself is covered above.
# ---------------------------------------------------------------------------


def _invoke_init(monkeypatch: pytest.MonkeyPatch, brain_home: Path):
    """Run `brain init` with the whole database layer mocked out."""
    from typer.testing import CliRunner

    from brain.cli import app

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/db")
    monkeypatch.setenv("BRAIN_HOME", str(brain_home))

    class _FakeEmbedder:
        dim = 1024

    conn = MagicMock()
    db = MagicMock()
    db.__enter__ = MagicMock(return_value=conn)
    db.__exit__ = MagicMock(return_value=False)

    with (
        patch("brain.cli.connect", return_value=db),
        patch("brain.cli.make_embedder", return_value=_FakeEmbedder()),
        patch("brain.cli.run_migrations", return_value=[]),
        patch("brain.cli.ensure_embedding_column"),
        patch("brain.cli.age_extension_available", return_value=False),
    ):
        return CliRunner().invoke(app, ["init"])


def test_init_links_brain_home_dotenv_at_dev_checkout_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_dotenv: Path
):
    """REGRESSION: init provisions the canonical $BRAIN_HOME/.env when missing."""
    brain_home = tmp_path / "brain_home"

    result = _invoke_init(monkeypatch, brain_home)

    assert result.exit_code == 0, result.output
    link = brain_home / ".env"
    assert link.is_symlink(), "init must provision $BRAIN_HOME/.env"
    assert Path(os.readlink(link)) == project_dotenv
    assert "linked" in result.output


def test_init_never_invents_a_dotenv_when_there_is_none_to_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_project_dotenv: Path
):
    """init must NOT render a template .env — its DATABASE_URL could be wrong.

    A guessed file would win over the environment in the next shell and
    silently point the CLI at a different database. Writing a real `.env` is
    `brain setup`'s job; it knows the port and the profile.
    """
    brain_home = tmp_path / "brain_home"

    result = _invoke_init(monkeypatch, brain_home)

    assert result.exit_code == 0, result.output
    assert not (brain_home / ".env").exists()


def test_init_reports_dangling_brain_home_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_project_dotenv: Path
):
    """A broken link is reported, never silently replaced."""
    brain_home = tmp_path / "brain_home"
    brain_home.mkdir()
    gone = tmp_path / "moved-away" / ".env"
    (brain_home / ".env").symlink_to(gone)

    result = _invoke_init(monkeypatch, brain_home)

    assert result.exit_code == 0, result.output
    assert "broken symlink" in result.output.lower()
    assert (brain_home / ".env").is_symlink()
    assert Path(os.readlink(brain_home / ".env")) == gone


def test_documented_remedy_repairs_a_dangling_link(
    tmp_path: Path, project_dotenv: Path
):
    """The `rm <path> && brain setup` remedy we print must actually work.

    Setup / init / doctor all print that exact command for a dangling link,
    precisely because re-running setup alone is a no-op. If removing the link
    did not then let provisioning succeed, every one of those messages would be
    sending the user in a circle.
    """
    brain_home = tmp_path / "brain_home"
    brain_home.mkdir()
    link = brain_home / ".env"
    link.symlink_to(tmp_path / "moved-away" / ".env")
    assert provision_brain_home_dotenv(brain_home).action == PROVISION_DANGLING

    link.unlink()  # the `rm <path>` half of the printed remedy
    result = provision_brain_home_dotenv(brain_home)

    assert result.action == PROVISION_SYMLINKED
    assert link.read_text() == _BODY
