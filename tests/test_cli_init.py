"""Tests for the `brain init` CLI command."""
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def test_init_applies_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")

    # wipe schema first
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "001_init.sql" in result.output

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='documents'"
        ).fetchone()
        assert row is not None
        assert row[0] == 1
