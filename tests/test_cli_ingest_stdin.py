"""Tests for `brain ingest-stdin` — the generic Claude-orchestrated ingester."""
import json
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _patch_embedder(monkeypatch: pytest.MonkeyPatch, fake_embedder: object) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)


def test_ingest_stdin_creates_document(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "krisp",
            "--external-id", "meeting-42",
            "--title", "person-x sync",
            "--content-type", "transcript",
            "--metadata",
            json.dumps({"participants": ["person-x", "Ali"], "duration_min": 28}),
        ],
        input="Hello person-x. Let me catch you up on COMPANY_REDACTED.\n\nIt was great.\n",
    )
    assert result.exit_code == 0, result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT d.title, d.content_type, s.kind, s.external_id "
            "FROM documents d JOIN sources s ON s.id=d.source_id "
            "WHERE s.external_id='meeting-42'"
        ).fetchone()
    assert row is not None
    assert row[0] == "person-x sync"
    assert row[1] == "transcript"
    assert row[2] == "krisp"
    assert row[3] == "meeting-42"


def test_ingest_stdin_dedups_on_external_id(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    args = [
        "ingest-stdin",
        "--source", "slack",
        "--external-id", "ts-1",
        "--title", "Thread",
        "--content-type", "transcript",
    ]
    CliRunner().invoke(app, args, input="same content")
    CliRunner().invoke(app, args, input="same content")
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        n_row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert n_row is not None
    assert n_row[0] == 1


def test_ingest_stdin_empty_input_fails(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Empty stdin content is a user error — exit 1 with a red message."""
    _patch_embedder(monkeypatch, fake_embedder)
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "krisp",
            "--external-id", "mt-empty",
            "--title", "Empty",
            "--content-type", "transcript",
        ],
        input="   \n\n   ",
    )
    assert result.exit_code == 1
    assert "stdin was empty" in result.output


def test_ingest_stdin_date_flag_populates_metadata(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """--date is merged into source+document metadata under the 'date' key."""
    _patch_embedder(monkeypatch, fake_embedder)
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "krisp",
            "--external-id", "mt-dated",
            "--title", "Dated sync",
            "--content-type", "transcript",
            "--date", "2026-04-24",
        ],
        input="some call content",
    )
    assert result.exit_code == 0, result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT d.metadata, s.metadata FROM documents d "
            "JOIN sources s ON s.id=d.source_id "
            "WHERE s.external_id='mt-dated'"
        ).fetchone()
    assert row is not None
    assert row[0]["date"] == "2026-04-24"
    assert row[1]["date"] == "2026-04-24"


def test_ingest_stdin_force_reingests(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """With --force, a repeat ingest of the same content replaces the prior row."""
    _patch_embedder(monkeypatch, fake_embedder)
    args = [
        "ingest-stdin",
        "--source", "slack",
        "--external-id", "ts-force",
        "--title", "Thread",
        "--content-type", "transcript",
    ]
    first = CliRunner().invoke(app, args, input="same content")
    assert first.exit_code == 0, first.output
    second = CliRunner().invoke(app, [*args, "--force"], input="same content")
    assert second.exit_code == 0, second.output
    assert "ingested" in second.output.lower()
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        n_row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert n_row is not None
    assert n_row[0] == 1
