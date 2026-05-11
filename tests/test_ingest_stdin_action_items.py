"""Tests for the Wave Q1-D ``--content-type krisp_action_items`` path.

Validates the metadata gate (``parent_meeting_external_id`` required) plus
the auto-applied ``action-items`` tag.
"""
from __future__ import annotations

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


def _setup(monkeypatch: pytest.MonkeyPatch, fake_embedder: object) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    # Skip enrichment to keep the test path hermetic — no Ollama dependency.
    # The ingest-stdin command builds the enricher unconditionally unless
    # ``--no-enrich`` is set; we always pass --no-enrich below.


def test_action_items_without_parent_external_id_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — DB schema fresh
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, fake_embedder)
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "krisp",
            "--external-id", "m1--action-items",
            "--title", "Action items: m1",
            "--content-type", "krisp_action_items",
            "--no-enrich",
        ],
        input="- [ ] follow up with person-x\n",
    )
    assert result.exit_code != 0
    combined = result.output + (
        result.stderr if hasattr(result, "stderr") else ""
    )
    assert "parent_meeting_external_id" in combined


def test_action_items_with_parent_external_id_succeeds_and_auto_tags(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, fake_embedder)
    meta = json.dumps({"parent_meeting_external_id": "meeting-xyz"})
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "krisp",
            "--external-id", "meeting-xyz--action-items",
            "--title", "Action items: meeting-xyz",
            "--content-type", "krisp_action_items",
            "--metadata", meta,
            "--no-enrich",
        ],
        input="- [ ] follow up with person-x\n",
    )
    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT tags, content_type FROM documents "
        "WHERE source_id = (SELECT id FROM sources "
        "WHERE external_id = 'meeting-xyz--action-items')"
    ).fetchone()
    assert row is not None
    tags, content_type = row
    assert content_type == "krisp_action_items"
    assert "action-items" in (tags or [])


def test_action_items_dedup_by_content_hash(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, fake_embedder)
    meta = json.dumps({"parent_meeting_external_id": "abc"})
    args = [
        "ingest-stdin",
        "--source", "krisp",
        "--external-id", "abc--action-items",
        "--title", "Action items: abc",
        "--content-type", "krisp_action_items",
        "--metadata", meta,
        "--no-enrich",
    ]
    body = "- [ ] follow up with person-x\n"
    r1 = CliRunner().invoke(app, args, input=body)
    r2 = CliRunner().invoke(app, args, input=body)
    assert r1.exit_code == 0 and r2.exit_code == 0
    # Stdin ingests dedupe by content_hash → second call is a no-op.
    count_row = test_db.execute(
        "SELECT count(*) FROM documents WHERE content_type='krisp_action_items'"
    ).fetchone()
    assert count_row is not None
    assert count_row[0] == 1
