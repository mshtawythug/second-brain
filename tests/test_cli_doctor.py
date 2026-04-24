"""Tests for the `brain doctor` CLI command."""
import os
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def test_doctor_passes_when_env_and_db_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_doctor_fails_without_voyage_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    # Also shield against a .env file on disk populating the key.
    with patch("brain.config.load_dotenv"):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code != 0
    assert "VOYAGE_API_KEY" in result.output


def test_doctor_reports_missing_pgvector(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the vector extension is not installed, doctor should fail with a clear message."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")

    # Fake a connection whose pg_extension lookup returns no row.
    fake_conn = MagicMock()
    # first .execute("SELECT 1") — we don't care about result
    # second .execute(pg_extension query).fetchone() → None
    fake_conn.execute.return_value.fetchone.return_value = None
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = fake_conn
    fake_ctx.__exit__.return_value = False

    with patch("brain.cli.connect", return_value=fake_ctx):
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code != 0
    assert "pgvector" in result.output


def test_doctor_reports_database_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """psycopg.Error raised by connect should produce a postgres FAIL line and exit 1."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")

    def boom(_url: str) -> None:
        raise psycopg.OperationalError("could not connect to server")

    with patch("brain.cli.connect", side_effect=boom):
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code != 0
    assert "postgres" in result.output
    assert "FAIL" in result.output


def test_doctor_reports_missing_gws(monkeypatch: pytest.MonkeyPatch) -> None:
    """When gws is not on PATH, doctor should note Gmail ingestion is disabled but still exit 0."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")

    with patch("brain.cli.shutil.which", return_value=None):
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "gws CLI" in result.output
    assert "missing" in result.output


def test_doctor_reports_gws_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """When gws IS on PATH, doctor should print the OK line for it."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")

    with patch("brain.cli.shutil.which", return_value="/usr/local/bin/gws"):
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "gws CLI" in result.output
    assert "OK" in result.output
