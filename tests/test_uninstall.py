"""Tests for brain uninstall (T4.3).

Tests cover:
1. Default run keeps data/postgres intact while removing runtime files.
2. --remove-db requires typed confirmation "yes, delete my data"; wrong answer aborts.
3. --remove-vault removes the vault; absence of flag keeps it.
4. launchd uninstall_main is called on macOS.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

# ---------------------------------------------------------------------------
# Helper — run run_uninstall with pre-populated directories
# ---------------------------------------------------------------------------


def _build_brain_home(tmp_path: Path) -> Path:
    """Create a typical $BRAIN_HOME skeleton under tmp_path."""
    home = tmp_path / ".brain"
    # data/postgres (should survive by default)
    (home / "data" / "postgres").mkdir(parents=True)
    (home / "data" / "postgres" / "PG_VERSION").write_text("16")
    # safe-to-remove entries
    (home / ".env").write_text("DATABASE_URL=postgresql://localhost/brain\n")
    (home / ".shims").mkdir()
    (home / ".shims" / "brain-up").write_text("#!/usr/bin/env bash\n")
    (home / "Caddyfile").write_text("# caddy\n")
    (home / "docker-compose.yml").write_text("version: '3'\n")
    return home


def _subprocess_noop(*args: object, **kwargs: object) -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    return m


# ---------------------------------------------------------------------------
# Test 1 — default uninstall removes runtime files but keeps data/postgres
# ---------------------------------------------------------------------------


def test_uninstall_removes_brain_home_keeps_data_postgres(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    pg_marker = home / "data" / "postgres" / "PG_VERSION"
    assert pg_marker.exists()

    with patch("subprocess.run", side_effect=_subprocess_noop):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=False,
            remove_vault=False,
            brain_home=home,
            vault_path=tmp_path / "vault",
            _launchd_uninstall=MagicMock(),
        )

    # Runtime files must be gone.
    assert not (home / ".env").exists(), ".env should be removed"
    assert not (home / ".shims").exists(), ".shims/ should be removed"
    assert not (home / "Caddyfile").exists(), "Caddyfile should be removed"

    # Database data must survive.
    assert pg_marker.exists(), "data/postgres must NOT be removed without --remove-db"


# ---------------------------------------------------------------------------
# Test 2 — --remove-db requires the exact typed phrase; wrong answer aborts
# ---------------------------------------------------------------------------


def test_uninstall_remove_db_requires_typed_confirmation(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    pg_marker = home / "data" / "postgres" / "PG_VERSION"

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value="no"),  # wrong confirmation phrase
        pytest.raises((typer.Abort, SystemExit)),
    ):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=True,
            remove_vault=False,
            brain_home=home,
            vault_path=tmp_path / "vault",
            _launchd_uninstall=MagicMock(),
        )

    # Database data must survive the failed confirmation.
    assert pg_marker.exists(), "data/postgres must NOT be removed when confirmation fails"


def test_uninstall_remove_db_correct_phrase_removes_postgres(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    pg_dir = home / "data" / "postgres"
    assert pg_dir.exists()

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch("typer.prompt", return_value="yes, delete my data"),
    ):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=True,
            remove_vault=False,
            brain_home=home,
            vault_path=tmp_path / "vault",
            _launchd_uninstall=MagicMock(),
        )

    assert not pg_dir.exists(), "data/postgres must be removed when correct phrase given"


# ---------------------------------------------------------------------------
# Test 3 — --remove-vault removes vault; default keeps it
# ---------------------------------------------------------------------------


def test_uninstall_remove_vault_takes_explicit_flag(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# My note\n")

    # Without flag: vault survives.
    with patch("subprocess.run", side_effect=_subprocess_noop):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=False,
            remove_vault=False,
            brain_home=home,
            vault_path=vault,
            _launchd_uninstall=MagicMock(),
        )

    assert (vault / "note.md").exists(), "vault must NOT be removed without --remove-vault"

    # Rebuild brain home for the second call.
    home2 = _build_brain_home(tmp_path / "run2")

    # With flag: vault is removed.
    with patch("subprocess.run", side_effect=_subprocess_noop):
        run_uninstall(
            yes=True,
            remove_db=False,
            remove_vault=True,
            brain_home=home2,
            vault_path=vault,
            _launchd_uninstall=MagicMock(),
        )

    assert not vault.exists(), "vault must be removed when --remove-vault is given"


# ---------------------------------------------------------------------------
# Test 4 — launchd uninstall_main is called on macOS
# ---------------------------------------------------------------------------


def test_uninstall_runs_launchd_uninstall_on_macos(tmp_path: Path) -> None:
    home = _build_brain_home(tmp_path)
    called: list[int] = []

    def _fake_launchd() -> None:
        called.append(1)

    with (
        patch("subprocess.run", side_effect=_subprocess_noop),
        patch.object(sys, "platform", "darwin"),
    ):
        from brain.uninstall import run_uninstall

        run_uninstall(
            yes=True,
            remove_db=False,
            remove_vault=False,
            brain_home=home,
            vault_path=tmp_path / "vault",
            _launchd_uninstall=_fake_launchd,
        )

    assert called, "uninstall_main (launchd) must be called on macOS"
