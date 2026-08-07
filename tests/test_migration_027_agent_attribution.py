"""Migration 027 — ``agent_id`` on the three event tables (F10).

Asserts the shipped DDL by reading it back out of the catalog rather than by
trusting the file, because the properties that matter are properties of the
resulting schema:

- the column exists on all three tables, is TEXT, and is **nullable with no
  default** (every pre-027 row is genuinely unattributed; a literal ``'cli'``
  backfill would be a fabricated fact, and a NOT NULL would have forced one);
- the two rollup indexes exist and are **partial** on ``agent_id IS NOT NULL``,
  so a brain that never sets ``BRAIN_AGENT_ID`` pays nothing to maintain them;
- there is **no CHECK constraint** on ``agent_id`` — the shape gate lives at
  the Python boundary, and a SQL mirror of a Python regex is precisely the
  drift migration 024 exists to remember.

Re-application is asserted too: ``brain init`` is expected to be safe to run
repeatedly.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir, run_migrations

_AGENT_TABLES = ("documents", "interactions", "search_queries")


def _column(
    conn: psycopg.Connection[Any], table: str, column: str
) -> tuple[str, str, str | None] | None:
    """``(data_type, is_nullable, column_default)`` or ``None`` if absent."""
    row = conn.execute(
        "SELECT data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    return None if row is None else (row[0], row[1], row[2])


def _indexdef(conn: psycopg.Connection[Any], name: str) -> str | None:
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (name,)
    ).fetchone()
    return None if row is None else str(row[0])


@pytest.mark.fresh_schema
@pytest.mark.parametrize("table", _AGENT_TABLES)
def test_agent_id_column_is_nullable_text_with_no_default(
    test_db: psycopg.Connection[Any], table: str
) -> None:
    column = _column(test_db, table, "agent_id")

    assert column is not None, f"{table}.agent_id is missing"
    data_type, is_nullable, default = column
    assert data_type == "text"
    assert is_nullable == "YES", (
        "NOT NULL would have forced a fabricated backfill for every pre-027 row"
    )
    assert default is None, (
        "a default of 'cli' would duplicate `source` into the actor field and "
        "make 'which agent' unanswerable for exactly the rows it claims to answer"
    )


@pytest.mark.fresh_schema
def test_pre_027_rows_read_as_unattributed(
    test_db: psycopg.Connection[Any],
) -> None:
    """A row inserted without an agent is NULL, not a placeholder string."""
    test_db.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES ('Synthetic Note', 'body', 'note', 'vault', 'w4-mig-027-a')"
    )

    row = test_db.execute(
        "SELECT agent_id FROM documents WHERE content_hash = 'w4-mig-027-a'"
    ).fetchone()

    assert row is not None
    assert row[0] is None


@pytest.mark.fresh_schema
@pytest.mark.parametrize(
    "index_name", ["search_queries_agent_at_idx", "interactions_agent_at_idx"]
)
def test_rollup_indexes_are_partial_on_attributed_rows(
    test_db: psycopg.Connection[Any], index_name: str
) -> None:
    indexdef = _indexdef(test_db, index_name)

    assert indexdef is not None, f"{index_name} is missing"
    assert "agent_id IS NOT NULL" in indexdef, (
        "a full index would cost maintenance on every row of a brain that "
        "never sets BRAIN_AGENT_ID"
    )


@pytest.mark.fresh_schema
@pytest.mark.parametrize("table", _AGENT_TABLES)
def test_no_check_constraint_mirrors_the_python_grammar(
    test_db: psycopg.Connection[Any], table: str
) -> None:
    """The lesson migration 024 paid for: one gate, at the Python boundary.

    A SQL CHECK duplicating ``AGENT_ID_PATTERN`` would drift from it, and the
    Python mirror rejects earlier — so fixing only the SQL would look correct
    and would not be.
    """
    rows = test_db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'c'",
        (table,),
    ).fetchall()

    offending = [str(r[0]) for r in rows if "agent_id" in str(r[0])]
    assert offending == [], f"unexpected CHECK on {table}.agent_id: {offending}"


@pytest.mark.fresh_schema
def test_migration_027_is_re_runnable(
    test_db: psycopg.Connection[Any],
) -> None:
    """``brain init`` must be safe to run twice.

    Rewind only 027's ledger row, re-run the migration set, and assert it
    re-applies without error and the schema is unchanged.
    """
    name = "027_agent_attribution.sql"
    assert (migrations_dir() / name).is_file(), "027 must be packaged"

    before = _column(test_db, "documents", "agent_id")
    test_db.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))

    applied = run_migrations(test_db)

    assert name in applied, "027 should re-apply after its ledger row is removed"
    assert _column(test_db, "documents", "agent_id") == before
    assert _indexdef(test_db, "search_queries_agent_at_idx") is not None


@pytest.mark.fresh_schema
def test_agent_id_accepts_any_text_at_the_sql_layer(
    test_db: psycopg.Connection[Any],
) -> None:
    """No CHECK means the DB stores what Python hands it, including odd ids.

    This is the intended contract, not a gap: the set of agent names is
    open-ended and user-defined, and validation happens once, in
    ``brain.agent.normalize_agent_id``.
    """
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_type, kind, content_hash, agent_id) "
        "VALUES ('Synthetic Note B', 'body', 'note', 'vault', 'w4-mig-027-b', %s)",
        ("anything-the-python-layer-allowed",),
    )

    row = test_db.execute(
        "SELECT agent_id FROM documents WHERE content_hash = 'w4-mig-027-b'"
    ).fetchone()

    assert row is not None
    assert row[0] == "anything-the-python-layer-allowed"
