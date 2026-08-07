"""Vault-tier writes must survive the FTS-only (``BRAIN_EMBEDDER=none``) backend.

Regression for a real gap found 2026-07-26: ``brain capture`` — very likely the
first command a new user runs — died with ``EmbedDisabledError`` whenever the
null backend was configured, because the vault-sync write path called
``embedder.embed()`` unconditionally.

The ingest pipeline already handled this: ``brain.ingest._embed_chunks`` checks
the duck-typed ``produces_embeddings`` flag and substitutes NULL placeholders.
The vault path did not, so the two write paths disagreed and the documented
zero-Ollama onboarding route was broken for authored notes.

All content here is synthetic.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from brain.vault.sync import _embed_and_insert_chunks

pytestmark = pytest.mark.integration


class _NoVectorEmbedder:
    """Mirrors :class:`brain.embeddings.NullEmbedder`'s contract.

    ``embed()`` raising is the tripwire: the vault write path must never reach
    it once ``produces_embeddings`` is False. Declared on a plain class rather
    than a ``NullEmbedder`` subclass to prove the guard keys off the duck-typed
    flag, not the concrete type — the same discipline
    ``tests/test_search_none_backend.py`` uses.
    """

    produces_embeddings = False
    #: Matches ``conftest.FakeEmbedder``'s default, which is the dim the test
    #: schema's ``chunks.embedding`` column is migrated to.
    dim = 4096

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        raise AssertionError(
            "embed() must not be called when the embedder produces no vectors"
        )


class _VectorEmbedder:
    """A normal backend: never declares the flag, so it must embed as before."""

    dim = 4096

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


def _insert_document(conn: psycopg.Connection[Any], doc_id: str) -> None:
    conn.execute(
        "INSERT INTO documents (id, title, content, content_hash, content_type, kind) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (doc_id, "Synthetic capture", "body", uuid.uuid4().hex, "note", "vault"),
    )


def test_null_backend_stores_chunks_with_null_embeddings(
    test_db: psycopg.Connection,
) -> None:
    """The chunk row is written; only the vector is NULL.

    Storing nothing at all would be the wrong fix — it would make the note
    unsearchable by full text as well, which is the one retrieval leg the
    FTS-only backend still has.
    """
    # Arrange
    doc_id = str(uuid.uuid4())
    _insert_document(test_db, doc_id)

    # Act
    _embed_and_insert_chunks(
        test_db,
        embedder=_NoVectorEmbedder(),  # type: ignore[arg-type]
        document_id=doc_id,
        content="A synthetic captured thought about quarterly planning.",
        title="Synthetic capture",
        tags=["inbox"],
    )

    # Assert
    rows = test_db.execute(
        "SELECT content, embedding FROM chunks WHERE document_id = %s", (doc_id,)
    ).fetchall()
    assert rows, "expected at least one chunk row"
    assert all(embedding is None for _content, embedding in rows)
    assert all(content.strip() for content, _embedding in rows)


def test_null_backend_still_populates_the_fts_columns(
    test_db: psycopg.Connection,
) -> None:
    """Title/tag weighting must survive — it is what FTS-only ranking relies on."""
    # Arrange
    doc_id = str(uuid.uuid4())
    _insert_document(test_db, doc_id)

    # Act
    _embed_and_insert_chunks(
        test_db,
        embedder=_NoVectorEmbedder(),  # type: ignore[arg-type]
        document_id=doc_id,
        content="Quarterly planning notes for the synthetic project.",
        title="Synthetic capture",
        tags=["inbox", "planning"],
    )

    # Assert
    row = test_db.execute(
        "SELECT title_text, tags_text FROM chunks WHERE document_id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    title_text, tags_text = row
    assert "Synthetic capture" in title_text
    assert "inbox" in tags_text


def test_a_real_backend_is_unaffected(test_db: psycopg.Connection) -> None:
    """Backends that never declare the flag must embed exactly as before."""
    # Arrange
    doc_id = str(uuid.uuid4())
    _insert_document(test_db, doc_id)

    # Act
    _embed_and_insert_chunks(
        test_db,
        embedder=_VectorEmbedder(),  # type: ignore[arg-type]
        document_id=doc_id,
        content="Quarterly planning notes for the synthetic project.",
        title="Synthetic capture",
        tags=["inbox"],
    )

    # Assert
    embeddings = test_db.execute(
        "SELECT embedding FROM chunks WHERE document_id = %s", (doc_id,)
    ).fetchall()
    assert embeddings
    assert all(embedding is not None for (embedding,) in embeddings)


def test_empty_content_writes_no_chunks_under_either_backend(
    test_db: psycopg.Connection,
) -> None:
    """Whitespace-only bodies short-circuit before any embed decision."""
    # Arrange
    doc_id = str(uuid.uuid4())
    _insert_document(test_db, doc_id)

    # Act
    _embed_and_insert_chunks(
        test_db,
        embedder=_NoVectorEmbedder(),  # type: ignore[arg-type]
        document_id=doc_id,
        content="   \n  ",
        title="Synthetic capture",
        tags=[],
    )

    # Assert
    count = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id = %s", (doc_id,)
    ).fetchone()[0]
    assert count == 0
