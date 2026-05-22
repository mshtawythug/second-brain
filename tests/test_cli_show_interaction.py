"""Tests for ``brain show`` interaction logging (G4-b, spec §17d Q2).

``brain show`` gains ``--query`` / ``--session-id`` / ``--graph-retrieved`` to
record an ``opened`` interaction (source='cli') the same way MCP ``brain_show``
does — with optional graph provenance. With no ``--query`` nothing is logged
(today's behavior). Logging is best-effort: a failure never blocks the
document from printing.
"""
from __future__ import annotations

import os
import uuid
from unittest import mock

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
) -> str:
    """Wire env + fake embedder; seed one doc; return its id."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="Seed",
            content="Seed body for show",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert result.document_id is not None
    return result.document_id


def _rows(doc_id: str) -> list[tuple[str, str, str | None, str | None, bool]]:
    """Read rows as (action, source, query, session_id, graph_retrieved)."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT action, source, query, session_id::text, graph_retrieved "
            "FROM interactions WHERE document_id = %s ORDER BY at",
            (doc_id,),
        ).fetchall()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def test_show_without_query_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Default ``brain show`` logs no interaction — unchanged pre-G4 behavior."""
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["show", doc_id])
    assert result.exit_code == 0, result.output
    assert "Seed body for show" in result.output
    assert _rows(doc_id) == []


def test_show_with_query_logs_opened(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["show", doc_id, "--query", "company-id"])
    assert result.exit_code == 0, result.output
    rows = _rows(doc_id)
    assert len(rows) == 1
    assert rows[0][0] == "opened"
    assert rows[0][1] == "cli"
    assert rows[0][2] == "company-id"
    assert rows[0][3] is None  # no session id given
    assert rows[0][4] is False  # graph_retrieved off by default


def test_show_graph_retrieved_stamps_provenance(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A graph-surfaced open records graph_retrieved=TRUE on the document row."""
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(
        app, ["show", doc_id, "--query", "themes with X", "--graph-retrieved"]
    )
    assert result.exit_code == 0, result.output
    rows = _rows(doc_id)
    assert len(rows) == 1
    assert rows[0][0] == "opened"
    assert rows[0][4] is True


def test_show_with_session_id_persists(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    sid = str(uuid.uuid4())
    result = CliRunner().invoke(
        app, ["show", doc_id, "--query", "q", "--session-id", sid]
    )
    assert result.exit_code == 0, result.output
    rows = _rows(doc_id)
    assert len(rows) == 1
    assert rows[0][3] == sid


def test_show_session_id_without_query_rejected(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Mirror MCP D15 — a session id with no query carries no signal."""
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(
        app, ["show", doc_id, "--session-id", str(uuid.uuid4())]
    )
    assert result.exit_code != 0
    assert _rows(doc_id) == []


def test_show_invalid_session_id_uuid_rejected(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(
        app, ["show", doc_id, "--query", "q", "--session-id", "not-a-uuid"]
    )
    assert result.exit_code != 0
    assert _rows(doc_id) == []


def test_show_logging_failure_does_not_break_show(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """G4-b never-raise: a logging failure must not stop the doc from printing."""
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    boom = psycopg.OperationalError("simulated logging outage")
    with mock.patch("brain.cli.record_interaction", side_effect=boom):
        result = CliRunner().invoke(app, ["show", doc_id, "--query", "q"])
    assert result.exit_code == 0, result.output
    assert "Seed body for show" in result.output  # body still printed
    assert _rows(doc_id) == []  # nothing persisted
