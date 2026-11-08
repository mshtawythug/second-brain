"""``db.ensure_relational_embedding_columns`` at every call site (RM-20 AC5).

``chunks.embedding`` and ``document_embeddings.embedding`` are reconciled
together, every target checked before any is resized. Exercised directly, through
``brain init``, through ``backup.restore``'s reconcile step and through both demo
call sites. ``fresh_schema`` tests start from the migrated schema (chunks at 4096
after migration 002, the document table at 1024) and restore it afterwards. All
data is synthetic.
"""
from __future__ import annotations

import inspect
import uuid
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli, demo
from brain.db import ensure_embedding_column, ensure_relational_embedding_columns
from brain.errors import BrainError
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder


def _dim(conn: psycopg.Connection[Any], table: str) -> str:
    row = conn.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = %s::regclass AND attname = 'embedding'",
        (table,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _doc(conn: psycopg.Connection[Any]) -> str:
    return str(
        conn.execute(
            "INSERT INTO documents (title, content, content_hash, content_type) "
            "VALUES ('Synthetic', 'synthetic body', %s, 'note') RETURNING id::text",
            (str(uuid.uuid4()),),
        ).fetchone()[0]
    )


@pytest.mark.fresh_schema
def test_a_qwen3_dim_embedder_resizes_the_empty_document_table(
    test_db: psycopg.Connection[Any],
) -> None:
    ensure_relational_embedding_columns(test_db, FakeEmbedder(dim=4096))

    assert _dim(test_db, "chunks") == "vector(4096)"
    assert _dim(test_db, "document_embeddings") == "vector(4096)"


@pytest.mark.fresh_schema
def test_a_matching_dim_is_a_no_op(test_db: psycopg.Connection[Any]) -> None:
    ensure_relational_embedding_columns(test_db, FakeEmbedder(dim=4096))
    ensure_relational_embedding_columns(test_db, FakeEmbedder(dim=4096))

    assert _dim(test_db, "document_embeddings") == "vector(4096)"


@pytest.mark.fresh_schema
def test_populated_document_vectors_at_another_dim_are_refused_with_the_reset_hint(
    test_db: psycopg.Connection[Any],
) -> None:
    # The document table holds a 1024 vector; chunks are at 4096, so a 4096
    # embedder leaves chunks alone and only the document table must refuse.
    # The preflight itself (chunks would otherwise be resized first) is the
    # next test's subject.
    test_db.execute(
        "INSERT INTO document_embeddings (document_id, embedding, basis, basis_hash, embedder) "
        "VALUES (%s, %s, 'title_head', 'h', 'synthetic|m|1024')",
        (_doc(test_db), [0.1] * 1024),
    )

    with pytest.raises(BrainError, match="document_embeddings.embedding") as refused:
        ensure_relational_embedding_columns(test_db, FakeEmbedder(dim=4096))

    assert "destructive reset" in str(refused.value)

    assert _dim(test_db, "chunks") == "vector(4096)"
    assert _dim(test_db, "document_embeddings") == "vector(1024)"


@pytest.mark.fresh_schema
def test_the_preflight_leaves_chunks_untouched_when_only_the_document_table_refuses(
    test_db: psycopg.Connection[Any],
) -> None:
    # Arrange: chunks empty at 4096, the document table populated at 4096.
    ensure_embedding_column(test_db, FakeEmbedder(dim=4096), "document_embeddings", "embedding")
    test_db.execute(
        "INSERT INTO document_embeddings (document_id, embedding, basis, basis_hash, embedder) "
        "VALUES (%s, %s, 'title_head', 'h', 'synthetic|m|4096')",
        (_doc(test_db), [0.1] * 4096),
    )

    # Act: a 1024 embedder. Without the preflight chunks (empty) would be
    # resized before the document table refused.
    with pytest.raises(BrainError, match="document_embeddings.embedding"):
        ensure_relational_embedding_columns(test_db, FakeEmbedder(dim=1024))

    # Assert
    assert _dim(test_db, "chunks") == "vector(4096)"


@pytest.mark.fresh_schema
def test_both_empty_columns_are_resized_together(test_db: psycopg.Connection[Any]) -> None:
    ensure_relational_embedding_columns(test_db, FakeEmbedder(dim=1024))

    assert _dim(test_db, "chunks") == "vector(1024)"
    assert _dim(test_db, "document_embeddings") == "vector(1024)"


@pytest.mark.fresh_schema
def test_brain_init_on_a_qwen3_dim_database_resizes_the_document_table(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``init`` builds its embedder with ``make_embedder(cfg)``; qwen3 is 4096
    # dims and constructing it opens no connection.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_EMBEDDER", "qwen3")
    assert _dim(test_db, "document_embeddings") == "vector(1024)"

    result = CliRunner().invoke(cli.app, ["init"])

    assert result.exit_code == 0, result.output
    assert "dim=4096" in result.output
    assert _dim(test_db, "document_embeddings") == "vector(4096)"


def test_the_demo_container_bring_up_reconciles_both_columns() -> None:
    # AC5's fourth call site, ``demo.provision``, starts a Docker container, so
    # it is read from the source; the seed and restore sites are exercised for
    # real (``tests/test_demo_seed.py``, ``tests/test_restore_swap.py``).
    source = inspect.getsource(demo.provision)

    assert "ensure_relational_embedding_columns(conn, DemoEmbedder())" in source
    assert "ensure_embedding_column(" not in source
