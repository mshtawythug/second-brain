"""Real-DB tests for migration ``030_near_duplicates.sql`` (RM-19).

Two additive tables: ``document_minhash`` (one signature per document) and ``near_duplicates``
(one row per verified pair, ``doc_a < doc_b``). The contract is the ticket's DDL: the status
CHECK, the ordering CHECK, every ``ON DELETE CASCADE`` (AC4), the GIN index on ``bands``, and a
clean re-apply. All rows are synthetic.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from brain.db import migrations_dir

_MIGRATION_030 = migrations_dir() / "030_near_duplicates.sql"


def _doc(conn: psycopg.Connection[Any]) -> str:
    return str(
        conn.execute(
            "INSERT INTO documents (title, content, content_hash, content_type) "
            "VALUES ('Synthetic', 'synthetic body', %s, 'note') RETURNING id::text",
            (str(uuid.uuid4()),),
        ).fetchone()[0]
    )


def _pair(conn: psycopg.Connection[Any]) -> tuple[str, str]:
    a, b = sorted((_doc(conn), _doc(conn)), key=uuid.UUID)
    conn.execute(
        "INSERT INTO near_duplicates (doc_a, doc_b, similarity) VALUES (%s, %s, 0.9)", (a, b)
    )
    return a, b


def test_a_pair_defaults_to_open_with_a_detection_time(test_db: psycopg.Connection[Any]) -> None:
    _pair(test_db)

    row = test_db.execute(
        "SELECT status, detected_at IS NOT NULL FROM near_duplicates"
    ).fetchone()

    assert row == ("open", True)


def test_status_outside_the_three_values_is_refused(test_db: psycopg.Connection[Any]) -> None:
    a, b = _pair(test_db)

    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "UPDATE near_duplicates SET status = 'merged' WHERE doc_a = %s AND doc_b = %s", (a, b)
        )


def test_a_pair_must_be_stored_in_order(test_db: psycopg.Connection[Any]) -> None:
    a, b = sorted((_doc(test_db), _doc(test_db)), key=uuid.UUID)

    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO near_duplicates (doc_a, doc_b, similarity) VALUES (%s, %s, 0.9)", (b, a)
        )


@pytest.mark.parametrize("end", ["doc_a", "doc_b"])
def test_deleting_either_document_deletes_the_pair(
    test_db: psycopg.Connection[Any], end: str
) -> None:
    # AC4.
    a, b = _pair(test_db)

    test_db.execute("DELETE FROM documents WHERE id = %s", (a if end == "doc_a" else b,))

    assert test_db.execute("SELECT count(*) FROM near_duplicates").fetchone() == (0,)


def test_deleting_a_document_deletes_its_signature(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    test_db.execute(
        "INSERT INTO document_minhash (document_id, content_hash, signature, bands) "
        "VALUES (%s, 'h', ARRAY[1,2]::bigint[], ARRAY[3]::bigint[])",
        (doc,),
    )

    test_db.execute("DELETE FROM documents WHERE id = %s", (doc,))

    assert test_db.execute("SELECT count(*) FROM document_minhash").fetchone() == (0,)


def test_bands_carry_a_gin_index(test_db: psycopg.Connection[Any]) -> None:
    row = test_db.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_document_minhash_bands'"
    ).fetchone()

    assert row is not None
    assert "USING gin (bands)" in row[0]


def test_bigint_extremes_round_trip(test_db: psycopg.Connection[Any]) -> None:
    doc = _doc(test_db)
    extremes = [-(2**63), 2**63 - 1]
    test_db.execute(
        "INSERT INTO document_minhash (document_id, content_hash, signature, bands) "
        "VALUES (%s, 'h', %s, %s)",
        (doc, extremes, extremes),
    )

    row = test_db.execute("SELECT signature, bands FROM document_minhash").fetchone()

    assert row == (extremes, extremes)


def test_the_migration_reapplies_cleanly(test_db: psycopg.Connection[Any]) -> None:
    _pair(test_db)

    test_db.execute(_MIGRATION_030.read_text())

    assert test_db.execute("SELECT count(*) FROM near_duplicates").fetchone() == (1,)
