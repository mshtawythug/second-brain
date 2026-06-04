"""Tests for the ANALYZE helpers in `brain.queries`.

`list_public_tables` and `analyze_tables` back the `brain analyze` command.
They run against the real test DB; all data is synthetic.
"""
from __future__ import annotations

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


def test_analyze_tables_sets_last_analyze(
    test_db: psycopg.Connection,
) -> None:
    """Running analyze_tables refreshes pg_stat last_analyze for the table."""
    test_db.autocommit = True

    analyze_tables(test_db, ["chunks"])

    row = test_db.execute(
        "SELECT last_analyze FROM pg_stat_user_tables WHERE relname = 'chunks'"
    ).fetchone()
    assert row is not None
    assert row[0] is not None


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
