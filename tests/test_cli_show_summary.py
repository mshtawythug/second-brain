"""Tests for the Wave Q1-D ``summary:`` line on ``brain show`` output.

Additive — when ``documents.summary IS NULL`` the line is omitted entirely
so existing scripts parsing the labeled-prefix output stay unaffected (R7).
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


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    *,
    summary: str | None,
) -> str:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="Seeded doc",
            content="Body of the seeded document used by show-summary tests.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert result.document_id is not None
    if summary is not None:
        test_db.execute(
            "UPDATE documents SET summary=%s, summary_model='llama3.1:8b', "
            "summary_at=NOW() WHERE id=%s",
            (summary, result.document_id),
        )
    return result.document_id


def test_brain_show_displays_summary_line_when_set(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder, summary="A short summary.")
    result = CliRunner().invoke(app, ["show", doc_id])
    assert result.exit_code == 0, result.output
    assert "summary:      A short summary." in result.output


def test_brain_show_omits_summary_line_when_null(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder, summary=None)
    result = CliRunner().invoke(app, ["show", doc_id])
    assert result.exit_code == 0, result.output
    assert "summary:" not in result.output


def test_brain_show_json_includes_summary_only_when_set(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder, summary="JSON-mode summary.")
    result = CliRunner().invoke(app, ["show", doc_id, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["summary"] == "JSON-mode summary."


def test_brain_show_json_omits_summary_key_when_null(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder, summary=None)
    result = CliRunner().invoke(app, ["show", doc_id, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "summary" not in payload
