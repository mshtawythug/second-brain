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


def test_elicit_default_empty_queue_exits_clean(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`brain elicit` with an empty queue never drafts (no Ollama) and exits 0.

    With no gaps the session loop has nothing to draft, so the command returns
    before any enricher call — keeping this test deterministic and offline.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit"], input="q\n")
    assert res.exit_code == 0, res.output
    assert "no open gaps" in res.output.lower()


def test_elicit_default_rejects_unknown_signal(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown --signal value fails fast with a BadParameter (exit 2)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit", "--signal", "bogus"])
    assert res.exit_code == 2, res.output
    # Rich wraps the BadParameter panel across lines, so assert on tokens that
    # survive wrapping rather than the full comma-joined phrase.
    assert "signal" in res.output.lower()
    assert "delta" in res.output


def test_elicit_list_still_works_under_default_callback(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding the default callback must not shadow the `list` subcommand."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit", "list", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == []
