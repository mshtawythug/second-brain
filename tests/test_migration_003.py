"""Tests for migration 003_vault_model.sql — schema additions for vault model.

Phase 1 contract: additive only. Existing rows transition to ``kind='ingested'``
via the column default. ``vault_path`` is nullable. ``links`` and
``unresolved_links`` tables are new.
"""
from collections.abc import Callable

import psycopg
import pytest


def _column_info(conn: psycopg.Connection, table: str, col: str) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s AND column_name=%s
        """,
        (table, col),
    ).fetchone()
    if row is None:
        raise AssertionError(f"{table}.{col} not found")
    return {
        "data_type": row[0],
        "is_nullable": row[1],
        "column_default": row[2],
    }


def test_documents_kind_column_exists_with_default(
    test_db: psycopg.Connection,
) -> None:
    info = _column_info(test_db, "documents", "kind")
    assert info["data_type"] == "text"
    assert info["is_nullable"] == "NO"
    # Column default is the SQL literal — psql renders it as a quoted string.
    assert "ingested" in str(info["column_default"])


def test_documents_kind_check_constraint(test_db: psycopg.Connection) -> None:
    # Violation should be rejected.
    with pytest.raises(psycopg.errors.CheckViolation), test_db.transaction():
        test_db.execute(
            "INSERT INTO documents "
            "(title, content, content_hash, content_type, kind) "
            "VALUES ('t', 'b', 'hash-bogus', 'note', 'illegal-kind')"
        )


def test_documents_vault_path_column_is_nullable(
    test_db: psycopg.Connection,
) -> None:
    info = _column_info(test_db, "documents", "vault_path")
    assert info["data_type"] == "text"
    assert info["is_nullable"] == "YES"


def test_documents_vault_path_unique_when_not_null(
    test_db: psycopg.Connection,
) -> None:
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, vault_path, kind) "
        "VALUES ('a', 'a', 'hash-a', 'note', 'notes/a.md', 'vault')"
    )
    # Same vault_path → unique violation.
    with pytest.raises(psycopg.errors.UniqueViolation), test_db.transaction():
        test_db.execute(
            "INSERT INTO documents "
            "(title, content, content_hash, content_type, vault_path, kind) "
            "VALUES ('b', 'b', 'hash-b', 'note', 'notes/a.md', 'vault')"
        )


def test_documents_vault_path_allows_multiple_nulls(
    test_db: psycopg.Connection,
) -> None:
    # Partial unique index: NULLs are not constrained.
    test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES ('a', 'a', 'hash-a', 'note')"
    )
    test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES ('b', 'b', 'hash-b', 'note')"
    )
    row = test_db.execute(
        "SELECT count(*) FROM documents WHERE vault_path IS NULL"
    ).fetchone()
    assert row is not None and row[0] == 2


def test_documents_kind_index_present(test_db: psycopg.Connection) -> None:
    row = test_db.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='documents_kind_idx'"
    ).fetchone()
    assert row is not None


def test_documents_vault_path_index_present(
    test_db: psycopg.Connection,
) -> None:
    row = test_db.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='documents_vault_path_idx'"
    ).fetchone()
    assert row is not None


def test_links_table_schema(test_db: psycopg.Connection) -> None:
    rows = test_db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='links' "
        "ORDER BY column_name"
    ).fetchall()
    cols = {r[0] for r in rows}
    assert cols == {
        "id",
        "src_document_id",
        "dst_document_id",
        "link_text",
        "link_kind",
        "display_text",
    }


def test_links_link_kind_check_constraint(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    src = seed_doc(title="A", content="aaa")
    dst = seed_doc(title="B", content="bbb")
    with pytest.raises(psycopg.errors.CheckViolation), test_db.transaction():
        test_db.execute(
            "INSERT INTO links "
            "(src_document_id, dst_document_id, link_text, link_kind) "
            "VALUES (%s, %s, 'B', 'illegal')",
            (src, dst),
        )


def test_links_unique_constraint_blocks_duplicate(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    src = seed_doc(title="A", content="aaa")
    dst = seed_doc(title="B", content="bbb")
    test_db.execute(
        "INSERT INTO links "
        "(src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s, %s, 'B', 'wiki')",
        (src, dst),
    )
    with pytest.raises(psycopg.errors.UniqueViolation), test_db.transaction():
        test_db.execute(
            "INSERT INTO links "
            "(src_document_id, dst_document_id, link_text, link_kind) "
            "VALUES (%s, %s, 'B', 'wiki')",
            (src, dst),
        )


def test_links_cascade_delete_on_src(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    src = seed_doc(title="A", content="aaa")
    dst = seed_doc(title="B", content="bbb")
    test_db.execute(
        "INSERT INTO links "
        "(src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s, %s, 'B', 'wiki')",
        (src, dst),
    )
    test_db.execute("DELETE FROM documents WHERE id=%s", (src,))
    row = test_db.execute("SELECT count(*) FROM links").fetchone()
    assert row is not None and row[0] == 0


def test_unresolved_links_table_exists(test_db: psycopg.Connection) -> None:
    rows = test_db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='unresolved_links' "
        "ORDER BY column_name"
    ).fetchall()
    cols = {r[0] for r in rows}
    assert cols == {
        "id",
        "src_document_id",
        "link_text",
        "link_kind",
        "display_text",
    }


def test_existing_rows_default_to_ingested_kind(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """Pre-vault-model rows transition cleanly to kind='ingested' via the default.

    Simulates the production-data scenario: documents already exist when
    migration 003 lands. Each row's ``kind`` is filled by the column default;
    no manual backfill is required.
    """
    doc_id = seed_doc(title="legacy", content="legacy body")
    row = test_db.execute(
        "SELECT kind, vault_path FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "ingested"
    assert row[1] is None


def test_can_insert_vault_kind_with_path(test_db: psycopg.Connection) -> None:
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind, vault_path) "
        "VALUES ('vault note', 'b', 'hash-vault', 'note', 'vault', 'notes/n.md')"
    )
    row = test_db.execute(
        "SELECT kind, vault_path FROM documents WHERE content_hash='hash-vault'"
    ).fetchone()
    assert row == ("vault", "notes/n.md")
