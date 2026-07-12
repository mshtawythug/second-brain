"""Tests for the ANALYZE helpers in `brain.queries`.

`list_public_tables` and `analyze_tables` back the `brain analyze` command.
They run against the real test DB; all data is synthetic.
"""
from __future__ import annotations

from datetime import datetime

import psycopg
import pytest

from brain.queries import analyze_tables, list_public_tables


def test_list_public_tables_includes_core_tables(
    test_db: psycopg.Connection,
) -> None:
    """The known core tables appear in the listing, sorted."""
    tables = list_public_tables(test_db)

    assert "chunks" in tables
    assert "documents" in tables
    assert "sources" in tables
    assert tables == sorted(tables)


def _chunks_last_analyze(conn: psycopg.Connection) -> datetime | None:
    """Fresh read of ``chunks.last_analyze`` (None until the first ANALYZE).

    ``pg_stat_clear_snapshot()`` drops the transaction-cached stats snapshot so
    the read reflects the latest ANALYZE rather than a stale one.
    """
    conn.execute("SELECT pg_stat_clear_snapshot()")
    row = conn.execute(
        "SELECT last_analyze FROM pg_stat_user_tables WHERE relname = 'chunks'"
    ).fetchone()
    return row[0] if row is not None else None


def test_analyze_tables_sets_last_analyze(
    test_db: psycopg.Connection,
) -> None:
    """Running analyze_tables ADVANCES pg_stat last_analyze for the table.

    ``pg_stat_user_tables.last_analyze`` is NOT reset by the per-test TRUNCATE
    reset (migrate-once strategy), so a prior test's ANALYZE can leave it
    non-NULL. Capture the value BEFORE and assert this call moved it forward — an
    ``IS NOT NULL`` check alone would pass vacuously.
    """
    test_db.autocommit = True

    before = _chunks_last_analyze(test_db)
    analyze_tables(test_db, ["chunks"])
    after = _chunks_last_analyze(test_db)

    assert after is not None
    assert before is None or after > before


def test_analyze_tables_unknown_table_raises(
    test_db: psycopg.Connection,
) -> None:
    """A nonexistent identifier surfaces as a psycopg error, not silent success.

    Callers (the CLI) validate against list_public_tables first; this asserts
    the helper itself does not swallow a bad name.
    """
    test_db.autocommit = True

    with pytest.raises(psycopg.Error):
        analyze_tables(test_db, ["definitely_not_a_table"])
