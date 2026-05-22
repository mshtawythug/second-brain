"""Tests for Q1-C MCP session tracking — ``brain_search`` returns
``session_id``; ``brain_show`` accepts ``originating_query`` +
``session_id`` and logs an ``opened`` row when both are passed.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any
from unittest import mock

import psycopg
import pytest
from mcp import McpError
from mcp.types import INVALID_PARAMS

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


def _ingest(
    conn: psycopg.Connection, embedder: object, *, title: str, content: str
) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=f"manual:{title}",
    )
    assert result.document_id is not None
    return result.document_id


def _interactions(doc_id: str) -> list[dict[str, Any]]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT action, source, query, session_id::text, graph_retrieved "
            "FROM interactions WHERE document_id = %s ORDER BY at",
            (doc_id,),
        ).fetchall()
    return [
        {
            "action": r[0],
            "source": r[1],
            "query": r[2],
            "session_id": r[3],
            "graph_retrieved": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# brain_search return shape — Q1-C breaking change
# ---------------------------------------------------------------------------


def test_brain_search_returns_session_id_and_results(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    _ingest(test_db, fake_embedder, title="A", content="A: company-id")
    payload = mcp_server.brain_search(query="company-id")
    assert set(payload.keys()) == {"session_id", "results"}
    assert isinstance(payload["results"], list)


def test_brain_search_session_id_is_a_valid_uuid(
    test_db: psycopg.Connection,  # noqa: ARG001
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    payload = mcp_server.brain_search(query="anything")
    # Must parse — uuid.uuid4() round-trip.
    uuid.UUID(payload["session_id"])


def test_brain_search_results_keep_pre_q1c_inner_shape(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """The per-result dict shape must not regress — only the outer wrapper
    changed."""
    _ingest(test_db, fake_embedder, title="A", content="A: company-id was great")
    payload = mcp_server.brain_search(query="company-id")
    results = payload["results"]
    assert results
    expected = {"id", "title", "source_kind", "snippet", "score",
                "content_type", "tags"}
    for r in results:
        assert set(r.keys()) == expected


def test_brain_search_each_call_mints_a_fresh_session_id(
    test_db: psycopg.Connection,  # noqa: ARG001
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    a = mcp_server.brain_search(query="x")
    b = mcp_server.brain_search(query="x")
    assert a["session_id"] != b["session_id"]


# ---------------------------------------------------------------------------
# brain_show — interaction logging contract
# ---------------------------------------------------------------------------


def test_brain_show_without_originating_query_writes_nothing(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    mcp_server.brain_show(id_prefix=doc_id[:8])
    assert _interactions(doc_id) == []


def test_brain_show_with_originating_query_logs_opened(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    mcp_server.brain_show(id_prefix=doc_id[:8], originating_query="company-id")
    rows = _interactions(doc_id)
    assert len(rows) == 1
    assert rows[0]["action"] == "opened"
    assert rows[0]["source"] == "mcp"
    assert rows[0]["query"] == "company-id"
    assert rows[0]["session_id"] is None


def test_brain_show_with_session_id_persists_session(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A full search→show round-trip records both the query and session."""
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    search_payload = mcp_server.brain_search(query="company-id")
    sid = search_payload["session_id"]
    mcp_server.brain_show(
        id_prefix=doc_id[:8],
        originating_query="company-id",
        session_id=sid,
    )
    rows = _interactions(doc_id)
    assert len(rows) == 1
    assert rows[0]["session_id"] == sid
    assert rows[0]["query"] == "company-id"


def test_brain_show_session_id_without_query_is_rejected(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Plan D15 — a session id without an originating query carries no
    useful signal and must surface as INVALID_PARAMS."""
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_show(
            id_prefix=doc_id[:8],
            session_id=str(uuid.uuid4()),
        )
    assert exc_info.value.error.code == INVALID_PARAMS
    assert _interactions(doc_id) == []


def test_brain_show_invalid_session_id_uuid_is_rejected(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_show(
            id_prefix=doc_id[:8],
            originating_query="company-id",
            session_id="not-a-uuid",
        )
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "not a valid UUID" in exc_info.value.error.message
    assert _interactions(doc_id) == []


def test_brain_show_failed_id_resolution_writes_nothing(
    test_db: psycopg.Connection,  # noqa: ARG001
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """When the doc can't be resolved, no interaction row is written —
    the FK would fail anyway, but the resolver short-circuits first."""
    with pytest.raises(McpError):
        mcp_server.brain_show(
            id_prefix="ffffff",
            originating_query="company-id",
        )
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute("SELECT count(*) FROM interactions").fetchone()
        assert row is not None
        assert row[0] == 0


# ---------------------------------------------------------------------------
# G4-b — brain_show graph_retrieved provenance + never-raise
# ---------------------------------------------------------------------------


def test_brain_show_graph_retrieved_defaults_false(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """An ordinary open records graph_retrieved=FALSE (unchanged pre-G4)."""
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    mcp_server.brain_show(id_prefix=doc_id[:8], originating_query="company-id")
    rows = _interactions(doc_id)
    assert len(rows) == 1
    assert rows[0]["graph_retrieved"] is False


def test_brain_show_graph_retrieved_true_stamps_provenance(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Opening a graph-surfaced doc records graph_retrieved=TRUE on the row."""
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    mcp_server.brain_show(
        id_prefix=doc_id[:8],
        originating_query="themes with X",
        graph_retrieved=True,
    )
    rows = _interactions(doc_id)
    assert len(rows) == 1
    assert rows[0]["action"] == "opened"
    assert rows[0]["graph_retrieved"] is True


def test_brain_show_graph_retrieved_without_query_writes_nothing(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Provenance alone (no originating_query) logs nothing — gated on query."""
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    mcp_server.brain_show(id_prefix=doc_id[:8], graph_retrieved=True)
    assert _interactions(doc_id) == []


def test_brain_show_return_shape_unchanged_with_graph_retrieved(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """The locked brain_show return shape is unaffected by graph_retrieved."""
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    payload = mcp_server.brain_show(
        id_prefix=doc_id[:8],
        originating_query="q",
        graph_retrieved=True,
    )
    assert "graph_retrieved" not in payload  # provenance is log-only, not wire
    assert payload["id"] == doc_id


def test_brain_show_logging_failure_does_not_raise(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """G4-b never-raise: a logging failure is swallowed; the doc still returns."""
    doc_id = _ingest(test_db, fake_embedder, title="A", content="A body")
    boom = psycopg.OperationalError("simulated logging outage")
    with mock.patch.object(mcp_server, "record_interaction", side_effect=boom):
        payload = mcp_server.brain_show(
            id_prefix=doc_id[:8], originating_query="company-id"
        )
    assert payload["id"] == doc_id  # returned despite the logging failure
    assert _interactions(doc_id) == []  # nothing persisted
