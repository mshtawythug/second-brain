"""Tests for ``brain.interactions`` — the append-only feedback log writer.

Covers the Python-side enum gates, the DB-level ``CHECK`` constraints,
the FK ``ON DELETE CASCADE`` behavior, and the three migration-010
indexes. Uses the real test DB via the ``test_db`` fixture.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from brain.errors import InteractionError
from brain.interactions import record_interaction


def _seed_doc(conn: psycopg.Connection) -> str:
    """Insert one bare-bones document row and return its UUID as text.

    Bypasses the chunk pipeline — the interactions table only needs a
    valid ``documents.id`` for the FK. Saves the test from carrying a
    fake embedder when it doesn't exercise the search path.
    """
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        ("seed", "seed body", "seed-hash", "note"),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_record_interaction_inserts_row(test_db: psycopg.Connection) -> None:
    """Happy path: RETURNING id resolves and a SELECT reads the row back."""
    doc_id = _seed_doc(test_db)
    sid = uuid.uuid4()
    new_id = record_interaction(
        test_db,
        document_id=doc_id,
        action="opened",
        source="mcp",
        query="company-id notes",
        session_id=sid,
    )
    # Verify the UUID round-trips by parsing it.
    assert uuid.UUID(new_id)
    row = test_db.execute(
        "SELECT document_id::text, query, action, source, session_id::text "
        "FROM interactions WHERE id = %s",
        (new_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == doc_id
    assert row[1] == "company-id notes"
    assert row[2] == "opened"
    assert row[3] == "mcp"
    assert row[4] == str(sid)


def test_record_interaction_unknown_action_raises_before_sql(
    test_db: psycopg.Connection,
) -> None:
    """Bad ``action`` raises ``InteractionError`` and inserts nothing."""
    doc_id = _seed_doc(test_db)
    with pytest.raises(InteractionError, match="unknown action"):
        record_interaction(
            test_db,
            document_id=doc_id,
            action="invalid",  # type: ignore[arg-type]
            source="cli",
        )
    count = test_db.execute("SELECT count(*) FROM interactions").fetchone()
    assert count is not None
    assert count[0] == 0


def test_record_interaction_unknown_source_raises_before_sql(
    test_db: psycopg.Connection,
) -> None:
    """Bad ``source`` raises ``InteractionError`` and inserts nothing."""
    doc_id = _seed_doc(test_db)
    with pytest.raises(InteractionError, match="unknown source"):
        record_interaction(
            test_db,
            document_id=doc_id,
            action="opened",
            source="badsource",  # type: ignore[arg-type]
        )
    count = test_db.execute("SELECT count(*) FROM interactions").fetchone()
    assert count is not None
    assert count[0] == 0


def test_db_check_constraint_catches_raw_bad_action(
    test_db: psycopg.Connection,
) -> None:
    """Raw INSERT with a bad action trips the DB-level ``CHECK`` (defense-in-depth)."""
    doc_id = _seed_doc(test_db)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO interactions (document_id, action, source) "
            "VALUES (%s, %s, %s)",
            (doc_id, "completely-bogus", "cli"),
        )


def test_db_check_constraint_catches_raw_bad_source(
    test_db: psycopg.Connection,
) -> None:
    """Raw INSERT with a bad source trips the DB-level ``CHECK``."""
    doc_id = _seed_doc(test_db)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO interactions (document_id, action, source) "
            "VALUES (%s, %s, %s)",
            (doc_id, "opened", "facebook"),
        )


def test_db_accepts_wiki_source_for_future_wave(
    test_db: psycopg.Connection,
) -> None:
    """Schema must accept ``source='wiki'`` so the deferred click wave is
    purely additive — no schema migration required when it lands."""
    doc_id = _seed_doc(test_db)
    # Direct INSERT — the Python-side enum allows it too, but this also
    # locks the SQL-level acceptance.
    test_db.execute(
        "INSERT INTO interactions (document_id, action, source) "
        "VALUES (%s, %s, %s)",
        (doc_id, "clicked", "wiki"),
    )
    row = test_db.execute(
        "SELECT count(*) FROM interactions WHERE source = 'wiki'"
    ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_record_interaction_cascades_on_document_delete(
    test_db: psycopg.Connection,
) -> None:
    """``ON DELETE CASCADE`` removes interactions when the doc goes away."""
    doc_id = _seed_doc(test_db)
    record_interaction(
        test_db,
        document_id=doc_id,
        action="rated_useful",
        source="cli",
    )
    test_db.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
    count = test_db.execute(
        "SELECT count(*) FROM interactions WHERE document_id = %s",
        (doc_id,),
    ).fetchone()
    assert count is not None
    assert count[0] == 0


def test_record_interaction_session_id_round_trips(
    test_db: psycopg.Connection,
) -> None:
    """A ``uuid.UUID`` session_id reads back as the same string."""
    doc_id = _seed_doc(test_db)
    sid = uuid.uuid4()
    new_id = record_interaction(
        test_db,
        document_id=doc_id,
        action="opened",
        source="mcp",
        query="x",
        session_id=sid,
    )
    row = test_db.execute(
        "SELECT session_id::text FROM interactions WHERE id = %s",
        (new_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == str(sid)


def test_record_interaction_null_query_allowed(
    test_db: psycopg.Connection,
) -> None:
    """``query=None`` writes ``NULL`` (the CLI rating path)."""
    doc_id = _seed_doc(test_db)
    new_id = record_interaction(
        test_db,
        document_id=doc_id,
        action="rated_irrelevant",
        source="cli",
    )
    row = test_db.execute(
        "SELECT query FROM interactions WHERE id = %s", (new_id,)
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_record_interaction_null_session_id_default(
    test_db: psycopg.Connection,
) -> None:
    """Omitting ``session_id`` leaves the column NULL."""
    doc_id = _seed_doc(test_db)
    new_id = record_interaction(
        test_db,
        document_id=doc_id,
        action="rated_useful",
        source="cli",
    )
    row = test_db.execute(
        "SELECT session_id FROM interactions WHERE id = %s", (new_id,)
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_migration_010_creates_three_indexes(test_db: psycopg.Connection) -> None:
    """Lock the three plan-prescribed indexes on the ``interactions`` table."""
    rows = test_db.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'interactions' "
        "ORDER BY indexname"
    ).fetchall()
    index_names = {str(r[0]) for r in rows}
    # PRIMARY KEY ships with its own implicit unique index.
    assert "interactions_pkey" in index_names
    assert "interactions_document_at_idx" in index_names
    assert "interactions_action_idx" in index_names
    assert "interactions_session_idx" in index_names


def test_migration_010_session_index_is_partial(test_db: psycopg.Connection) -> None:
    """The session-id index excludes NULL rows — keeps it small."""
    row = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'interactions_session_idx'"
    ).fetchone()
    assert row is not None
    assert "session_id IS NOT NULL" in str(row[0])
