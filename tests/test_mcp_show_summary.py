"""Tests for the Wave Q1-D additive ``summary`` key on MCP ``brain_show``.

Locks the wire contract: ``summary`` appears in the response ONLY when
populated on the row; absent when ``documents.summary IS NULL``. Confirms
``brain_search`` shape is untouched (no breaking change one wave after
Q1-C's session_id wrapper).
"""
from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — fixture keeps schema fresh
    fake_embedder: object,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _ingest_with_summary(
    conn: psycopg.Connection, embedder: object, *, summary: str | None
) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="Test doc",
            content="Body content. " * 30,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert result.document_id is not None
    if summary is not None:
        conn.execute(
            "UPDATE documents SET summary=%s, summary_model='llama3.1:8b', "
            "summary_at=NOW() WHERE id=%s",
            (summary, result.document_id),
        )
    return result.document_id


def test_brain_show_returns_summary_key_when_present(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest_with_summary(
        test_db, fake_embedder, summary="An LLM-generated summary."
    )
    payload = mcp_server.brain_show(id_prefix=doc_id[:8])
    assert "summary" in payload
    assert payload["summary"] == "An LLM-generated summary."


def test_brain_show_omits_summary_key_when_null(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest_with_summary(test_db, fake_embedder, summary=None)
    payload = mcp_server.brain_show(id_prefix=doc_id[:8])
    # Additive: the key is absent (not None) so consumers parsing the
    # JSON shape with ``"summary" in payload`` get a clean "not enriched"
    # signal.
    assert "summary" not in payload


def test_brain_search_does_not_expose_summary(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """D16 — explicitly do NOT change brain_search wire shape in Q1-D."""
    _ingest_with_summary(test_db, fake_embedder, summary="should not surface")
    # Use a query that matches the seeded body so we get at least one hit.
    response = mcp_server.brain_search(query="body content")
    assert "session_id" in response
    assert "results" in response
    for r in response["results"]:
        # The wave plan locks: brain_search results never carry summary.
        assert "summary" not in r
