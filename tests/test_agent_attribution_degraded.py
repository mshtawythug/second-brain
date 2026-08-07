"""Search survives a binary that writes ``agent_id`` to a pre-027 DB (F10).

The realistic upgrade order is: new binary lands, operator has not re-run
``brain init`` yet. The new binary's ``record_search_query`` names a column
the DB does not have. Search is the daily-driver command — it must keep
working, loudly nagging rather than failing.

``gaps.py`` already had this contract for migration 023's ``fts_count`` and
024's ``duration_ms``; 027's ``agent_id`` joins the same
:data:`~brain.gaps._ADDITIVE_COLUMNS` set, so these tests verify the guard
genuinely extends to it rather than assuming membership is enough.

The third assertion is the subtle one: the swallow must not poison the
caller's open transaction. ``record_search_query`` wraps its INSERT in
``conn.transaction()`` (a savepoint when nested) precisely so a failed
telemetry write rolls back only itself.

These tests are ``fresh_schema`` because they DROP a column; the fixture
rebuilds the schema afterwards.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg
import pytest

from brain.gaps import (
    _ADDITIVE_COLUMNS,
    record_search_query,
    search_queries_schema_hint,
)


def _drop_agent_id(conn: psycopg.Connection[Any], table: str) -> None:
    conn.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS agent_id")


def test_agent_id_is_registered_as_an_additive_column() -> None:
    """Membership is what wires the swallow AND the hint. Pin it explicitly."""
    assert _ADDITIVE_COLUMNS["agent_id"] == "027"


@pytest.mark.fresh_schema
def test_search_logging_survives_a_missing_agent_id_column(
    test_db: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    """The swallow: no exception escapes, and the operator is told why."""
    _drop_agent_id(test_db, "search_queries")

    with caplog.at_level(logging.WARNING, logger="brain.gaps"):
        record_search_query(
            test_db,
            query="daily driver query",
            result_count=2,
            session_id=None,
            source="cli",
            agent_id="research-agent",
        )

    assert "brain init" in caplog.text, "the warning must be actionable"
    assert "agent_id" in caplog.text


@pytest.mark.fresh_schema
def test_the_raw_query_is_not_logged_at_warning(
    test_db: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Privacy contract: the query string must not surface at WARNING+."""
    _drop_agent_id(test_db, "search_queries")

    with caplog.at_level(logging.WARNING, logger="brain.gaps"):
        record_search_query(
            test_db,
            query="synthetic-private-phrase",
            result_count=0,
            session_id=None,
            source="cli",
            agent_id="research-agent",
        )

    assert "synthetic-private-phrase" not in caplog.text


@pytest.mark.fresh_schema
def test_the_callers_open_transaction_survives_the_swallow(
    test_db: psycopg.Connection[Any],
) -> None:
    """The savepoint contract — the reason this is not a bare try/except.

    A caller mid-transaction (the CLI's autocommit path is the common case,
    but ``brain ui`` and batch paths are not) must be able to keep writing
    after telemetry fails. Without the inner ``conn.transaction()`` the
    aborted statement would poison the whole transaction and the caller's
    subsequent INSERT would raise ``InFailedSqlTransaction``.
    """
    _drop_agent_id(test_db, "search_queries")
    test_db.autocommit = False

    with test_db.transaction():
        test_db.execute(
            "INSERT INTO documents "
            "(title, content, content_type, kind, content_hash) "
            "VALUES ('Before Telemetry', 'body', 'note', 'vault', 'w4-deg-1')"
        )

        record_search_query(
            test_db,
            query="mid transaction",
            result_count=1,
            session_id=None,
            source="cli",
            agent_id="research-agent",
        )

        # The caller's transaction must still be usable.
        test_db.execute(
            "INSERT INTO documents "
            "(title, content, content_type, kind, content_hash) "
            "VALUES ('After Telemetry', 'body', 'note', 'vault', 'w4-deg-2')"
        )

    rows = test_db.execute(
        "SELECT title FROM documents WHERE content_hash IN ('w4-deg-1','w4-deg-2') "
        "ORDER BY title"
    ).fetchall()
    assert [r[0] for r in rows] == ["After Telemetry", "Before Telemetry"]


@pytest.mark.fresh_schema
def test_schema_hint_names_migration_027(
    test_db: psycopg.Connection[Any],
) -> None:
    """The read path's matching hint, so ``brain gaps`` fails cleanly too."""
    _drop_agent_id(test_db, "search_queries")

    try:
        test_db.execute("SELECT agent_id FROM search_queries LIMIT 1")
    except psycopg.errors.UndefinedColumn as exc:
        test_db.rollback()
        hint = search_queries_schema_hint(exc)
    else:  # pragma: no cover — the column was dropped above
        pytest.fail("expected UndefinedColumn")

    assert hint is not None
    assert "027" in hint
    assert "brain init" in hint


@pytest.mark.fresh_schema
def test_a_genuinely_unknown_column_still_propagates(
    test_db: psycopg.Connection[Any],
) -> None:
    """The guard is set-membership, not a blanket swallow.

    A real bug — a typo'd column name — must surface, not be eaten as if it
    were migration lag.
    """
    test_db.execute(
        "ALTER TABLE search_queries DROP COLUMN IF EXISTS result_count"
    )

    with pytest.raises(psycopg.errors.UndefinedColumn):
        record_search_query(
            test_db,
            query="unknown column",
            result_count=1,
            session_id=None,
            source="cli",
        )
