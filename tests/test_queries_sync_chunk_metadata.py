"""Unit tests for :func:`brain.queries.sync_chunk_search_metadata`.

The helper is the single ongoing-enforcement boundary that keeps
``chunks.title_text`` and ``chunks.tags_text`` in lockstep with their parent
``documents`` row when chunks are NOT being re-inserted (title-only +
tags-only edits, bulk normalize-tags). Tests cover: rewrite on title change,
rewrite on tags change, rowcount semantics, no-op idempotency, and document
isolation (sync on doc A must not touch doc B's chunks).
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import sync_chunk_search_metadata


def _seed(
    conn: psycopg.Connection[Any],
    fake_embedder: Any,
    *,
    title: str = "Initial",
    tags: list[str] | None = None,
    content: str = "alpha body content here",
) -> str:
    result = ingest_document(
        conn,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=tags or [],
    )
    assert result.document_id is not None
    return result.document_id


def _chunk_metadata(
    conn: psycopg.Connection[Any], document_id: str
) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT title_text, tags_text FROM chunks "
        "WHERE document_id=%s ORDER BY chunk_index",
        (document_id,),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def test_sync_propagates_title_change_to_every_chunk(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    doc_id = _seed(test_db, fake_embedder, title="Old", tags=["t"])
    test_db.execute(
        "UPDATE documents SET title=%s WHERE id=%s", ("New Title", doc_id)
    )
    updated = sync_chunk_search_metadata(test_db, doc_id)
    assert updated >= 1
    for title_text, tags_text in _chunk_metadata(test_db, doc_id):
        assert title_text == "New Title"
        assert tags_text == "t"


def test_sync_propagates_tags_change_to_every_chunk(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    doc_id = _seed(test_db, fake_embedder, title="Stable", tags=["one"])
    test_db.execute(
        "UPDATE documents SET tags=%s WHERE id=%s",
        (["one", "two"], doc_id),
    )
    updated = sync_chunk_search_metadata(test_db, doc_id)
    assert updated >= 1
    for title_text, tags_text in _chunk_metadata(test_db, doc_id):
        assert title_text == "Stable"
        assert tags_text == "one two"


def test_sync_returns_rowcount(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Returns the number of chunk rows actually rewritten."""
    # Use a body long enough to produce multiple chunks.
    paragraphs = [f"Paragraph {i} with some body content." for i in range(10)]
    big_body = "\n\n".join(paragraphs * 200)
    doc_id = _seed(test_db, fake_embedder, title="Big", content=big_body)
    chunk_count_row = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id=%s", (doc_id,)
    ).fetchone()
    assert chunk_count_row is not None
    chunk_count = int(chunk_count_row[0])
    assert chunk_count >= 1

    test_db.execute(
        "UPDATE documents SET title=%s WHERE id=%s", ("Big Renamed", doc_id)
    )
    updated = sync_chunk_search_metadata(test_db, doc_id)
    assert updated == chunk_count


def test_sync_is_no_op_when_already_consistent(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A second call after a converged sync returns 0 (IS DISTINCT FROM guard)."""
    doc_id = _seed(test_db, fake_embedder, title="T", tags=["x"])
    test_db.execute(
        "UPDATE documents SET title=%s WHERE id=%s", ("T2", doc_id)
    )
    first = sync_chunk_search_metadata(test_db, doc_id)
    assert first >= 1
    second = sync_chunk_search_metadata(test_db, doc_id)
    assert second == 0


def test_sync_does_not_touch_other_documents(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """sync(doc A) must not rewrite doc B's chunks."""
    # Distinct content per doc — stdin ingests dedup by content_hash, so
    # using the same body twice would collapse into one row and defeat
    # the isolation invariant we want to assert.
    doc_a = _seed(
        test_db,
        fake_embedder,
        title="A-title",
        tags=["a"],
        content="doc A body content alpha",
    )
    doc_b = _seed(
        test_db,
        fake_embedder,
        title="B-title",
        tags=["b"],
        content="doc B body content beta",
    )
    assert doc_a != doc_b, "seed produced duplicate ids — test setup bug"

    # Mutate A's parent row and call sync only for A.
    test_db.execute(
        "UPDATE documents SET title=%s WHERE id=%s", ("A-renamed", doc_a)
    )
    sync_chunk_search_metadata(test_db, doc_a)

    # B's chunks must still reflect its original title/tags.
    for title_text, tags_text in _chunk_metadata(test_db, doc_b):
        assert title_text == "B-title"
        assert tags_text == "b"
