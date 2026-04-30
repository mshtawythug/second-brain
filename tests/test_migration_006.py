"""Tests for migration 006_dedup_file_by_source_path.sql.

Migration 004's partial unique index on ``content_hash`` (scoped to
``kind='ingested'``) was over-broad: it deduped file-based ingests by
content_hash, which collapsed two distinct files with byte-identical
content into a single row. After this migration, file ingests dedup
by ``source_path`` in code; the DB-level constraint applies only to
stdin-sourced rows where ``source_path IS NULL``.

These tests verify (a) a fresh DB ends in the right shape, (b) a DB
that previously ran migration 004 converges to the same shape, and
(c) the migration SQL is safely re-runnable.
"""
from pathlib import Path

import psycopg

_MIGRATION_006 = (
    Path(__file__).parent.parent / "migrations" / "006_dedup_file_by_source_path.sql"
)
_OLD_004_INDEX_DDL = (
    "CREATE UNIQUE INDEX documents_content_hash_ingested_idx "
    "ON documents (content_hash) WHERE kind = 'ingested'"
)


def _index_exists(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_indexes "
        "WHERE schemaname='public' AND tablename='documents' AND indexname=%s",
        (name,),
    ).fetchone()
    return row is not None


def test_fresh_db_has_only_new_index(test_db: psycopg.Connection) -> None:
    """After all migrations run, only the post-006 index is present."""
    assert _index_exists(test_db, "documents_content_hash_stdin_idx")
    assert not _index_exists(test_db, "documents_content_hash_ingested_idx")


def test_post_004_state_converges_after_006(test_db: psycopg.Connection) -> None:
    """Simulate a DB that stopped at migration 004 and re-run 006.

    The fresh ``test_db`` fixture has already run 006, so we reverse it to
    reach the "post-004 / pre-006" state, then apply 006's SQL and check
    that the resulting state matches the fresh-DB state.
    """
    test_db.execute("DROP INDEX IF EXISTS documents_content_hash_stdin_idx")
    test_db.execute(_OLD_004_INDEX_DDL)
    assert _index_exists(test_db, "documents_content_hash_ingested_idx")
    assert not _index_exists(test_db, "documents_content_hash_stdin_idx")

    test_db.execute(_MIGRATION_006.read_text())

    assert _index_exists(test_db, "documents_content_hash_stdin_idx")
    assert not _index_exists(test_db, "documents_content_hash_ingested_idx")


def test_migration_006_is_idempotent(test_db: psycopg.Connection) -> None:
    """Re-running 006's SQL on a fresh DB succeeds and leaves state unchanged."""
    sql = _MIGRATION_006.read_text()
    test_db.execute(sql)  # second apply (first ran via the fixture)
    test_db.execute(sql)  # third apply — still safe
    assert _index_exists(test_db, "documents_content_hash_stdin_idx")
    assert not _index_exists(test_db, "documents_content_hash_ingested_idx")
