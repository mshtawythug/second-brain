"""Baseline tests for T3.1-T3.4 brain setup scaffold.

Three cases:
    1. dry_run=True — no filesystem side-effects, no subprocess.run called.
    2. --reset with wrong confirmation — aborts; existing data untouched.
    3. preflight fails when docker missing — exits non-zero with docker +
       Remediation in output.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exit_code(exc: BaseException) -> int:
    """Extract the exit code from a typer.Exit or SystemExit."""
    if isinstance(exc, typer.Exit):
        return exc.exit_code
    # SystemExit
    code = getattr(exc, "code", 1)
    return code if isinstance(code, int) else 1


# ---------------------------------------------------------------------------
# Test 1 — dry-run produces no filesystem side-effects and no subprocess calls
# ---------------------------------------------------------------------------


def test_setup_dry_run_no_side_effects(tmp_path: Path) -> None:
    """dry_run=True must not write any files and must not invoke subprocess.run."""
    brain_home = tmp_path / ".brain"

    subprocess_calls: list[Any] = []

    def _fake_run(*args: Any, **kwargs: Any) -> MagicMock:
        subprocess_calls.append(args)
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        return m

    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", side_effect=_fake_run),
    ):
        from brain.setup import run_setup

        run_setup(
            dry_run=True,
            non_interactive=True,
            brain_home_override=brain_home,
            # Use port 0 so the socket.bind probe always succeeds regardless of
            # whether the developer's local Postgres is already running on 5433.
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )

    # No directories or files should have been created under tmp.
    assert not brain_home.exists(), (
        "dry_run=True must not create brain_home or any subdirectories"
    )

    # No subprocess.run calls should have been made (preflight docker check
    # is skipped in dry_run, and all T3.4 startup steps are guarded by
    # _perform_action which no-ops in dry_run mode).
    assert subprocess_calls == [], (
        f"subprocess.run was called unexpectedly: {subprocess_calls}"
    )


# ---------------------------------------------------------------------------
# Test 2 — --reset with wrong confirmation leaves data intact
# ---------------------------------------------------------------------------


def test_setup_reset_requires_typed_confirmation(tmp_path: Path) -> None:
    """--reset with the wrong confirmation phrase must abort without deleting data."""
    brain_home = tmp_path / ".brain"
    pg_marker = brain_home / "data" / "postgres" / "marker"
    pg_marker.parent.mkdir(parents=True)
    pg_marker.write_text("precious postgres data")

    with (
        pytest.raises((typer.Exit, SystemExit)) as exc_info,
        patch("typer.prompt", return_value="no thanks"),
    ):
        from brain.setup import run_setup

        run_setup(
            reset=True,
            non_interactive=True,
            brain_home_override=brain_home,
            skip_wiki=True,
            skip_skill=True,
        )

    # Must exit non-zero.
    assert _exit_code(exc_info.value) != 0, (
        "setup with wrong reset confirmation must exit non-zero"
    )

    # Precious data must still exist — nothing was deleted.
    assert pg_marker.exists(), (
        "pg_marker was unexpectedly deleted despite wrong confirmation"
    )


# ---------------------------------------------------------------------------
# Test 3 — preflight fails with clear error when docker is missing
# ---------------------------------------------------------------------------


def test_setup_preflight_fails_when_docker_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preflight must exit non-zero and mention 'docker' + 'Remediation' when docker absent."""
    brain_home = tmp_path / ".brain"

    def _fake_which(name: str) -> str | None:
        if name == "docker":
            return None  # simulate docker missing
        return f"/usr/bin/{name}"

    with (
        pytest.raises((typer.Exit, SystemExit)) as exc_info,
        patch("shutil.which", side_effect=_fake_which),
    ):
        from brain.setup import run_setup

        run_setup(
            dry_run=True,
            non_interactive=True,
            brain_home_override=brain_home,
            # Use port 0 so the socket.bind probe always succeeds (OS assigns
            # an ephemeral port; there is no risk of collision with real Postgres).
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )

    # Must exit non-zero.
    assert _exit_code(exc_info.value) != 0, (
        "setup must exit non-zero when docker preflight fails"
    )

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()

    assert "docker" in combined, (
        "output must mention 'docker' so the user knows which check failed"
    )
    assert "remediation" in combined, (
        "output must contain a Remediation hint"
    )


# ---------------------------------------------------------------------------
# Test 4 — --vault is consumed: written into fresh .env and appended to existing
# ---------------------------------------------------------------------------


def _run_through_state_creation(
    brain_home: Path,
    vault_override: Path,
    *,
    pre_create_env: bool = False,
) -> None:
    """Helper: run setup past T3.3 with all external calls mocked out."""
    if pre_create_env:
        # Write a minimal existing .env (no BRAIN_VAULT_PATH line).
        brain_home.mkdir(parents=True, exist_ok=True)
        (brain_home / ".env").write_text("DATABASE_URL=postgresql://x\n")

    with (
        patch("shutil.which", return_value="/usr/bin/fake"),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("brain.setup.ensure_shim"),  # skip real shim install
    ):
        from brain.setup import run_setup

        run_setup(
            dry_run=False,
            non_interactive=True,
            brain_home_override=brain_home,
            vault_override=vault_override,
            pg_port=0,
            skip_wiki=True,
            skip_skill=True,
        )


def test_setup_vault_override_written_into_fresh_env(tmp_path: Path) -> None:
    """--vault must appear as BRAIN_VAULT_PATH=... in a freshly rendered .env."""
    brain_home = tmp_path / ".brain"
    vault = tmp_path / "my-vault"

    _run_through_state_creation(brain_home, vault)

    env_text = (brain_home / ".env").read_text()
    assert f"BRAIN_VAULT_PATH={vault}" in env_text, (
        f"BRAIN_VAULT_PATH={vault} not found in rendered .env:\n{env_text}"
    )
    # Commented-out placeholder must be gone (replaced by the live value).
    assert "# BRAIN_VAULT_PATH=" not in env_text


def test_setup_vault_override_appended_to_existing_env(tmp_path: Path) -> None:
    """--vault must be appended to an existing .env that lacks BRAIN_VAULT_PATH."""
    brain_home = tmp_path / ".brain"
    vault = tmp_path / "my-vault"

    _run_through_state_creation(brain_home, vault, pre_create_env=True)

    env_text = (brain_home / ".env").read_text()
    assert f"BRAIN_VAULT_PATH={vault}" in env_text, (
        f"BRAIN_VAULT_PATH={vault} not appended to existing .env:\n{env_text}"
    )
