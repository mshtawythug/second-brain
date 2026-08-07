"""Real-DB tests for migration ``026_document_sensitivity.sql``.

``documents.sensitivity`` is the trust-boundary column: ``'confidential'`` keeps
a document's body off the hosted embedder, and (in later tasks) out of MCP
responses and off the published wiki. It is deliberately NOT ``documents.draft``
— draft is a *publish* flag whose entire enforcement is one branch in the Quartz
emitter, and whose own docstring says draft documents stay fully visible
locally.

The migration is additive and re-runnable, and every pre-026 row reads
``'normal'`` via the column DEFAULT, which is exactly the pre-migration
behaviour. All rows here are synthetic.

The load-bearing test in this module is
:func:`test_check_constraint_matches_python_levels`. Migration 024 exists only
because a SQL CHECK and its Python mirror drifted apart — and because the Python
mirror raised BEFORE the INSERT was attempted, repairing the SQL alone looked
correct and was not. That failure mode is structural, not a one-off, so the two
definitions are pinned to each other here.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir, run_migrations
from brain.sensitivity import (
    DEFAULT_SENSITIVITY,
    SENSITIVITY_CHECK_CONSTRAINT,
    SENSITIVITY_INDEX,
    VALID_SENSITIVITY_LEVELS,
)

_MIGRATION_026 = migrations_dir() / "026_document_sensitivity.sql"


def _unapply_026(conn: psycopg.Connection[Any]) -> None:
    """Rewind an already-migrated schema to its pre-026 state.

    Reproduces what a production database looks like the moment before
    ``brain init`` picks 026 up — far more faithful (and far cheaper) than
    re-deriving a 001-025 schema by hand. Mirrors ``_unapply_025`` in
    ``tests/test_migration_025_updated_at.py``. Callers must carry
    ``@pytest.mark.fresh_schema``: this is DDL, so the TRUNCATE-only per-test
    reset cannot undo it.
    """
    conn.execute(f"DROP INDEX IF EXISTS {SENSITIVITY_INDEX}")
    conn.execute(
        f"ALTER TABLE documents DROP CONSTRAINT IF EXISTS "
        f"{SENSITIVITY_CHECK_CONSTRAINT}"
    )
    conn.execute("ALTER TABLE documents DROP COLUMN IF EXISTS sensitivity")
    conn.execute(
        "DELETE FROM schema_migrations WHERE name = %s", (_MIGRATION_026.name,)
    )


def _insert_legacy_row(conn: psycopg.Connection[Any], *, content_hash: str) -> None:
    """Insert a synthetic document without naming ``sensitivity``."""
    conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s)",
        ("Synthetic sensitivity probe", "probe body", content_hash, "note"),
    )


def test_sensitivity_column_exists_not_null_with_default(
    test_db: psycopg.Connection[Any],
) -> None:
    """RED-FIRST: 026 must add ``sensitivity TEXT NOT NULL DEFAULT 'normal'``."""
    row = test_db.execute(
        """
        SELECT data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = 'documents' AND column_name = 'sensitivity'
        """
    ).fetchone()

    assert row is not None, "migration 026 must add documents.sensitivity"
    assert row[0] == "text", "TEXT, not boolean — a third level must stay cheap"
    assert row[1] == "NO", "sensitivity must be NOT NULL"
    assert row[2] is not None
    assert DEFAULT_SENSITIVITY in str(row[2])


def test_check_constraint_matches_python_levels(
    test_db: psycopg.Connection[Any],
) -> None:
    """The SQL CHECK and ``brain.sensitivity``'s Literal cover the SAME set.

    This is the lockstep guard, and it is the reason this module exists.
    Migration 024 was needed only because ``interactions.py`` mirrored a SQL
    CHECK in a Python frozenset and the two drifted. Critically, the Python
    mirror rejected the new value BEFORE the INSERT was ever attempted, so a
    fix that widened only the SQL side passed a naive smoke test and still
    failed in production.

    Asserting on ``pg_get_constraintdef`` rather than on an INSERT's outcome is
    what makes this test see BOTH sides: it reads the live SQL definition and
    compares it against the live Python set, so widening either one alone turns
    it red.
    """
    row = test_db.execute(
        """
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conname = %s
          AND conrelid = 'documents'::regclass
        """,
        (SENSITIVITY_CHECK_CONSTRAINT,),
    ).fetchone()

    assert row is not None, (
        f"migration 026 must install a NAMED constraint "
        f"{SENSITIVITY_CHECK_CONSTRAINT!r} — an anonymous CHECK cannot be "
        f"swapped by a later migration"
    )
    definition = str(row[0])

    # Every Python-valid level must be permitted by the SQL constraint...
    for level in sorted(VALID_SENSITIVITY_LEVELS):
        assert f"'{level}'" in definition, (
            f"level {level!r} is valid in brain.sensitivity but absent from "
            f"the SQL CHECK: {definition}"
        )

    # ...and the SQL constraint must permit nothing the Python set does not.
    # Counting quoted literals catches a SQL-only widening, which is the
    # direction a Python-side assertion can never see.
    quoted_literals = definition.count("'") // 2
    assert quoted_literals == len(VALID_SENSITIVITY_LEVELS), (
        f"SQL CHECK permits {quoted_literals} value(s) but "
        f"brain.sensitivity defines {len(VALID_SENSITIVITY_LEVELS)}: {definition}"
    )


def test_check_rejects_an_unknown_level(test_db: psycopg.Connection[Any]) -> None:
    """A level outside the two literals is refused by the database itself."""
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO documents (title, content, content_hash, content_type, "
            "sensitivity) VALUES (%s, %s, %s, %s, %s)",
            ("Synthetic bad level", "body", "hash-026-bad", "note", "secret"),
        )


def test_partial_index_exists_and_is_scoped(
    test_db: psycopg.Connection[Any],
) -> None:
    """The index mirrors ``idx_documents_draft``: partial, on the small subset."""
    row = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'documents' AND indexname = %s",
        (SENSITIVITY_INDEX,),
    ).fetchone()

    assert row is not None, f"migration 026 must create {SENSITIVITY_INDEX}"
    indexdef = str(row[0])
    assert "WHERE" in indexdef.upper(), (
        "index must be PARTIAL — the confidential subset stays small and a "
        "full index on a two-value column earns nothing"
    )
    assert DEFAULT_SENSITIVITY in indexdef


def test_new_row_defaults_to_normal(test_db: psycopg.Connection[Any]) -> None:
    """An INSERT naming no ``sensitivity`` gets ``normal`` from the default.

    This is what makes 026 a behavioural no-op for every existing write path.
    """
    _insert_legacy_row(test_db, content_hash="hash-026-default")

    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE content_hash = %s",
        ("hash-026-default",),
    ).fetchone()
    assert row is not None
    assert row[0] == DEFAULT_SENSITIVITY


@pytest.mark.fresh_schema
def test_pre_026_rows_become_normal(test_db: psycopg.Connection[Any]) -> None:
    """A row that existed before 026 reads ``normal`` — never NULL.

    A NULL here would make ``is_confidential`` correct by accident but would
    break the NOT NULL contract every consumer relies on, and
    ``sensitivity <> 'normal'`` would silently exclude the row from the partial
    index.
    """
    # Arrange — rewind to pre-026 and seed a row the old schema would have.
    _unapply_026(test_db)
    _insert_legacy_row(test_db, content_hash="hash-026-legacy")

    # Act — exactly what `brain init` does on an existing database.
    applied = run_migrations(test_db)

    # Assert
    assert _MIGRATION_026.name in applied
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE content_hash = %s",
        ("hash-026-legacy",),
    ).fetchone()
    assert row is not None
    assert row[0] == DEFAULT_SENSITIVITY


@pytest.mark.fresh_schema
def test_migration_is_rerunnable(test_db: psycopg.Connection[Any]) -> None:
    """Applying 026 again is a clean no-op that preserves existing values.

    The re-run risk is not a crash — every statement is guarded. It is
    ``ADD COLUMN``'s DEFAULT re-stamping a row the user had marked
    confidential. ``IF NOT EXISTS`` is what prevents that, and this asserts it.
    Also pins that the guarded DDL stays singular: PostgreSQL 16 has no
    ``ADD CONSTRAINT IF NOT EXISTS``, so a missing ``DROP CONSTRAINT IF EXISTS``
    would raise on the second apply.
    """
    # Arrange — a row deliberately marked confidential.
    _insert_legacy_row(test_db, content_hash="hash-026-rerun")
    test_db.execute(
        "UPDATE documents SET sensitivity = 'confidential' WHERE content_hash = %s",
        ("hash-026-rerun",),
    )

    # Act — the session fixture applied it once; apply it twice more.
    sql = _MIGRATION_026.read_text()
    test_db.execute(sql)
    test_db.execute(sql)

    # Assert — the marking survived.
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE content_hash = %s",
        ("hash-026-rerun",),
    ).fetchone()
    assert row is not None and row[0] == "confidential"

    # ...and the DDL is still singular.
    columns = test_db.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'documents' AND column_name = 'sensitivity'"
    ).fetchone()
    assert columns is not None and columns[0] == 1

    constraints = test_db.execute(
        "SELECT count(*) FROM pg_constraint "
        "WHERE conname = %s AND conrelid = 'documents'::regclass",
        (SENSITIVITY_CHECK_CONSTRAINT,),
    ).fetchone()
    assert constraints is not None and constraints[0] == 1

    indexes = test_db.execute(
        "SELECT count(*) FROM pg_indexes "
        "WHERE tablename = 'documents' AND indexname = %s",
        (SENSITIVITY_INDEX,),
    ).fetchone()
    assert indexes is not None and indexes[0] == 1
