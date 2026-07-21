"""Real-DB tests for migration 016_index_hygiene.sql (perf wave T1).

Uses the ``test_db`` fixture (conftest's reset-and-migrate harness against the
Apache-AGE test instance on port 5434). Asserts:

* ``documents_tsv_idx`` (the provably dead GIN) is absent after migration.
* The two new indexes exist with the expected structure.
* ``uq_documents_gmail_thread`` (UNIQUE constraint, migration 008) is still
  present — guards against future over-eager drops of the data-integrity index.
* The migration is idempotent (re-applying is safe).

All rows are synthetic; no production data.
"""
from __future__ import annotations

import psycopg

from brain.db import migrations_dir

_MIGRATION_016 = migrations_dir() / "016_index_hygiene.sql"

# ---------------------------------------------------------------------------
# The one provably-dead index must be gone
# ---------------------------------------------------------------------------

def test_documents_tsv_idx_dropped(test_db: psycopg.Connection) -> None:
    """documents_tsv_idx (dead GIN, superseded by chunks.tsv in mig-009) must be absent."""
    row = test_db.execute(
        "SELECT indexname FROM pg_indexes WHERE indexname = 'documents_tsv_idx'"
    ).fetchone()
    assert row is None, "documents_tsv_idx should have been dropped by migration 016"


# ---------------------------------------------------------------------------
# Deferred filter indexes must NOT have been dropped
# ---------------------------------------------------------------------------

DEFERRED_INDEXES = [
    "derived_links_rule_idx",
    "documents_tags_idx",
    "directory_entries_email_idx",
    "idx_documents_draft",
    "idx_documents_sent_at",
    "idx_documents_thread_id",
]


def test_deferred_filter_indexes_untouched(test_db: psycopg.Connection) -> None:
    """The six deferred filter indexes must not have been dropped by migration 016.

    Their idx_scan=0 observation window was only ~3 days (post-cutover stats
    reset). They are deferred to migration 017 after a longer observation window.
    """
    # These indexes may or may not exist in the test schema (they're conditional
    # on prior migrations' tables being present), but if they DO exist they must
    # not have been dropped. We assert that migration 016's SQL contains no DROP
    # for them as the authoritative check.
    sql_text = _MIGRATION_016.read_text()
    for idx in DEFERRED_INDEXES:
        assert f"DROP INDEX IF EXISTS {idx}" not in sql_text, (
            f"Migration 016 must not DROP deferred index {idx}"
        )


# ---------------------------------------------------------------------------
# uq_documents_gmail_thread (UNIQUE constraint) must be preserved
# ---------------------------------------------------------------------------

def test_uq_documents_gmail_thread_preserved(test_db: psycopg.Connection) -> None:
    """uq_documents_gmail_thread is a UNIQUE invariant (mig-008) — must survive 016.

    UNIQUE indexes show idx_scan=0 legitimately (uniqueness enforcement does not
    increment idx_scan). Dropping it would silently remove a data-integrity guard.
    Regression guard: assert 016's SQL contains no DROP for it.
    """
    sql_text = _MIGRATION_016.read_text()
    assert "DROP INDEX IF EXISTS uq_documents_gmail_thread" not in sql_text, (
        "Migration 016 must NEVER drop uq_documents_gmail_thread (UNIQUE constraint)"
    )


def test_uq_documents_gmail_thread_still_exists_in_schema(
    test_db: psycopg.Connection,
) -> None:
    """After migration 016, uq_documents_gmail_thread must still exist in the DB."""
    row = test_db.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE indexname = 'uq_documents_gmail_thread'"
    ).fetchone()
    assert row is not None, (
        "uq_documents_gmail_thread must still exist after migration 016"
    )


# ---------------------------------------------------------------------------
# New indexes must exist
# ---------------------------------------------------------------------------

def test_participants_gin_index_exists(test_db: psycopg.Connection) -> None:
    """documents_participants_idx (GIN) must exist after migration 016."""
    row = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'documents_participants_idx'"
    ).fetchone()
    assert row is not None, "documents_participants_idx not found"
    assert "using gin" in str(row[0]).lower()


def test_source_path_partial_btree_index_exists(test_db: psycopg.Connection) -> None:
    """documents_source_path_idx (partial btree) must exist after migration 016."""
    row = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'documents_source_path_idx'"
    ).fetchone()
    assert row is not None, "documents_source_path_idx not found"
    assert "source_path is not null" in str(row[0]).lower()


def test_source_path_index_is_btree(test_db: psycopg.Connection) -> None:
    """documents_source_path_idx must be a btree (not GIN) index."""
    row = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'documents_source_path_idx'"
    ).fetchone()
    assert row is not None
    assert "using btree" in str(row[0]).lower()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_migration_016_is_idempotent(test_db: psycopg.Connection) -> None:
    """Re-running the SQL on the migrated DB is safe (IF NOT EXISTS / IF EXISTS guards)."""
    sql = _MIGRATION_016.read_text()
    test_db.execute(sql)  # second apply (first ran via the fixture)
    test_db.execute(sql)  # third apply — still safe
    # New indexes survive the re-apply.
    row = test_db.execute(
        "SELECT count(*) FROM pg_indexes "
        "WHERE indexname IN ('documents_participants_idx', 'documents_source_path_idx')"
    ).fetchone()
    assert row is not None
    assert row[0] == 2
    # uq_documents_gmail_thread still intact after re-apply.
    row2 = test_db.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE indexname = 'uq_documents_gmail_thread'"
    ).fetchone()
    assert row2 is not None, "uq_documents_gmail_thread must survive idempotent re-apply"
