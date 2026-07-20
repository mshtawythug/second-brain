"""CLI-layer tests for `brain setup` (Typer option defaults).

``test_setup.py`` pins ``run_setup``'s own default port; this module pins the
Typer option layer, which passes ``pg_port=port`` explicitly and would otherwise
silently override the canonical default. Keeping both pinned stops the two
layers from drifting apart again.
"""
from typing import Any

import pytest
from typer.testing import CliRunner

from brain.cli import app


def test_setup_cli_port_default_is_canonical_55432(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`brain setup` with no --port threads the canonical 55432 into run_setup.

    Regression for the overhaul port-canonicalization work: ``setup_cmd`` calls
    ``run_setup(pg_port=port, ...)`` explicitly, so a stale Typer default (5433)
    overrode ``run_setup``'s 55432 default and generated a dead DATABASE_URL.
    Pins the CLI layer so the two defaults can't diverge.
    """
    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    # setup_cmd does `from .setup import ... run_setup` at call time, so patch
    # the source module attribute rather than a cli-level alias.
    monkeypatch.setattr("brain.setup.run_setup", _spy)
    result = CliRunner().invoke(app, ["setup", "--dry-run", "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert captured["pg_port"] == 55432


def test_setup_cli_default_profile_is_standard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`brain setup` with no --profile threads the canonical 'standard' profile."""
    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("brain.setup.run_setup", _spy)
    result = CliRunner().invoke(app, ["setup", "--dry-run", "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert captured["profile"] == "standard"


def test_setup_cli_rejects_unknown_profile() -> None:
    """M8: --profile is a click.Choice — an invalid value exits 2 (BadParameter).

    resolve_profile's SetupError remains the backstop for direct run_setup
    callers, but the CLI layer rejects the bad value before run_setup is reached.
    """
    result = CliRunner().invoke(
        app, ["setup", "--profile", "gigantic", "--dry-run", "--non-interactive"]
    )

    assert result.exit_code == 2, result.output
    assert "gigantic" in result.output or "Invalid value" in result.output


def test_setup_cli_accepts_each_valid_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every profile name in the Choice is accepted and threaded to run_setup."""
    for name in ("minimal", "standard", "full"):
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            "brain.setup.run_setup", lambda _c=captured, **kw: _c.update(kw)
        )
        result = CliRunner().invoke(
            app, ["setup", "--profile", name, "--dry-run", "--non-interactive"]
        )
        assert result.exit_code == 0, result.output
        assert captured["profile"] == name
