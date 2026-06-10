"""Tests for the SearchDiagnostics out-parameter on hybrid_search.

Regression coverage for the `brain gaps` design bug: the vector leg always
returns nearest neighbours, so ``len(results)`` can be > 0 even when the query
has *no lexical trace* in the corpus. ``SearchDiagnostics.fts_count`` exposes
the FTS-leg hit count so a lexical miss (``fts_count == 0``) is detectable even
when the vector leg returned filler results.
"""
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import SearchDiagnostics, hybrid_search


def _seed(test_db, embedder, items):
    for title, content in items:
        ingest_document(
            test_db,
            embedder=embedder,
            doc=ExtractedDoc(
                title=title,
                content=f"{title}: {content}",
                content_type="txt",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:{title}",
            tags=[],
        )


def test_diagnostics_fts_count_nonzero_on_lexical_match(test_db, fake_embedder):
    # Arrange
    _seed(test_db, fake_embedder, [("Doc A", "company-id was a great place")])
    diag = SearchDiagnostics()

    # Act
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, diagnostics=diag
    )

    # Assert
    assert results
    assert diag.fts_count is not None and diag.fts_count > 0


def test_diagnostics_fts_count_zero_when_vector_returns_filler(
    test_db, fake_embedder
):
    """THE BUG: off-corpus query -> vector filler results but FTS leg matched 0.

    In production the vector leg always returns nearest neighbours, so an
    off-corpus query reports ``len(results) > 0`` even though the lexical leg
    matched nothing. We force that masking deterministically with
    ``vector_sim_floor=-1.0`` (accept every cosine, since the fake embedder's
    similarity can otherwise be negative and get floored out). The assertion
    that matters is ``fts_count == 0`` despite non-empty results -- that zero
    is the real knowledge-gap signal.
    """
    # Arrange -- corpus has no lexical trace of "wombat".
    _seed(test_db, fake_embedder, [("Doc A", "company-id was a great place")])
    diag = SearchDiagnostics()

    # Act
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="wombat quokka numbat",
        limit=5,
        vector_sim_floor=-1.0,
        diagnostics=diag,
    )

    # Assert -- vector leg returned filler, but FTS leg is empty.
    assert results, "vector leg should return filler nearest-neighbours"
    assert diag.fts_count == 0


def test_diagnostics_default_none_leaves_contract_unchanged(test_db, fake_embedder):
    """Callers that don't pass diagnostics get the unchanged list contract."""
    _seed(test_db, fake_embedder, [("Doc A", "company-id was a great place")])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5
    )
    assert isinstance(results, list)


def test_diagnostics_fts_count_set_under_fts_only(test_db, fake_embedder):
    """fts_only path still records the lexical hit count."""
    _seed(test_db, fake_embedder, [("Doc A", "company-id was a great place")])
    diag = SearchDiagnostics()
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="company-id",
        limit=5,
        fts_only=True,
        diagnostics=diag,
    )
    assert results
    assert diag.fts_count is not None and diag.fts_count > 0
