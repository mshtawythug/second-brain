"""Real-DB tests for migration ``024_search_queries_duration.sql``.

Two unrelated payloads ride in one file (see the migration's own header):

1. ``search_queries.duration_ms`` — nullable INT retrieval latency.
2. Widening the ``source`` CHECK on BOTH telemetry tables to admit ``'ui'``.

Payload 2's SQL half is asserted here; its Python-mirror half lives in
``tests/test_telemetry_source_enum.py``. All rows are synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir

_MIGRATION_024 = migrations_dir() / "024_search_queries_duration.sql"


def test_duration_column_exists(test_db: psycopg.Connection[Any]) -> None:
    """RED-FIRST: ``search_queries.duration_ms`` must exist after migration."""
    row = test_db.execute(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'search_queries' AND column_name = 'duration_ms'
        """
    ).fetchone()

    assert row is not None, "migration 024 must add search_queries.duration_ms"
    assert row[0] == "integer"
    assert row[1] == "YES", "duration_ms must stay nullable ('not measured')"


def test_pre_024_rows_read_as_not_measured_never_zero(
    test_db: psycopg.Connection[Any]
) -> None:
    """A row written without a duration keeps NULL — 0 ms would be a lie."""
    # Arrange / Act
    test_db.execute(
        "INSERT INTO search_queries (query, result_count, source) "
        "VALUES (%s, %s, %s)",
        ("synthetic legacy query", 3, "cli"),
    )

    # Assert
    row = test_db.execute(
        "SELECT duration_ms FROM search_queries WHERE query = %s",
        ("synthetic legacy query",),
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_source_check_admits_ui_on_both_tables(
    test_db: psycopg.Connection[Any]
) -> None:
    """The SQL half of payload 2, asserted straight off the live constraints."""
    for table in ("interactions", "search_queries"):
        row = test_db.execute(
            """
            SELECT pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            WHERE c.conrelid = %s::regclass
              AND c.contype = 'c'
              AND pg_get_constraintdef(c.oid) LIKE '%%source%%'
            """,
            (table,),
        ).fetchone()
        assert row is not None, f"{table} must carry a source CHECK"
        definition = str(row[0])
        for value in ("cli", "mcp", "wiki", "ui"):
            assert f"'{value}'" in definition, f"{table} must admit {value!r}"


def test_old_auto_generated_constraint_names_are_gone(
    test_db: psycopg.Connection[Any]
) -> None:
    """The unnamed inline CHECKs were REPLACED, not merely shadowed.

    PostgreSQL auto-named migrations 010's and 019's inline CHECKs
    ``interactions_source_check`` / ``search_queries_source_check``. If the
    migration dropped the wrong name, the old narrow constraint would survive
    alongside the new one and every ``'ui'`` insert would still fail — while
    the migration appeared to succeed.
    """
    rows = test_db.execute(
        """
        SELECT conname FROM pg_constraint
        WHERE conname IN (
            'interactions_source_check', 'search_queries_source_check',
            'interactions_source_allowed', 'search_queries_source_allowed'
        )
        """
    ).fetchall()
    names = {str(r[0]) for r in rows}
    assert names == {
        "interactions_source_allowed",
        "search_queries_source_allowed",
    }


@pytest.mark.fresh_schema
def test_migration_is_rerunnable(test_db: psycopg.Connection[Any]) -> None:
    """Applying 024 a second time is a clean no-op."""
    # Arrange
    sql = _MIGRATION_024.read_text()

    # Act — the session fixture already applied it once; apply it twice more.
    test_db.execute(sql)
    test_db.execute(sql)

    # Assert — column still present exactly once, constraints still correct.
    columns = test_db.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'search_queries' AND column_name = 'duration_ms'"
    ).fetchone()
    assert columns is not None and columns[0] == 1
    constraints = test_db.execute(
        "SELECT count(*) FROM pg_constraint "
        "WHERE conname = 'search_queries_source_allowed'"
    ).fetchone()
    assert constraints is not None and constraints[0] == 1
    # And the widened value set still works after re-application.
    test_db.execute(
        "INSERT INTO search_queries (query, result_count, source) "
        "VALUES (%s, %s, %s)",
        ("synthetic rerun probe", 1, "ui"),
    )
