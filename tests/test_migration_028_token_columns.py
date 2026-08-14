"""Real-DB tests for migration ``028_search_query_tokens.sql``.

Mirrors ``tests/test_migration_024_search_duration.py`` — the nearest prior
"nullable INT ride-along on ``search_queries``" migration.

What 028 adds is two columns whose *nullability* is the contract, not an
implementation detail: every pre-028 row is honestly NULL, and a consumer that
reads NULL as ``0`` would report that retrieval was free. All rows here are
synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir

_MIGRATION_028 = migrations_dir() / "028_search_query_tokens.sql"


def _column(conn: psycopg.Connection[Any], name: str) -> tuple[str, str] | None:
    row = conn.execute(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'search_queries' AND column_name = %s
        """,
        (name,),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def test_payload_tokens_column_exists_and_is_nullable(
    test_db: psycopg.Connection[Any],
) -> None:
    """The MEASURED half: nullable INT, so "not measured" is representable."""
    column = _column(test_db, "payload_tokens")

    assert column is not None, "migration 028 must add payload_tokens"
    assert column[0] == "integer"
    assert column[1] == "YES", "payload_tokens must stay nullable"


def test_baseline_tokens_column_exists_and_is_nullable(
    test_db: psycopg.Connection[Any],
) -> None:
    """The COUNTERFACTUAL half: nullable, and NULL is its normal state."""
    column = _column(test_db, "baseline_tokens")

    assert column is not None, "migration 028 must add baseline_tokens"
    assert column[0] == "integer"
    assert column[1] == "YES", "baseline_tokens must stay nullable"


def test_pre_028_rows_read_back_as_null(
    test_db: psycopg.Connection[Any],
) -> None:
    """A row written without token columns keeps NULL — 0 would be a lie."""
    # Arrange / Act — the shape of every row written before this migration.
    test_db.execute(
        "INSERT INTO search_queries (query, result_count, source) "
        "VALUES (%s, %s, %s)",
        ("synthetic pre-028 query", 3, "cli"),
    )

    # Assert
    row = test_db.execute(
        "SELECT payload_tokens, baseline_tokens FROM search_queries "
        "WHERE query = %s",
        ("synthetic pre-028 query",),
    ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None


def test_no_check_constraint_mirrors_the_python_gate(
    test_db: psycopg.Connection[Any],
) -> None:
    """The migration's stated design: no SQL mirror of the Python bound.

    Migration 024's header records why — a CHECK duplicating a Python rule
    drifts, and fixing only the SQL half looks correct while not being. The
    gate lives in ``brain.gaps._validate_token_columns`` and is tested there.
    A future edit that "hardens" the schema with a CHECK would create exactly
    the second, silently-diverging gate that decision rejected.
    """
    rows = test_db.execute(
        """
        SELECT pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        WHERE c.conrelid = 'search_queries'::regclass AND c.contype = 'c'
        """
    ).fetchall()
    definitions = " ".join(str(r[0]) for r in rows)

    assert "payload_tokens" not in definitions
    assert "baseline_tokens" not in definitions


@pytest.mark.fresh_schema
def test_migration_is_rerunnable(test_db: psycopg.Connection[Any]) -> None:
    """Applying 028 again is a clean no-op (``ADD COLUMN IF NOT EXISTS``)."""
    # Arrange
    sql = _MIGRATION_028.read_text()

    # Act — the session fixture already applied it once; apply it twice more.
    test_db.execute(sql)
    test_db.execute(sql)

    # Assert — each column still present exactly once.
    for name in ("payload_tokens", "baseline_tokens"):
        row = test_db.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'search_queries' AND column_name = %s",
            (name,),
        ).fetchone()
        assert row is not None and row[0] == 1, name
