"""Real-DB tests for migration ``025_documents_updated_at.sql``.

``documents.updated_at`` records the last time the *user's knowledge* in a
document changed. It is deliberately distinct from the two timestamps that
already exist:

- ``ingested_at`` — when the row was (re-)written by the pipeline.
- ``coalesce(sent_at, ingested_at)`` — the *document's own* date, which is
  what ``--after`` / ``--before`` filter on. For an email or a transcript
  ``coalesce`` prefers ``sent_at``, so before 025 the edit dimension was
  unreachable at any layer.

The migration is additive and re-runnable, and it BACKFILLS from
``ingested_at``: a pre-025 row must never read a fabricated "now", or every
``--updated-after`` query would claim the whole corpus was touched at upgrade
time. All rows here are synthetic.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir, run_migrations

_MIGRATION_025 = migrations_dir() / "025_documents_updated_at.sql"


def _unapply_025(conn: psycopg.Connection[Any]) -> None:
    """Rewind an already-migrated schema to its pre-025 state.

    Reproduces exactly what a production database looks like the moment
    before ``brain init`` picks 025 up — which is far more faithful (and far
    cheaper) than re-deriving a 001-024 schema by hand. Callers must carry
    ``@pytest.mark.fresh_schema``: this is DDL, so the TRUNCATE-only per-test
    reset cannot undo it.
    """
    conn.execute("DROP INDEX IF EXISTS idx_documents_updated_at")
    conn.execute("ALTER TABLE documents DROP COLUMN IF EXISTS updated_at")
    conn.execute(
        "DELETE FROM schema_migrations WHERE name = %s", (_MIGRATION_025.name,)
    )


def _insert_legacy_row(
    conn: psycopg.Connection[Any], *, content_hash: str, ingested_at: datetime
) -> None:
    """Insert a synthetic document with an explicit historical ``ingested_at``."""
    conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, "
        "ingested_at) VALUES (%s, %s, %s, %s, %s)",
        ("Synthetic legacy note", "legacy body", content_hash, "note", ingested_at),
    )


def test_updated_at_column_exists_not_null_with_default(
    test_db: psycopg.Connection[Any],
) -> None:
    """RED-FIRST: migration 025 must add a NOT NULL ``updated_at DEFAULT NOW()``."""
    row = test_db.execute(
        """
        SELECT data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = 'documents' AND column_name = 'updated_at'
        """
    ).fetchone()

    assert row is not None, "migration 025 must add documents.updated_at"
    assert row[0] == "timestamp with time zone"
    assert row[1] == "NO", "updated_at must be NOT NULL after the backfill"
    assert row[2] is not None and "now()" in str(row[2]).lower()


def test_updated_at_index_exists(test_db: psycopg.Connection[Any]) -> None:
    """The DESC index is what keeps ``--updated-after`` cheap on a large corpus."""
    row = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'documents' AND indexname = 'idx_documents_updated_at'"
    ).fetchone()

    assert row is not None, "migration 025 must create idx_documents_updated_at"
    assert "updated_at DESC" in str(row[0])


def test_new_row_defaults_to_now(test_db: psycopg.Connection[Any]) -> None:
    """An INSERT that names no ``updated_at`` gets one from the column default.

    This is what makes bump site #1 (the ingest INSERT) a no-op change.
    """
    # Arrange / Act
    test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s)",
        ("Synthetic default probe", "body", "hash-025-default", "note"),
    )

    # Assert
    row = test_db.execute(
        "SELECT updated_at, NOW() - updated_at FROM documents WHERE content_hash = %s",
        ("hash-025-default",),
    ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert row[1] < timedelta(minutes=1), "default must be NOW(), not an epoch"


@pytest.mark.fresh_schema
def test_updated_at_backfilled_to_ingested_at(
    test_db: psycopg.Connection[Any],
) -> None:
    """A pre-025 row reads ``updated_at == ingested_at`` — never NULL, never now().

    The whole point of the backfill: after the upgrade,
    ``--updated-after <yesterday>`` must return the documents the user actually
    touched yesterday, not every row in the corpus.
    """
    # Arrange — rewind to pre-025 and seed a row dated well in the past.
    legacy_ts = datetime(2024, 3, 4, 5, 6, 7, tzinfo=UTC)
    _unapply_025(test_db)
    _insert_legacy_row(
        test_db, content_hash="hash-025-legacy", ingested_at=legacy_ts
    )

    # Act — this is exactly what `brain init` does on an existing database.
    applied = run_migrations(test_db)

    # Assert
    assert _MIGRATION_025.name in applied
    row = test_db.execute(
        "SELECT updated_at, ingested_at FROM documents WHERE content_hash = %s",
        ("hash-025-legacy",),
    ).fetchone()
    assert row is not None
    updated_at, ingested_at = row
    assert updated_at is not None, "backfill must not leave a NULL behind"
    assert updated_at == ingested_at == legacy_ts


@pytest.mark.fresh_schema
def test_migration_is_rerunnable(test_db: psycopg.Connection[Any]) -> None:
    """Applying 025 again is a clean no-op that does NOT restamp existing rows.

    The re-run risk is not a crash — every statement is guarded — it is the
    ``UPDATE ... SET updated_at = ingested_at`` clobbering a genuine edit
    timestamp. The ``WHERE updated_at IS NULL`` guard is what prevents that,
    and this asserts it.
    """
    # Arrange — a row whose updated_at deliberately differs from ingested_at,
    # i.e. a document the user edited after it was first ingested.
    edited_ts = datetime(2025, 6, 7, 8, 9, 10, tzinfo=UTC)
    ingest_ts = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    _insert_legacy_row(
        test_db, content_hash="hash-025-rerun", ingested_at=ingest_ts
    )
    test_db.execute(
        "UPDATE documents SET updated_at = %s WHERE content_hash = %s",
        (edited_ts, "hash-025-rerun"),
    )

    # Act — the session fixture already applied it once; apply it twice more.
    sql = _MIGRATION_025.read_text()
    test_db.execute(sql)
    test_db.execute(sql)

    # Assert — the edit timestamp survived, and the DDL is still singular.
    row = test_db.execute(
        "SELECT updated_at FROM documents WHERE content_hash = %s",
        ("hash-025-rerun",),
    ).fetchone()
    assert row is not None and row[0] == edited_ts

    columns = test_db.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'documents' AND column_name = 'updated_at'"
    ).fetchone()
    assert columns is not None and columns[0] == 1

    indexes = test_db.execute(
        "SELECT count(*) FROM pg_indexes "
        "WHERE tablename = 'documents' AND indexname = 'idx_documents_updated_at'"
    ).fetchone()
    assert indexes is not None and indexes[0] == 1
