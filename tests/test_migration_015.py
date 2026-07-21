"""Real-DB tests for migration 015_interactions_graph_targets.sql (wave G4-a, §17d Q2/Q5).

Uses the ``test_db`` fixture (conftest's reset-and-migrate harness against the
Apache-AGE test instance on port 5434). Asserts the schema generalization holds:

* ``interactions.document_id`` is now NULLABLE;
* ``target_type`` / ``target_id`` (TEXT, nullable) + ``graph_retrieved``
  (BOOLEAN NOT NULL DEFAULT FALSE) exist;
* the ``target_type`` domain CHECK rejects unknown kinds, accepts the three;
* the XOR CHECK rejects both-set and neither-set, accepts document-only and
  graph-target-only;
* a partial index on (target_type, target_id) exists for graph lookups;
* the migration is idempotent (re-applies cleanly).

All rows are synthetic; no production data.
"""
from __future__ import annotations

import psycopg
import pytest

from brain.db import migrations_dir

_MIGRATION_015 = migrations_dir() / "015_interactions_graph_targets.sql"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _seed_doc(conn: psycopg.Connection) -> str:
    """Insert one bare document row and return its UUID as text."""
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        ("seed", "seed body", "seed-hash", "note"),
    ).fetchone()
    assert row is not None
    return str(row[0])


# --------------------------------------------------------------------------- #
# Columns
# --------------------------------------------------------------------------- #
def test_document_id_is_nullable(test_db: psycopg.Connection) -> None:
    """``document_id`` lost its NOT NULL so graph-target rows can omit it."""
    row = test_db.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'interactions' AND column_name = 'document_id'"
    ).fetchone()
    assert row is not None
    assert str(row[0]) == "YES"


@pytest.mark.parametrize(
    ("column", "data_type", "is_nullable"),
    [
        ("target_type", "text", "YES"),
        ("target_id", "text", "YES"),
        ("graph_retrieved", "boolean", "NO"),
    ],
)
def test_new_columns_exist(
    test_db: psycopg.Connection, column: str, data_type: str, is_nullable: str
) -> None:
    row = test_db.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'interactions' AND column_name = %s",
        (column,),
    ).fetchone()
    assert row is not None, f"interactions.{column} is missing"
    assert str(row[0]) == data_type
    assert str(row[1]) == is_nullable


def test_graph_retrieved_defaults_false(test_db: psycopg.Connection) -> None:
    """``graph_retrieved`` defaults to FALSE for the unchanged document path."""
    doc_id = _seed_doc(test_db)
    test_db.execute(
        "INSERT INTO interactions (document_id, action, source) "
        "VALUES (%s, %s, %s)",
        (doc_id, "opened", "mcp"),
    )
    row = test_db.execute(
        "SELECT graph_retrieved FROM interactions WHERE document_id = %s",
        (doc_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is False


# --------------------------------------------------------------------------- #
# target_type domain CHECK
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("target_type", ["entity", "community", "theme"])
def test_target_type_check_accepts_known_kinds(
    test_db: psycopg.Connection, target_type: str
) -> None:
    test_db.execute(
        "INSERT INTO interactions (action, source, target_type, target_id) "
        "VALUES (%s, %s, %s, %s)",
        ("rated_useful", "cli", target_type, "tgt-1"),
    )
    count = test_db.execute(
        "SELECT count(*) FROM interactions WHERE target_type = %s",
        (target_type,),
    ).fetchone()
    assert count is not None
    assert count[0] == 1


def test_target_type_check_rejects_unknown_kind(
    test_db: psycopg.Connection,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO interactions (action, source, target_type, target_id) "
            "VALUES (%s, %s, %s, %s)",
            ("rated_useful", "cli", "document", "tgt-1"),
        )


# --------------------------------------------------------------------------- #
# XOR target-shape CHECK
# --------------------------------------------------------------------------- #
def test_xor_accepts_document_only(test_db: psycopg.Connection) -> None:
    doc_id = _seed_doc(test_db)
    test_db.execute(
        "INSERT INTO interactions (document_id, action, source) "
        "VALUES (%s, %s, %s)",
        (doc_id, "opened", "mcp"),
    )
    count = test_db.execute("SELECT count(*) FROM interactions").fetchone()
    assert count is not None
    assert count[0] == 1


def test_xor_accepts_graph_target_only(test_db: psycopg.Connection) -> None:
    test_db.execute(
        "INSERT INTO interactions (action, source, target_type, target_id) "
        "VALUES (%s, %s, %s, %s)",
        ("rated_useful", "cli", "entity", "ent-7"),
    )
    count = test_db.execute("SELECT count(*) FROM interactions").fetchone()
    assert count is not None
    assert count[0] == 1


def test_xor_rejects_both_document_and_graph_target(
    test_db: psycopg.Connection,
) -> None:
    doc_id = _seed_doc(test_db)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO interactions "
            "(document_id, action, source, target_type, target_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (doc_id, "rated_useful", "cli", "entity", "ent-7"),
        )


def test_xor_rejects_neither_document_nor_graph_target(
    test_db: psycopg.Connection,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO interactions (action, source) VALUES (%s, %s)",
            ("rated_useful", "cli"),
        )


def test_xor_rejects_half_specified_graph_target(
    test_db: psycopg.Connection,
) -> None:
    """target_type set but target_id NULL trips the XOR (not a full target)."""
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO interactions (action, source, target_type) "
            "VALUES (%s, %s, %s)",
            ("rated_useful", "cli", "entity"),
        )


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #
def test_migration_015_creates_target_index(test_db: psycopg.Connection) -> None:
    rows = test_db.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'interactions'"
    ).fetchall()
    names = {str(r[0]) for r in rows}
    assert "interactions_target_idx" in names


def test_migration_015_target_index_is_partial(test_db: psycopg.Connection) -> None:
    """The target index excludes document-only rows (target_type NULL)."""
    row = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'interactions_target_idx'"
    ).fetchone()
    assert row is not None
    assert "target_type IS NOT NULL" in str(row[0])


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_migration_015_is_idempotent(test_db: psycopg.Connection) -> None:
    """Re-running the SQL on the migrated DB is safe (IF NOT EXISTS guards)."""
    sql = _MIGRATION_015.read_text()
    test_db.execute(sql)  # second apply (first ran via the fixture)
    test_db.execute(sql)  # third apply — still safe
    # Columns + constraints survive the re-apply.
    row = test_db.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'interactions' "
        "AND column_name IN ('target_type', 'target_id', 'graph_retrieved')"
    ).fetchone()
    assert row is not None
    assert row[0] == 3
    cons = test_db.execute(
        "SELECT count(*) FROM pg_constraint "
        "WHERE conname IN ('interactions_target_type_chk', "
        "'interactions_target_xor_chk')"
    ).fetchone()
    assert cons is not None
    assert cons[0] == 2
