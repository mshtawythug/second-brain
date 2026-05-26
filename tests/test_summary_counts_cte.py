"""Regression tests for the single-round-trip CTE rewrite of summary_counts().

Verifies that the rewritten function returns the same shape and values as
the previous five-query implementation. All rows are synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import StatusCounts, summary_counts


def _seed(conn: psycopg.Connection, fake_embedder: Any, *, title: str = "doc") -> str:
    # Use title-derived content so each doc gets a unique content_hash.
    result = ingest_document(
        conn,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content=f"body content for {title}",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=[],
    )
    assert result.document_id is not None
    return result.document_id


def test_summary_counts_empty_db(test_db: psycopg.Connection) -> None:
    """Empty brain returns zero counts and None last_ingest."""
    counts = summary_counts(test_db)
    assert isinstance(counts, StatusCounts)
    assert counts.documents == 0
    assert counts.chunks == 0
    assert counts.sources == 0
    assert counts.last_ingest is None
    assert counts.by_kind == []


def test_summary_counts_single_document(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """One manual doc → documents=1, sources=0 (manual has no source row)."""
    _seed(test_db, fake_embedder, title="Alpha")
    counts = summary_counts(test_db)
    assert counts.documents == 1
    assert counts.chunks >= 1
    assert counts.last_ingest is not None
    # Manual (no source row) shows as 'manual' kind.
    assert len(counts.by_kind) == 1
    assert counts.by_kind[0][0] == "manual"
    assert counts.by_kind[0][1] == 1


def test_summary_counts_multiple_documents(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """Three docs → documents=3, by_kind sums correctly."""
    for i in range(3):
        _seed(test_db, fake_embedder, title=f"Doc {i}")
    counts = summary_counts(test_db)
    assert counts.documents == 3
    total_from_by_kind = sum(c for _, c in counts.by_kind)
    assert total_from_by_kind == 3


def test_summary_counts_by_kind_sorted_desc(
    test_db: psycopg.Connection, fake_embedder: Any
) -> None:
    """by_kind is sorted by count descending (most common source first)."""
    # Two manual docs + one krisp doc (via direct INSERT to simulate).
    _seed(test_db, fake_embedder, title="Manual A")
    _seed(test_db, fake_embedder, title="Manual B")
    # Insert a krisp-sourced doc directly.
    test_db.execute(
        "INSERT INTO sources (kind, external_id) VALUES (%s, %s)",
        ("krisp", "meeting-001"),
    )
    src_row = test_db.execute(
        "SELECT id FROM sources WHERE external_id = %s", ("meeting-001",)
    ).fetchone()
    assert src_row is not None
    test_db.execute(
        "INSERT INTO documents (source_id, title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s, %s)",
        (src_row[0], "Krisp doc", "transcript body", "krisptest-hash", "transcript"),
    )
    counts = summary_counts(test_db)
    assert counts.documents == 3
    # 'manual' has 2 docs; 'krisp' has 1 — manual should come first.
    kinds = [k for k, _ in counts.by_kind]
    assert kinds[0] == "manual"


def test_summary_counts_return_type(test_db: psycopg.Connection) -> None:
    """Return value is a StatusCounts with the expected field names."""
    counts = summary_counts(test_db)
    assert hasattr(counts, "documents")
    assert hasattr(counts, "chunks")
    assert hasattr(counts, "sources")
    assert hasattr(counts, "last_ingest")
    assert hasattr(counts, "by_kind")
    assert isinstance(counts.by_kind, list)
