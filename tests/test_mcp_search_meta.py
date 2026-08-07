"""MCP ``brain_search`` metadata keys — additive, never a shape change.

The MCP surface is a dict today, MCP clients read named keys, and adding keys
to a JSON object is the canonical non-breaking evolution. The frozen part of
the contract (``session_id`` + ``results`` + the seven result keys) is owned by
``tests/test_search_output_unchanged.py``; this module covers what F5 added.

All fixture data is synthetic.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.db import PersistentConnection
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: Any,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


@pytest.fixture
def mcp_state_persistent(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: Any,
) -> Iterator[mcp_server._State]:
    """``_State`` shaped the way production builds it, with ``db_conn`` set.

    Necessary for asserting on rows the server WRITES. Without ``db_conn``,
    ``_mcp_conn`` falls back to ``brain.db.connect``, whose context manager
    only closes the connection — it never commits — so psycopg rolls back
    every write on exit and the telemetry row vanishes. Production always
    supplies a :class:`PersistentConnection`, which is ``autocommit=True``,
    so this fixture reproduces the real path rather than the fallback.
    """
    persistent = PersistentConnection(TEST_DATABASE_URL)
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,
        db_conn=persistent,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    try:
        yield state
    finally:
        conn = persistent._conn
        if conn is not None and not conn.closed:
            conn.close()


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 3) -> None:
    """Ingest ``count`` synthetic documents matching 'quarterly'.

    Bodies differ per document: ``documents.content_hash`` is UNIQUE.
    """
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Quarterly note {i}",
                content=f"The quarterly review covered budget and hiring {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:quarterly-{i}",
            tags=["planning"],
        )


def test_brain_search_keeps_session_id_and_results_keys(
    mcp_state: mcp_server._State,  # noqa: ARG001
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """The Q1-C keys survive the F5 additions."""
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    payload = mcp_server.brain_search(query="quarterly", fts_only=True)

    # Assert
    assert {"session_id", "results"} <= set(payload)


def test_brain_search_result_objects_unchanged(
    mcp_state: mcp_server._State,  # noqa: ARG001
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """Result objects keep exactly seven keys — no metadata leaked into them."""
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    payload = mcp_server.brain_search(query="quarterly", fts_only=True)

    # Assert
    assert payload["results"]
    for row in payload["results"]:
        assert set(row) == {
            "id", "title", "source_kind", "snippet", "score",
            "content_type", "tags",
        }


def test_brain_search_gains_total_and_timing_keys(
    mcp_state: mcp_server._State,  # noqa: ARG001
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """MCP always pays for the total — an agent needs '5 of 5' vs '5 of 544'."""
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    payload = mcp_server.brain_search(query="quarterly", limit=2, fts_only=True)

    # Assert
    assert payload["total_documents"] == 3
    assert payload["returned"] == 2
    assert payload["fts_count"] == 3
    assert set(payload["timing_ms"]) == {"embed", "sql", "facets", "total"}
    assert payload["timing_ms"]["sql"] is not None
    assert payload["timing_ms"]["total"] is not None
    assert payload["fts_only"] is True
    assert payload["timing_ms"]["embed"] is None


def test_brain_search_facets_default_none_and_populated_when_requested(
    mcp_state: mcp_server._State,  # noqa: ARG001
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """``facets`` is opt-in: ``null`` by default, grouped buckets when asked."""
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    without = mcp_server.brain_search(query="quarterly", fts_only=True)
    with_facets = mcp_server.brain_search(
        query="quarterly", fts_only=True, facets=True
    )

    # Assert
    assert without["facets"] is None
    assert without["timing_ms"]["facets"] is None
    assert with_facets["facets"] == {
        "source": [{"value": "manual", "count": 3}],
        "content_type": [{"value": "note", "count": 3}],
        "tag": [{"value": "planning", "count": 3}],
        "tag_truncated": 0,
    }
    assert with_facets["timing_ms"]["facets"] is not None


def test_brain_search_reports_embed_phase_when_vector_leg_runs(
    mcp_state: mcp_server._State,  # noqa: ARG001
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """With the vector leg on, the embed phase and cache flag are reported."""
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    payload = mcp_server.brain_search(query="quarterly mcp embed marker")

    # Assert
    assert payload["fts_only"] is False
    assert payload["timing_ms"]["embed"] is not None
    assert payload["embed_cached"] is False


def test_brain_search_persists_duration_ms(
    mcp_state_persistent: mcp_server._State,  # noqa: ARG001
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """Migration 024's column is populated on the MCP surface too."""
    # Arrange
    _seed(test_db, fake_embedder)

    # Act
    mcp_server.brain_search(query="quarterly", fts_only=True)

    # Assert
    row = test_db.execute(
        "SELECT duration_ms, source FROM search_queries ORDER BY at DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[1] == "mcp"
    assert isinstance(row[0], int) and row[0] >= 0
