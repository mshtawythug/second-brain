"""``agent_id`` actually lands on the event rows (F10).

Migration 027 adds the column; these tests prove the two writers put a value
in it and that the default really is NULL rather than a fabricated
placeholder. The `(unattributed)` bucket in ``brain usage`` is only honest if
un-configured surfaces genuinely write NULL.

All fixture data is synthetic.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from brain.gaps import record_search_query
from brain.interactions import record_interaction


def _seed_doc(conn: psycopg.Connection[Any], *, content_hash: str) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES ('Quarterly Planning Notes', 'body', 'note', 'vault', %s) "
        "RETURNING id::text",
        (content_hash,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _one(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


# ---------------------------------------------------------------------------
# search_queries
# ---------------------------------------------------------------------------


def test_search_query_records_the_agent(test_db: psycopg.Connection[Any]) -> None:
    record_search_query(
        test_db,
        query="quarterly planning",
        result_count=3,
        session_id=None,
        source="cli",
        agent_id="research-agent",
    )

    assert (
        _one(
            test_db,
            "SELECT agent_id FROM search_queries WHERE query = %s",
            ("quarterly planning",),
        )
        == "research-agent"
    )


def test_search_query_without_an_agent_is_null_not_a_placeholder(
    test_db: psycopg.Connection[Any],
) -> None:
    """The default must be NULL — ``'cli'`` would duplicate ``source``."""
    record_search_query(
        test_db,
        query="unattributed search",
        result_count=1,
        session_id=None,
        source="cli",
    )

    assert (
        _one(
            test_db,
            "SELECT agent_id FROM search_queries WHERE query = %s",
            ("unattributed search",),
        )
        is None
    )


def test_agent_and_source_are_independent_axes(
    test_db: psycopg.Connection[Any],
) -> None:
    """Two agents on the same surface must be distinguishable.

    This is the whole point of the column: ``source`` cannot tell these two
    rows apart, and ``brain usage``'s headline question depends on being able
    to.
    """
    for agent in ("research-agent", "capture-bot"):
        record_search_query(
            test_db,
            query=f"query from {agent}",
            result_count=1,
            session_id=None,
            source="mcp",
            agent_id=agent,
        )

    rows = test_db.execute(
        "SELECT agent_id, source FROM search_queries "
        "WHERE query LIKE 'query from %%' ORDER BY agent_id"
    ).fetchall()

    assert [(r[0], r[1]) for r in rows] == [
        ("capture-bot", "mcp"),
        ("research-agent", "mcp"),
    ]


def test_agent_id_coexists_with_the_other_additive_columns(
    test_db: psycopg.Connection[Any],
) -> None:
    """027's column must not disturb 023's ``fts_count`` or 024's ``duration_ms``."""
    record_search_query(
        test_db,
        query="all columns",
        result_count=5,
        session_id=None,
        source="cli",
        fts_count=42,
        duration_ms=1234,
        agent_id="research-agent",
    )

    row = test_db.execute(
        "SELECT fts_count, duration_ms, agent_id FROM search_queries "
        "WHERE query = 'all columns'"
    ).fetchone()

    assert row == (42, 1234, "research-agent")


# ---------------------------------------------------------------------------
# interactions
# ---------------------------------------------------------------------------


def test_interaction_records_the_agent(test_db: psycopg.Connection[Any]) -> None:
    doc_id = _seed_doc(test_db, content_hash="w4-attr-int-1")

    interaction_id = record_interaction(
        test_db,
        document_id=doc_id,
        action="opened",
        source="mcp",
        agent_id="research-agent",
    )

    assert (
        _one(
            test_db,
            "SELECT agent_id FROM interactions WHERE id = %s",
            (interaction_id,),
        )
        == "research-agent"
    )


def test_interaction_without_an_agent_is_null(
    test_db: psycopg.Connection[Any],
) -> None:
    doc_id = _seed_doc(test_db, content_hash="w4-attr-int-2")

    interaction_id = record_interaction(
        test_db, document_id=doc_id, action="opened", source="cli"
    )

    assert (
        _one(
            test_db,
            "SELECT agent_id FROM interactions WHERE id = %s",
            (interaction_id,),
        )
        is None
    )


def test_agent_id_does_not_disturb_the_graph_target_shape(
    test_db: psycopg.Connection[Any],
) -> None:
    """The document-XOR-graph-target invariant is orthogonal to attribution."""
    interaction_id = record_interaction(
        test_db,
        action="opened",
        source="mcp",
        target_type="entity",
        target_id="person:synthetic-key",
        graph_retrieved=True,
        agent_id="research-agent",
    )

    row = test_db.execute(
        "SELECT document_id, target_type, target_id, graph_retrieved, agent_id "
        "FROM interactions WHERE id = %s",
        (interaction_id,),
    ).fetchone()

    assert row == (
        None,
        "entity",
        "person:synthetic-key",
        True,
        "research-agent",
    )


def test_session_tracking_still_works_alongside_attribution(
    test_db: psycopg.Connection[Any],
) -> None:
    """``session_id`` and ``agent_id`` answer different questions; both persist."""
    doc_id = _seed_doc(test_db, content_hash="w4-attr-int-3")
    session = uuid.uuid4()

    record_search_query(
        test_db,
        query="session plus agent",
        result_count=1,
        session_id=session,
        source="mcp",
        agent_id="research-agent",
    )
    record_interaction(
        test_db,
        document_id=doc_id,
        action="opened",
        source="mcp",
        session_id=session,
        agent_id="research-agent",
    )

    search_row = test_db.execute(
        "SELECT session_id::text, agent_id FROM search_queries "
        "WHERE query = 'session plus agent'"
    ).fetchone()
    assert search_row == (str(session), "research-agent")


@pytest.mark.parametrize("source", ["cli", "mcp", "wiki"])
def test_every_valid_source_accepts_an_agent(
    test_db: psycopg.Connection[Any], source: str
) -> None:
    """Guards against a narrowed ``_VALID_SOURCES`` regressing Task 1D."""
    record_search_query(
        test_db,
        query=f"from {source}",
        result_count=1,
        session_id=None,
        source=source,
        agent_id="research-agent",
    )

    assert (
        _one(
            test_db,
            "SELECT agent_id FROM search_queries WHERE query = %s",
            (f"from {source}",),
        )
        == "research-agent"
    )
