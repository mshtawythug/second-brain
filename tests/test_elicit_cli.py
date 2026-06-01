"""Tests for the `brain elicit list` CLI command."""
from __future__ import annotations

import json
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

# psycopg3's conn.info.dsn strips the password (security feature); use the
# explicit test URL (which includes credentials) as every other CLI test does.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def test_elicit_list_empty(test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """brain elicit list --json returns an empty JSON array when the queue is empty."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit", "list", "--json"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip().startswith("[")
    data = json.loads(res.stdout)
    assert data == []


def test_elicit_list_text_empty(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain elicit list (text mode) prints 'no open gaps' on empty queue."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit", "list"])
    assert res.exit_code == 0, res.output
    assert "no open gaps" in res.output.lower()
