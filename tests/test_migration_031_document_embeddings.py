"""Real-DB tests for migration ``031_document_embeddings.sql`` (RM-20).

One additive table, one vector per document. The contract is the ticket's DDL:
the ``basis`` CHECK, the primary key, ``ON DELETE CASCADE``, the house starting
dimension, and a clean re-apply. All rows are synthetic.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir

_MIGRATION_031 = migrations_dir() / "031_document_embeddings.sql"


def _doc(conn: psycopg.Connection[Any]) -> str:
    return str(
        conn.execute(
            "INSERT INTO documents (title, content, content_hash, content_type) "
            "VALUES ('Synthetic', 'synthetic body', %s, 'note') RETURNING id::text",
            (str(uuid.uuid4()),),
        ).fetchone()[0]
    )


def _vector_row(conn: psycopg.Connection[Any], document_id: str, basis: str = "title_head") -> None:
    conn.execute(
        "INSERT INTO document_embeddings (document_id, basis, basis_hash, embedder) "
        "VALUES (%s, %s, 'h', 'synthetic|model|1024')",
        (document_id, basis),
    )


@pytest.mark.fresh_schema
def test_the_column_starts_at_the_house_dimension(test_db: psycopg.Connection[Any]) -> None:
    row = test_db.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'document_embeddings'::regclass AND attname = 'embedding'"
    ).fetchone()

    assert row == ("vector(1024)",)


def test_a_row_records_its_creation_time(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _vector_row(test_db, doc)

    row = test_db.execute(
        "SELECT basis, created_at IS NOT NULL FROM document_embeddings WHERE document_id = %s",
        (doc,),
    ).fetchone()

    assert row == ("title_head", True)


def test_a_basis_outside_the_two_kinds_is_refused(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _vector_row(test_db, _doc(test_db), basis="title_body")


def test_one_row_per_document(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _vector_row(test_db, doc)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _vector_row(test_db, doc)


def test_a_row_needs_an_existing_document(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _vector_row(test_db, str(uuid.uuid4()))


def test_deleting_the_document_deletes_its_vector(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _vector_row(test_db, doc)

    test_db.execute("DELETE FROM documents WHERE id = %s", (doc,))

    assert test_db.execute("SELECT count(*) FROM document_embeddings").fetchone() == (0,)


def test_re_applying_the_migration_is_a_no_op(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    _vector_row(test_db, doc)

    test_db.execute(_MIGRATION_031.read_text())

    assert test_db.execute("SELECT count(*) FROM document_embeddings").fetchone() == (1,)
