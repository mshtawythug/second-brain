"""MCP tests for ``brain_gaps`` + the ``brain_search`` logging hook (Plan 08)."""
from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config

from .conftest import TEST_DATABASE_URL


@pytest.fixture
def gaps_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _insert(
    conn: psycopg.Connection,
    query: str,
    result_count: int = 0,
    *,
    session_id: uuid.UUID | None = None,
    source: str = "cli",
) -> None:
    conn.execute(
        "INSERT INTO search_queries "
        "(query, result_count, session_id, source) VALUES (%s, %s, %s, %s)",
        (
            query,
            result_count,
            str(session_id) if session_id is not None else None,
            source,
        ),
    )


def test_brain_gaps_empty(
    gaps_state: mcp_server._State,  # noqa: ARG001 — installs state
) -> None:
    assert mcp_server.brain_gaps() == {"gaps": []}


def test_brain_gaps_returns_normalized_labels(
    gaps_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,
) -> None:
    """MCP returns derived canonical labels (not raw stored query text)."""
    _insert(test_db, "Benefits Policy", 0)
    _insert(test_db, "policy benefits", 0)
    payload = mcp_server.brain_gaps(since_days=30, limit=10)
    assert payload["gaps"] == [
        {"query": "benefits policy", "count": 2, "kind": "zero_results"}
    ]


def test_brain_gaps_push_runs_without_error(
    gaps_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,
) -> None:
    """push=True exercises the detector + upsert path and returns the read view."""
    for _ in range(3):
        _insert(test_db, "vendor comparison", 0)
    payload = mcp_server.brain_gaps(since_days=30, limit=10, push=True)
    assert payload["gaps"][0]["query"] == "comparison vendor"
    assert payload["gaps"][0]["count"] == 3


@pytest.mark.fresh_schema
def test_brain_gaps_pre_023_schema_surfaces_init_hint(
    gaps_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,
) -> None:
    """MCP brain_gaps surfaces a clean `brain init` hint when fts_count is absent.

    The read path queries fts_count; on a pre-023 DB that raises UndefinedColumn.
    Instead of a generic "database error" the user gets the actionable hint.
    """
    from brain.mcp_compat import MCPError

    test_db.execute("ALTER TABLE search_queries DROP COLUMN fts_count")
    test_db.commit()
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_gaps(since_days=30, limit=10)
    msg = exc_info.value.error.message
    assert "brain init" in msg
    assert "migration 023" in msg


def test_brain_search_returns_session_id_with_hook(
    gaps_state: mcp_server._State,  # noqa: ARG001 — installs state
) -> None:
    """The logging hook leaves the brain_search return shape intact."""
    payload = mcp_server.brain_search(query="synthetic nothing matches")
    assert "session_id" in payload
    assert uuid.UUID(payload["session_id"])  # parses as a UUID
    assert payload["results"] == []
