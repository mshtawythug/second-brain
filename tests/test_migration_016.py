"""Real-DB tests for migration 016_index_hygiene.sql (perf wave T1).

Uses the ``test_db`` fixture (conftest's reset-and-migrate harness against the
Apache-AGE test instance on port 5434). Asserts:

* All eight dead indexes are absent after migration.
* The two new indexes exist with the expected structure.
* The migration is idempotent (re-applying is safe).

All rows are synthetic; no production data.
"""
from __future__ import annotations

from pathlib import Path

import psycopg

_MIGRATION_016 = (
    Path(__file__).parent.parent
    / "migrations"
    / "016_index_hygiene.sql"
)

# ---------------------------------------------------------------------------
# Dead indexes must be absent after migration
# ---------------------------------------------------------------------------
DROPPED_INDEXES = [
    "documents_tsv_idx",
    "derived_links_rule_idx",
    "documents_tags_idx",
    "directory_entries_email_idx",
    "idx_documents_draft",
    "idx_documents_sent_at",
    "idx_documents_thread_id",
    "uq_documents_gmail_thread",
]


def test_dead_indexes_dropped(test_db: psycopg.Connection) -> None:
    """All eight dead indexes must be absent after migration 016."""
    rows = test_db.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE indexname = ANY(%s)",
        (DROPPED_INDEXES,),
    ).fetchall()
    remaining = {str(r[0]) for r in rows}
    assert remaining == set(), (
        f"Expected these indexes to be gone after migration 016: {remaining}"
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
    """Re-running the SQL on the migrated DB is safe (IF NOT EXISTS guards)."""
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
