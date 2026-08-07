"""Tests for the exact total-match count and latency phases on hybrid search.

``SearchDiagnostics.total_documents`` is a SIBLING of ``fts_count``, not a
replacement: ``fts_count`` stays capped at ``CANDIDATE_LIMIT`` (only its zero
case is exact, which is what ``brain gaps`` keys off), while
``total_documents`` is an exact ``count(DISTINCT document_id)`` over the
lexical predicate. Both semantics are asserted here so a future refactor
cannot quietly merge them.

All fixture data is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.search import CANDIDATE_LIMIT, SearchDiagnostics, hybrid_search


def _seed(
    conn: psycopg.Connection[Any],
    embedder: Any,
    items: list[tuple[str, str]],
    *,
    tags: dict[str, list[str]] | None = None,
) -> None:
    """Ingest ``(title, body)`` pairs as manual documents."""
    for title, content in items:
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=title,
                content=content,
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:{title}",
            tags=(tags or {}).get(title, []),
        )


def test_total_documents_exceeds_returned_page(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """RED-FIRST: 7 matching docs, a 2-row page, and an exact total of 7."""
    # Arrange
    _seed(
        test_db,
        fake_embedder,
        # Bodies must differ: ``documents.content_hash`` is UNIQUE, so
        # identical bodies would dedup into a single row and the test would
        # silently assert against a corpus of one.
        [
            (f"Quarterly note {i}", f"The quarterly review covered budget {i}.")
            for i in range(7)
        ],
    )
    diag = SearchDiagnostics()

    # Act
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="quarterly",
        limit=2,
        fts_only=True,
        diagnostics=diag,
        total_count=True,
    )

    # Assert
    assert len(results) == 2
    assert diag.total_documents == 7


def test_total_respects_metadata_filters(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The count uses the SAME predicate as the ranked legs."""
    # Arrange — 7 matching docs, 3 of them tagged ``alpha``.
    items = [
        (f"Quarterly note {i}", f"The quarterly review covered budget {i}.")
        for i in range(7)
    ]
    _seed(
        test_db,
        fake_embedder,
        items,
        tags={title: ["alpha"] for title, _ in items[:3]},
    )
    diag = SearchDiagnostics()

    # Act
    hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="quarterly",
        limit=5,
        fts_only=True,
        tag="alpha",
        diagnostics=diag,
        total_count=True,
    )

    # Assert
    assert diag.total_documents == 3


def test_total_is_none_when_not_requested(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """``total_count`` defaults off, so no caller pays for the extra query."""
    # Arrange
    _seed(test_db, fake_embedder, [("Quarterly note", "The quarterly review.")])
    diag = SearchDiagnostics()

    # Act
    hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="quarterly",
        limit=5,
        fts_only=True,
        diagnostics=diag,
    )

    # Assert
    assert diag.total_documents is None


def test_total_counts_documents_not_chunks(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A single document with several matching chunks counts once."""
    # Arrange
    body = "\n\n".join(
        f"Paragraph {i} about the quarterly review. " * 60 for i in range(6)
    )
    _seed(test_db, fake_embedder, [("Long quarterly doc", body)])
    chunks = test_db.execute("SELECT count(*) FROM chunks").fetchone()
    assert chunks is not None and chunks[0] > 1, "need multiple chunks"
    diag = SearchDiagnostics()

    # Act
    hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="quarterly",
        limit=5,
        fts_only=True,
        diagnostics=diag,
        total_count=True,
    )

    # Assert
    assert diag.total_documents == 1


def test_fts_count_semantics_unchanged(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """``fts_count`` keeps its capped, len(fts_rows) meaning alongside the total.

    ``brain gaps`` keys off ``fts_count == 0``; redefining it as an uncapped
    total would silently recalibrate the gap detector and invalidate every
    ``search_queries.fts_count`` row already stored.
    """
    # Arrange
    _seed(
        test_db,
        fake_embedder,
        [
            (f"Quarterly note {i}", f"The quarterly review covered budget {i}.")
            for i in range(7)
        ],
    )
    diag = SearchDiagnostics()
    off_corpus = SearchDiagnostics()

    # Act
    hybrid_search(
        test_db, embedder=fake_embedder, query="quarterly", limit=2,
        fts_only=True, diagnostics=diag, total_count=True,
    )
    hybrid_search(
        test_db, embedder=fake_embedder, query="nonexistentterm", limit=2,
        fts_only=True, diagnostics=off_corpus, total_count=True,
    )

    # Assert — fts_count is the candidate-chunk count, capped, NOT the total.
    assert diag.fts_count == 7
    assert diag.fts_count <= CANDIDATE_LIMIT
    # ...and the zero case stays exact on BOTH fields for an off-corpus query.
    assert off_corpus.fts_count == 0
    assert off_corpus.total_documents == 0


def test_timing_fields_populated(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The phase split is real, and the total bounds its parts."""
    # Arrange
    _seed(test_db, fake_embedder, [("Quarterly note", "The quarterly review.")])
    diag = SearchDiagnostics()

    # Act
    hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="quarterly",
        limit=5,
        diagnostics=diag,
        total_count=True,
    )

    # Assert
    assert isinstance(diag.embed_ms, float)
    assert isinstance(diag.sql_ms, float)
    assert isinstance(diag.total_ms, float)
    assert diag.total_ms >= diag.sql_ms


def test_embed_ms_none_under_fts_only(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """No embed ran, so no embed timing is reported (never a misleading 0)."""
    # Arrange
    _seed(test_db, fake_embedder, [("Quarterly note", "The quarterly review.")])
    diag = SearchDiagnostics()

    # Act
    hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="quarterly",
        limit=5,
        fts_only=True,
        diagnostics=diag,
    )

    # Assert
    assert diag.embed_ms is None
    assert diag.embed_cached is None
    assert diag.sql_ms is not None


def test_embed_cached_true_on_second_identical_query(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The in-process LRU turns the second identical embed into a hit."""
    # Arrange — a unique query string so a prior test cannot have warmed it.
    _seed(test_db, fake_embedder, [("Quarterly note", "The quarterly review.")])
    query = "quarterly cachecheck marker"
    first = SearchDiagnostics()
    second = SearchDiagnostics()

    # Act
    hybrid_search(
        test_db, embedder=fake_embedder, query=query, limit=5, diagnostics=first,
    )
    hybrid_search(
        test_db, embedder=fake_embedder, query=query, limit=5, diagnostics=second,
    )

    # Assert
    assert first.embed_cached is False
    assert second.embed_cached is True


def test_zero_results_still_report_timings(
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — an empty corpus is the point
    fake_embedder: Any,
) -> None:
    """The early-return path is timed too — 'nothing matched, and here's how long'."""
    # Arrange
    diag = SearchDiagnostics()

    # Act
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="nonexistentterm",
        limit=5,
        fts_only=True,
        diagnostics=diag,
        total_count=True,
    )

    # Assert
    assert results == []
    assert diag.total_documents == 0
    assert diag.sql_ms is not None
    assert diag.total_ms is not None


def test_count_failure_leaves_total_none_and_returns_results(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    mocker: Any,
) -> None:
    """A failing count degrades to ``None`` — it never breaks the search."""
    # Arrange
    _seed(test_db, fake_embedder, [("Quarterly note", "The quarterly review.")])
    mocker.patch(
        "brain.search._count_matching_documents",
        side_effect=psycopg.OperationalError("boom"),
    )
    diag = SearchDiagnostics()

    # Act
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="quarterly",
        limit=5,
        fts_only=True,
        diagnostics=diag,
        total_count=True,
    )

    # Assert — results survive; the total is unknown, NOT zero.
    assert results
    assert diag.total_documents is None
