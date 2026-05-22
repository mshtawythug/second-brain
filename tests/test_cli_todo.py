"""Tests for the ``brain todo`` CLI command (Wave Q1-D 2.7).

Drives the CLI against the real test DB; seeds ``krisp_action_items`` docs
and asserts the parsed/printed output.
"""
from __future__ import annotations

import json
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _seed_krisp_action_items(
    test_db: psycopg.Connection,
    fake_embedder: object,
    *,
    external_id: str,
    title: str,
    body: str,
) -> str:
    """Seed one ``krisp_action_items`` doc."""
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title,
            content=body,
            content_type="krisp_action_items",
            source_path=None,
            metadata={"parent_meeting_external_id": "meeting-xyz"},
        ),
        source_kind="krisp",
        source_external_id=external_id,
    )
    assert result.document_id is not None
    return result.document_id


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> tuple[str, str]:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    a = _seed_krisp_action_items(
        test_db,
        fake_embedder,
        external_id="m1--action-items",
        title="Action items: meeting 1",
        body=(
            "## Action items from meeting 1\n\n"
            "- [ ] follow up with person-x\n"
            "- [x] send the deck\n"
            "- [ ] schedule sync\n"
        ),
    )
    b = _seed_krisp_action_items(
        test_db,
        fake_embedder,
        external_id="m2--action-items",
        title="Action items: meeting 2",
        body="- [ ] review pricing model\n",
    )
    return a, b


def test_brain_todo_lists_open_items_by_default(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["todo"])
    assert result.exit_code == 0, result.output
    assert "follow up with person-x" in result.output
    assert "schedule sync" in result.output
    assert "review pricing model" in result.output
    # Done item must be excluded by default.
    assert "send the deck" not in result.output


def test_brain_todo_closed_flag_includes_done(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["todo", "--closed"])
    assert result.exit_code == 0
    assert "send the deck" in result.output
    assert "[done]" in result.output


def test_brain_todo_json_shape(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_a, _ = _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["todo", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert all("document_id" in row for row in payload)
    assert all("state" in row for row in payload)
    states = {row["state"] for row in payload}
    assert states == {"open"}
    ids = {row["document_id"] for row in payload}
    assert doc_a in ids


def test_brain_todo_empty_corpus_prints_message(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — empty DB
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["todo"])
    assert result.exit_code == 0
    assert "no action items" in result.output


def test_brain_todo_since_filters_by_recency(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """``--since N`` flag accepted; semantics tested at the SQL level
    (rows older than the window are filtered)."""
    _setup(monkeypatch, test_db, fake_embedder)
    # Window of 30 days; seeded rows are fresh so they all appear.
    result = CliRunner().invoke(app, ["todo", "--since", "30"])
    assert result.exit_code == 0
    assert "follow up with person-x" in result.output
    # Tiny window — 0-day lookback excludes everything (NOW() - 0 days = NOW(),
    # rows ingested before NOW() drop out).
    result_tiny = CliRunner().invoke(app, ["todo", "--since", "0"])
    assert result_tiny.exit_code == 0
    assert "no action items" in result_tiny.output


def test_brain_todo_limit_caps_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["todo", "--limit", "1"])
    assert result.exit_code == 0
    # Only one open-item line is emitted.
    lines = [
        ln for ln in result.output.strip().splitlines()
        if ln.startswith("[open]")
    ]
    assert len(lines) == 1
