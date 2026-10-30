"""Real-DB tests for migration ``029_saved_searches.sql`` (RM-15).

Two additive tables: ``saved_searches`` (one row per watch) and ``saved_search_hits`` (one row per
watch and matching document, with ``notified_at`` NULL until a published brief lists it). The
contract tested here is the DDL the ticket fixes: the ``mode`` CHECK, the UNIQUE name, both
``ON DELETE CASCADE`` foreign keys, the partial index, and a clean re-apply. All rows are
synthetic.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir

_MIGRATION_029 = migrations_dir() / "029_saved_searches.sql"


def _migration_sql() -> str:
    return _MIGRATION_029.read_text()


def _doc(conn: psycopg.Connection[Any]) -> str:
    return str(
        conn.execute(
            "INSERT INTO documents (title, content, content_hash, content_type) "
            "VALUES ('Synthetic', 'synthetic body', %s, 'note') RETURNING id::text",
            (str(uuid.uuid4()),),
        ).fetchone()[0]
    )


def _watch(conn: psycopg.Connection[Any], name: str = "w") -> str:
    return str(
        conn.execute(
            "INSERT INTO saved_searches (name, query) VALUES (%s, 'q') RETURNING id::text",
            (name,),
        ).fetchone()[0]
    )


def test_defaults_are_lexical_enabled_and_empty_filters(
    test_db: psycopg.Connection[Any],
) -> None:
    _watch(test_db)

    row = test_db.execute(
        "SELECT mode, enabled, filters, created_at IS NOT NULL, last_checked_at IS NOT NULL "
        "FROM saved_searches"
    ).fetchone()

    assert row == ("lexical", True, {}, True, True)


def test_mode_outside_the_two_values_is_refused(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO saved_searches (name, query, mode) VALUES ('w', 'q', 'fuzzy')"
        )


def test_names_are_unique(test_db: psycopg.Connection[Any]) -> None:
    _watch(test_db, "dup")

    with pytest.raises(psycopg.errors.UniqueViolation):
        _watch(test_db, "dup")


def test_hits_cascade_from_both_parents(test_db: psycopg.Connection[Any]) -> None:
    watch, doc_a, doc_b = _watch(test_db), _doc(test_db), _doc(test_db)
    for doc in (doc_a, doc_b):
        test_db.execute(
            "INSERT INTO saved_search_hits (saved_search_id, document_id) VALUES (%s, %s)",
            (watch, doc),
        )

    test_db.execute("DELETE FROM documents WHERE id = %s", (doc_a,))
    after_doc = test_db.execute("SELECT count(*) FROM saved_search_hits").fetchone()[0]
    test_db.execute("DELETE FROM saved_searches WHERE id = %s", (watch,))
    after_watch = test_db.execute("SELECT count(*) FROM saved_search_hits").fetchone()[0]

    assert (after_doc, after_watch) == (1, 0)


def test_a_hit_is_unnotified_until_marked_and_keyed_once(
    test_db: psycopg.Connection[Any],
) -> None:
    watch, doc = _watch(test_db), _doc(test_db)
    test_db.execute(
        "INSERT INTO saved_search_hits (saved_search_id, document_id) VALUES (%s, %s)",
        (watch, doc),
    )

    row = test_db.execute(
        "SELECT notified_at, first_seen_at IS NOT NULL FROM saved_search_hits"
    ).fetchone()
    with pytest.raises(psycopg.errors.UniqueViolation):
        test_db.execute(
            "INSERT INTO saved_search_hits (saved_search_id, document_id) VALUES (%s, %s)",
            (watch, doc),
        )

    assert row == (None, True)


def test_the_unnotified_index_is_partial(test_db: psycopg.Connection[Any]) -> None:
    definition = test_db.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND indexname = 'idx_saved_search_hits_unnotified'"
    ).fetchone()

    assert definition is not None
    assert "WHERE (notified_at IS NULL)" in definition[0]


@pytest.mark.fresh_schema
def test_migration_is_rerunnable(test_db: psycopg.Connection[Any]) -> None:
    """Applying 029 again is a clean no-op: every statement is ``IF NOT EXISTS``."""
    sql = _migration_sql()

    test_db.execute(sql)
    test_db.execute(sql)

    tables = test_db.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name IN ('saved_searches', 'saved_search_hits')"
    ).fetchone()[0]
    assert tables == 2
