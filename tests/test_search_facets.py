"""Real-DB tests for the facet rollup over a search match set.

Facet counts are DOCUMENT counts, and they must describe exactly the same
match set as the ranked results — which is why every test here builds its
predicate through the shared ``build_predicate``.

All fixture data is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.facets import (
    DEFAULT_TOP_TAGS,
    SOURCE_NONE_BUCKET,
    compute_facets,
    count_matching_documents,
)
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import build_tsquery
from brain.search_predicate import build_predicate


def _ingest(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    title: str,
    body: str,
    source_kind: str = "manual",
    content_type: str = "note",
    tags: list[str] | None = None,
) -> None:
    """Ingest one synthetic document with an explicit source/type/tag shape."""
    ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=body,
            content_type=content_type,
            source_path=None,
            metadata={},
        ),
        source_kind=source_kind,
        source_external_id=f"{source_kind}:{title}",
        tags=tags or [],
    )


def _facets(conn: psycopg.Connection[Any], query: str, **kwargs: Any) -> Any:
    """Compute facets for ``query`` through the shared predicate builder."""
    top_tags = kwargs.pop("top_tags", DEFAULT_TOP_TAGS)
    return compute_facets(
        conn,
        predicate=build_predicate(**kwargs),
        tsquery=build_tsquery(conn, query),
        top_tags=top_tags,
    )


def _buckets(facet: Any) -> dict[str, int]:
    return {b.value: b.count for b in facet}


def test_facets_group_by_source_content_type_and_tag(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Exact bucket counts across three sources, types, and overlapping tags."""
    # Arrange
    _ingest(test_db, fake_embedder, title="Quarterly A", body="quarterly one",
            source_kind="manual", content_type="note", tags=["alpha", "beta"])
    _ingest(test_db, fake_embedder, title="Quarterly B", body="quarterly two",
            source_kind="gmail", content_type="email", tags=["alpha"])
    _ingest(test_db, fake_embedder, title="Quarterly C", body="quarterly three",
            source_kind="krisp", content_type="transcript", tags=["beta"])

    # Act
    facets = _facets(test_db, "quarterly")

    # Assert
    assert _buckets(facets.source) == {"manual": 1, "gmail": 1, "krisp": 1}
    assert _buckets(facets.content_type) == {"note": 1, "email": 1, "transcript": 1}
    assert _buckets(facets.tag) == {"alpha": 2, "beta": 2}
    assert facets.total_documents == 3


def test_facet_counts_are_documents_not_chunks(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """One document with many matching chunks counts exactly once."""
    # Arrange — a body long enough to chunk, mentioning the term throughout.
    body = "\n\n".join(
        f"Paragraph {i} about quarterly planning. " * 60 for i in range(6)
    )
    _ingest(test_db, fake_embedder, title="Long quarterly doc", body=body,
            tags=["alpha"])
    chunk_count = test_db.execute("SELECT count(*) FROM chunks").fetchone()
    assert chunk_count is not None and chunk_count[0] > 1, "need multiple chunks"

    # Act
    facets = _facets(test_db, "quarterly")

    # Assert
    assert facets.total_documents == 1
    assert _buckets(facets.source) == {"manual": 1}
    assert _buckets(facets.tag) == {"alpha": 1}


def test_facets_respect_filters(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A source filter narrows the facet buckets, not just the ranked rows."""
    # Arrange
    _ingest(test_db, fake_embedder, title="Quarterly A", body="quarterly one",
            source_kind="manual", tags=["alpha"])
    _ingest(test_db, fake_embedder, title="Quarterly B", body="quarterly two",
            source_kind="gmail", content_type="email", tags=["beta"])

    # Act
    facets = _facets(test_db, "quarterly", source_kind="gmail")

    # Assert
    assert _buckets(facets.source) == {"gmail": 1}
    assert _buckets(facets.content_type) == {"email": 1}
    assert _buckets(facets.tag) == {"beta": 1}
    assert facets.total_documents == 1


def test_tag_truncation_reports_remainder(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """12 distinct tags with top_tags=8 → 8 buckets and a remainder of 4."""
    # Arrange
    _ingest(test_db, fake_embedder, title="Tagged quarterly", body="quarterly tags",
            tags=[f"tag-{i:02d}" for i in range(12)])

    # Act
    facets = _facets(test_db, "quarterly", top_tags=8)

    # Assert
    assert len(facets.tag) == 8
    assert facets.tag_truncated == 4


def test_no_truncation_reports_zero_remainder(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Fewer tags than the cap leaves ``tag_truncated`` at zero."""
    # Arrange
    _ingest(test_db, fake_embedder, title="Quarterly few tags",
            body="quarterly tags", tags=["alpha", "beta"])

    # Act
    facets = _facets(test_db, "quarterly")

    # Assert
    assert len(facets.tag) == 2
    assert facets.tag_truncated == 0


def test_null_source_gets_its_own_bucket_not_manuals(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A doc with no ``sources`` row lands in ``none``, never in ``manual``.

    This test previously asserted the opposite, and the behaviour it pinned was
    a wrong answer that looked right: ``coalesce(s.kind, 'manual')`` filed every
    source-less document under a REAL source kind, so ``manual``'s count was
    inflated by all of them and filtering to ``manual`` returned documents with
    no source at all.

    Both halves are asserted, not just the absence of ``manual``: a query that
    dropped the row entirely would satisfy ``"manual" not in ...`` while making
    source-less documents invisible in the panel — the same information loss,
    quieter. The exact-dict comparison catches both.
    """
    # Arrange — two documents, one WITH a manual source and one with none, so
    # the test distinguishes "moved to its own bucket" from "manual renamed".
    _ingest(test_db, fake_embedder, title="Quarterly kept", body="quarterly kept",
            source_kind="manual")
    _ingest(test_db, fake_embedder, title="Quarterly orphan", body="quarterly orphan",
            source_kind="manual")
    test_db.execute(
        "UPDATE documents SET source_id = NULL WHERE title = %s", ("Quarterly orphan",)
    )

    # Act
    facets = _facets(test_db, "quarterly")

    # Assert
    assert _buckets(facets.source) == {"manual": 1, SOURCE_NONE_BUCKET: 1}
    # The split moved a count BETWEEN buckets; it did not lose one. ``manual``
    # was 2 before this change and is 1 now, and the total is unchanged.
    assert facets.total_documents == 2


def test_the_none_bucket_value_is_the_one_the_source_filter_accepts() -> None:
    """The bucket must be CLICKABLE, which makes this a contract, not a label.

    ``brain.facets`` is core and cannot import ``brain.ui.schemas`` (that would
    invert the dependency for one string), so the value is defined twice. If the
    two ever drift, the facet panel offers a ``source`` value the search
    endpoint rejects — a dead row in the panel that looks live. Pinning them
    equal here is what makes the duplication safe.
    """
    from brain.ui.schemas import SOURCE_FILTER_VALUES, SOURCE_NONE

    assert SOURCE_NONE_BUCKET == SOURCE_NONE
    assert SOURCE_NONE_BUCKET in SOURCE_FILTER_VALUES


def test_empty_match_set_yields_empty_facets(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """An off-corpus query produces empty tuples, not an exception."""
    # Arrange
    _ingest(test_db, fake_embedder, title="Quarterly A", body="quarterly one")

    # Act
    facets = _facets(test_db, "nonexistentterm")

    # Assert
    assert facets.source == ()
    assert facets.content_type == ()
    assert facets.tag == ()
    assert facets.tag_truncated == 0
    assert facets.total_documents == 0


def test_facet_total_agrees_with_the_standalone_count(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The footer's total and the facet panel's total must never disagree."""
    # Arrange
    for i in range(5):
        _ingest(test_db, fake_embedder, title=f"Quarterly {i}",
                body=f"quarterly body {i}", tags=["alpha"])
    predicate = build_predicate(tag="alpha")
    tsquery = build_tsquery(test_db, "quarterly")

    # Act
    facets = compute_facets(test_db, predicate=predicate, tsquery=tsquery)
    counted = count_matching_documents(
        test_db, predicate=predicate, tsquery=tsquery
    )

    # Assert
    assert facets.total_documents == counted == 5
