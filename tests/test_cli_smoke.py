"""Smoke tests for the Typer CLI app."""
from typer.testing import CliRunner

from brain.cli import app


def test_help_succeeds() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "brain" in result.output.lower() or "Usage" in result.output


def test_help_documents_krisp_ingestion() -> None:
    """`brain --help` must explain how to import Krisp calls.

    Krisp has no native CLI command — transcripts are pulled by Claude via
    the Krisp MCP and piped into `brain ingest-stdin`. The --help epilog
    is the only place a human-or-Claude reading the CLI surface can discover
    that flow, so guard it with a smoke test.
    """
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    output = result.output
    assert "Importing Krisp calls" in output
    assert "ingest-stdin" in output
    assert "--source krisp" in output
    assert "--external-id" in output
