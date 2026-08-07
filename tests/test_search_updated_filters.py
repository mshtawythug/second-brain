"""F9: ``updated_after`` / ``updated_before`` on hybrid search.

The existing ``after`` / ``before`` pair binds ``coalesce(sent_at,
ingested_at)`` — the *document's* date. For an email or a transcript
``coalesce`` prefers ``sent_at``, so "the notes I touched last week" was
unanswerable at any layer. These filters bind ``documents.updated_at``
instead, and the load-bearing test here is
:func:`test_updated_filters_see_an_edit_that_after_before_cannot`, which pins
that the two pairs are genuinely different axes.

The generic predicate properties (one clause + one param per filter, no
caller value in SQL text, frozen dataclass) come from
``tests/test_search_predicate.py``'s ``ALL_FILTERS`` table, which both new
filters are registered in. What lives here is what is specific to F9: the
inclusive/exclusive boundary semantics and the real-DB behaviour.

All documents are synthetic.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.search import SearchResult, hybrid_search
from brain.search_predicate import build_predicate

_SENT_LONG_AGO = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
_EDITED_RECENTLY = datetime(2026, 7, 20, 9, 0, 0, tzinfo=UTC)
_EDITED_LONG_AGO = datetime(2024, 2, 1, 9, 0, 0, tzinfo=UTC)
_CUTOFF = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)


def _seed(
    conn: psycopg.Connection[Any],
    embedder: object,
    *,
    title: str,
    updated_at: datetime,
    sent_at: datetime | None = None,
    tags: list[str] | None = None,
) -> str:
    """Ingest one synthetic doc and stamp its timestamps directly.

    The ingest pipeline takes ``updated_at`` from the column default (= now),
    so a test that needs a specific edit time writes it afterwards — the same
    shape ``test_search_metadata_filters._seed`` uses for ``sent_at``.
    """
    result = ingest_document(
        conn,
        embedder=embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title,
            content=f"{title}: shared probe term",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=f"manual:{title}",
        tags=tags or [],
    )
    assert result.document_id is not None
    conn.execute(
        "UPDATE documents SET updated_at = %s, sent_at = %s WHERE id = %s",
        (updated_at, sent_at, result.document_id),
    )
    return result.document_id


def _titles(results: list[SearchResult]) -> set[str]:
    return {r.title for r in results}


# ---------------------------------------------------------------------------
# Boundary semantics — pure predicate assembly, no DB
# ---------------------------------------------------------------------------


def test_build_predicate_binds_updated_after_inclusively() -> None:
    """RED-FIRST: ``updated_after`` reaches the WHERE clause as ``>=``."""
    # Arrange / Act
    predicate = build_predicate(updated_after=_CUTOFF)

    # Assert
    assert "d.updated_at >= %s" in predicate.where_sql
    assert predicate.where_params == (_CUTOFF,)


def test_build_predicate_binds_updated_before_exclusively() -> None:
    """``<``, not ``<=`` — so ``updated_after=X, updated_before=X`` is empty."""
    # Arrange / Act
    predicate = build_predicate(updated_before=_CUTOFF)

    # Assert
    assert "d.updated_at < %s" in predicate.where_sql
    assert predicate.where_params == (_CUTOFF,)


def test_build_predicate_stamps_naive_updated_bounds_as_utc() -> None:
    """A naive ``--updated-after 2026-07-01`` must not shift with session TZ.

    Same hazard ``_ensure_utc`` exists for on ``after`` / ``before``: bound
    naive against a ``timestamptz``, PostgreSQL reads the literal in the
    session's ``TimeZone`` and moves the boundary by its UTC offset.
    """
    # Arrange
    naive = datetime(2026, 7, 1, 0, 0, 0)

    # Act
    predicate = build_predicate(updated_after=naive, updated_before=naive)

    # Assert
    assert predicate.where_params == (_CUTOFF, _CUTOFF)
    assert all(p.tzinfo is UTC for p in predicate.where_params)


# ---------------------------------------------------------------------------
# hybrid_search — real DB
# ---------------------------------------------------------------------------


def test_updated_after_excludes_untouched_docs(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """RED-FIRST: fails with ``TypeError: unexpected keyword argument``."""
    # Arrange
    _seed(test_db, fake_embedder, title="Edited recently", updated_at=_EDITED_RECENTLY)
    _seed(test_db, fake_embedder, title="Never touched", updated_at=_EDITED_LONG_AGO)

    # Act
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="probe",
        limit=10,
        updated_after=_CUTOFF,
    )

    # Assert
    assert _titles(results) == {"Edited recently"}


def test_updated_after_is_inclusive_on_the_boundary(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Arrange
    _seed(test_db, fake_embedder, title="Exactly at cutoff", updated_at=_CUTOFF)

    # Act
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="probe",
        limit=10,
        updated_after=_CUTOFF,
    )

    # Assert
    assert _titles(results) == {"Exactly at cutoff"}


def test_updated_before_is_exclusive_on_the_boundary(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Mirrors ``before``: the upper bound never includes its own instant."""
    # Arrange
    _seed(test_db, fake_embedder, title="Exactly at cutoff", updated_at=_CUTOFF)

    # Act
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="probe",
        limit=10,
        updated_before=_CUTOFF,
    )

    # Assert
    assert results == []


def test_updated_filters_see_an_edit_that_after_before_cannot(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The reason F9 exists, asserted directly.

    Both docs were sent long ago, so ``--after <cutoff>`` finds neither —
    ``coalesce`` prefers ``sent_at``. Only ``--updated-after`` tells them apart.
    """
    # Arrange
    _seed(
        test_db, fake_embedder,
        title="Old email touched recently",
        sent_at=_SENT_LONG_AGO,
        updated_at=_EDITED_RECENTLY,
    )
    _seed(
        test_db, fake_embedder,
        title="Old email untouched",
        sent_at=_SENT_LONG_AGO,
        updated_at=_EDITED_LONG_AGO,
    )

    # Act
    by_doc_date = hybrid_search(
        test_db, embedder=fake_embedder, query="probe", limit=10, after=_CUTOFF
    )
    by_edit_date = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="probe",
        limit=10,
        updated_after=_CUTOFF,
    )

    # Assert
    assert by_doc_date == [], "sent_at wins in coalesce, so --after sees nothing"
    assert _titles(by_edit_date) == {"Old email touched recently"}


def test_updated_range_composes_with_other_filters(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The updated range ANDs with the existing filters rather than replacing them."""
    # Arrange
    _seed(
        test_db, fake_embedder,
        title="Recent and tagged", updated_at=_EDITED_RECENTLY, tags=["keep"],
    )
    _seed(
        test_db, fake_embedder,
        title="Recent but untagged", updated_at=_EDITED_RECENTLY,
    )
    _seed(
        test_db, fake_embedder,
        title="Tagged but stale", updated_at=_EDITED_LONG_AGO, tags=["keep"],
    )

    # Act
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="probe",
        limit=10,
        tag="keep",
        updated_after=_CUTOFF,
    )

    # Assert
    assert _titles(results) == {"Recent and tagged"}


def test_explain_reports_the_updated_bounds(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """``matched_filters`` carries both bounds as ISO strings, like after/before."""
    # Arrange
    _seed(test_db, fake_embedder, title="Edited recently", updated_at=_EDITED_RECENTLY)

    # Act
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="probe",
        limit=10,
        explain=True,
        updated_after=_CUTOFF,
    )

    # Assert
    assert results
    assert results[0].explain is not None
    matched = results[0].explain.matched_filters
    assert matched["updated_after"] == _CUTOFF.isoformat()
    assert matched["updated_before"] is None
