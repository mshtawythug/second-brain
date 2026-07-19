"""The ``BRAIN_EMBEDDER=none`` → real-backend upgrade path (A11).

Because ``NullEmbedder.dim == 1024`` matches the arctic / voyage schema, moving
off the FTS-only backend is a plain ``brain reembed`` backfill — no destructive
column rebuild. This exercises that end-to-end against the real test DB:

    init(none) → ingest (NULL embeddings) → switch backend → reembed → finalize

and asserts the column finalizes (NOT NULL + HNSW) with no reset in between.
Carries ``@pytest.mark.fresh_schema`` because ``ensure_embedding_column`` and
``finalize_embedding_index`` mutate the schema (column rebuild / NOT NULL /
index DDL).
"""
from __future__ import annotations

import os

import psycopg
import pytest

from brain.db import ensure_embedding_column
from brain.embeddings import NullEmbedder
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import (
    count_chunks_missing_embedding,
    embedding_column_state,
    finalize_embedding_index,
    iter_chunks_missing_embedding,
)
from tests.conftest import FakeEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _null_ingest(conn: psycopg.Connection, *, title: str, content: str) -> str:
    result = ingest_document(
        conn,
        embedder=NullEmbedder(),
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=[],
    )
    assert result.document_id is not None
    return result.document_id


def _count_non_null_embeddings(conn: psycopg.Connection, document_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM chunks "
        "WHERE document_id = %s AND embedding IS NOT NULL",
        (document_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.fresh_schema
def test_null_to_arctic_upgrade_backfills_without_reset(
    test_db: psycopg.Connection,
) -> None:
    """A ``none`` corpus upgrades to a 1024-dim backend via a plain reembed."""
    # 1. `brain init` under the null backend: align the column to 1024 dims.
    ensure_embedding_column(test_db, NullEmbedder())
    pre = embedding_column_state(test_db)
    assert pre.column_type == "vector(1024)"
    assert pre.not_null is False
    assert pre.has_index is False

    # 2. Ingest under the null backend → NULL embeddings at the 1024-dim column.
    doc_id = _null_ingest(
        test_db,
        title="Upgrade note",
        content="Synthetic body for the none-to-arctic upgrade backfill path.",
    )
    assert _count_non_null_embeddings(test_db, doc_id) == 0
    assert count_chunks_missing_embedding(test_db) > 0

    # 3. Switch to a real 1024-dim backend — no destructive reset needed
    #    (ensure_embedding_column is a no-op because the dims already agree).
    arctic_like = FakeEmbedder(dim=1024)
    ensure_embedding_column(test_db, arctic_like)  # must NOT raise
    assert embedding_column_state(test_db).column_type == "vector(1024)"

    # 4. `brain reembed`: backfill the NULL embeddings, then finalize.
    for batch in iter_chunks_missing_embedding(test_db, batch_size=32):
        vectors = arctic_like.embed(
            [c.content for c in batch], input_type="document"
        )
        for chunk, vec in zip(batch, vectors, strict=True):
            test_db.execute(
                "UPDATE chunks SET embedding = %s WHERE id = %s", (vec, chunk.id)
            )
    assert count_chunks_missing_embedding(test_db) == 0
    finalize_embedding_index(test_db, arctic_like)

    # 5. Column is finalized: NOT NULL + HNSW present, no reset was required.
    post = embedding_column_state(test_db)
    assert post.not_null is True
    assert post.has_index is True
