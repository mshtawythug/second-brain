"""Tests for migration 004_relax_content_hash_uniqueness.sql.

Vault-tier notes can legitimately share body bytes; the original UNIQUE
constraint on ``documents.content_hash`` was an accidental block on that
case. Migration 004 replaces the table-wide constraint with a partial
unique index scoped to ``kind='ingested'``.
"""
import psycopg
import pytest


def test_constraint_dropped(test_db: psycopg.Connection) -> None:
    """The table-wide UNIQUE constraint is gone."""
    row = test_db.execute(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE table_schema = 'public' "
        "AND table_name = 'documents' "
        "AND constraint_name = 'documents_content_hash_key'"
    ).fetchone()
    assert row is None, "documents_content_hash_key should be dropped"


def test_partial_unique_index_exists(test_db: psycopg.Connection) -> None:
    """The new partial index is in place and scoped to ``kind='ingested'``."""
    row = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' "
        "AND tablename = 'documents' "
        "AND indexname = 'documents_content_hash_ingested_idx'"
    ).fetchone()
    assert row is not None, "documents_content_hash_ingested_idx should exist"
    indexdef = str(row[0])
    assert "UNIQUE" in indexdef
    assert "kind = 'ingested'" in indexdef


def test_two_vault_tier_rows_can_share_content_hash(
    test_db: psycopg.Connection,
) -> None:
    """Two vault-tier rows with the same content_hash insert without conflict."""
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind) "
        "VALUES ('A', 'body', 'shared-hash', 'note', 'vault')"
    )
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind) "
        "VALUES ('B', 'body', 'shared-hash', 'note', 'vault')"
    )
    cnt = test_db.execute(
        "SELECT count(*) FROM documents WHERE content_hash = 'shared-hash'"
    ).fetchone()
    assert cnt is not None
    assert cnt[0] == 2


def test_two_ingested_tier_rows_still_collide_on_content_hash(
    test_db: psycopg.Connection,
) -> None:
    """Ingested-tier dedup still enforced — re-ingest is still idempotent."""
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind) "
        "VALUES ('A', 'body', 'collide', 'note', 'ingested')"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        test_db.execute(
            "INSERT INTO documents "
            "(title, content, content_hash, content_type, kind) "
            "VALUES ('B', 'body', 'collide', 'note', 'ingested')"
        )


def test_vault_and_ingested_can_share_hash(test_db: psycopg.Connection) -> None:
    """A vault-tier row and an ingested-tier row with the same hash coexist."""
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind) "
        "VALUES ('I', 'body', 'cross-tier', 'note', 'ingested')"
    )
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind) "
        "VALUES ('V', 'body', 'cross-tier', 'note', 'vault')"
    )
    cnt = test_db.execute(
        "SELECT count(*) FROM documents WHERE content_hash = 'cross-tier'"
    ).fetchone()
    assert cnt is not None
    assert cnt[0] == 2
