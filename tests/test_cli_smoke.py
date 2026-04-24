"""Smoke tests for the Typer CLI app."""
from typer.testing import CliRunner

from brain.cli import app


def test_help_succeeds() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "brain" in result.output.lower() or "Usage" in result.output
