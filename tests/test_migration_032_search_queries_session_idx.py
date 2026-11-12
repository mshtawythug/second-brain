"""Real-DB tests for migration ``032_search_queries_session_idx.sql`` (RM-22b, plan D5).

The engagement stage counts a read with a session only when that session names a
logged search (``EXISTS (SELECT 1 FROM search_queries s WHERE s.session_id = …)``).
Without an index that probe is a sequential scan of ``search_queries`` per read
row: the D5 benchmark measured p95 11.7 s at 1M interactions and 300k searches.
The contract tested here is the index itself: on ``session_id``, partial on
``session_id IS NOT NULL``, usable by that probe, and a clean re-apply. All rows
are synthetic.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg

from brain.db import migrations_dir

_MIGRATION_032 = migrations_dir() / "032_search_queries_session_idx.sql"
_INDEX = "search_queries_session_idx"


def _indexdef(conn: psycopg.Connection[Any]) -> str | None:
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'search_queries' AND indexname = %s",
        (_INDEX,),
    ).fetchone()
    return None if row is None else str(row[0])


def test_the_index_is_on_session_id_and_partial(test_db: psycopg.Connection[Any]) -> None:
    indexdef = _indexdef(test_db)

    assert indexdef is not None
    assert "(session_id)" in indexdef
    assert "WHERE (session_id IS NOT NULL)" in indexdef


def test_the_engagement_probe_can_use_it(test_db: psycopg.Connection[Any]) -> None:
    # With sequential scans priced out, the planner must find an index for the
    # probe's predicate; on a table this small it would otherwise always seq-scan.
    with test_db.transaction():
        test_db.execute("SET LOCAL enable_seqscan = off")
        plan = test_db.execute(
            "EXPLAIN SELECT 1 FROM search_queries s WHERE s.session_id = %s",
            (uuid.uuid4(),),
        ).fetchall()

    assert _INDEX in "\n".join(str(r[0]) for r in plan)


def test_re_applying_the_migration_is_a_no_op(test_db: psycopg.Connection[Any]) -> None:
    before = _indexdef(test_db)

    test_db.execute(_MIGRATION_032.read_text())

    assert _indexdef(test_db) == before
