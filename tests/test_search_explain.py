"""Tests for hybrid_search(..., explain=True) — SearchExplanation payload."""

import pytest

from brain.ingest import ExtractedDoc, ingest_document
from brain.search import SearchExplanation, hybrid_search


def _seed(test_db, embedder, items):
    """Seed docs as (title, content, tags, source_kind)."""
    for title, content, tags, source_kind in items:
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
            source_kind=source_kind,
            source_external_id=f"{source_kind}:{title}",
            tags=tags or [],
        )


# ---------------------------------------------------------------------------
# explain=False (default)
# ---------------------------------------------------------------------------


def test_explain_disabled_by_default(test_db, fake_embedder):
    """hybrid_search without explain flag returns explain=None on every result."""
    _seed(test_db, fake_embedder, [("Doc A", "company-id workspace note", [], "manual")])
    results = hybrid_search(test_db, embedder=fake_embedder, query="company-id", limit=5)
    assert results
    for r in results:
        assert r.explain is None


def test_explain_false_explicit(test_db, fake_embedder):
    """Explicit explain=False produces None explain field."""
    _seed(test_db, fake_embedder, [("Doc B", "company-id meeting notes", [], "manual")])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, explain=False
    )
    assert results
    assert all(r.explain is None for r in results)


# ---------------------------------------------------------------------------
# explain=True — payload shape
# ---------------------------------------------------------------------------


def test_explain_attaches_payload_to_each_result(test_db, fake_embedder):
    """explain=True attaches a SearchExplanation to every result."""
    _seed(test_db, fake_embedder, [
        ("Alpha", "project alpha kickoff notes", [], "manual"),
        ("Beta", "project beta roadmap planning", [], "manual"),
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="project", limit=5, explain=True
    )
    assert results
    for r in results:
        assert isinstance(r.explain, SearchExplanation), (
            f"result {r.document_id!r} has explain={r.explain!r}"
        )


def test_explain_fts_only_path_populates_fts_fields_only(test_db, fake_embedder):
    """With fts_only=True the vector leg is skipped; explain has no vector fields."""
    _seed(test_db, fake_embedder, [("Doc FTS", "crisp unique fts term here", [], "manual")])
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="crisp",
        limit=5,
        fts_only=True,
        explain=True,
    )
    assert results
    ex = results[0].explain
    assert ex is not None
    assert ex.vector_rank is None
    assert ex.vector_cosine is None
    assert ex.vector_rrf_contribution == pytest.approx(0.0)
    # FTS must have a rank (chunk appeared in the FTS leg).
    assert ex.fts_rank is not None
    assert ex.fts_rank >= 1


def test_explain_rrf_score_equals_sum_of_contributions(test_db, fake_embedder):
    """rrf_score ≈ fts_rrf_contribution + vector_rrf_contribution for best chunk."""
    _seed(test_db, fake_embedder, [("Doc Sum", "rrf contribution test data", [], "manual")])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="rrf contribution", limit=5, explain=True
    )
    assert results
    ex = results[0].explain
    assert ex is not None
    total = ex.fts_rrf_contribution + ex.vector_rrf_contribution
    assert ex.rrf_score == pytest.approx(total, abs=1e-8)


def test_explain_recency_boost_matches_rrf_when_disabled(test_db, fake_embedder):
    """With recency_halflife_days=None, boost==1.0 and final_score==rrf_score."""
    _seed(test_db, fake_embedder, [("Doc Recency", "recency disabled baseline", [], "manual")])
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="recency",
        limit=5,
        explain=True,
        recency_halflife_days=None,
    )
    assert results
    ex = results[0].explain
    assert ex is not None
    assert ex.recency_boost == pytest.approx(1.0)
    assert ex.final_score == pytest.approx(ex.rrf_score)


def test_explain_recency_age_none_when_disabled(test_db, fake_embedder):
    """recency_age_days is None when recency_halflife_days is None."""
    _seed(test_db, fake_embedder, [("Doc NoAge", "no age tracked here", [], "manual")])
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="no age",
        limit=5,
        explain=True,
        recency_halflife_days=None,
    )
    assert results
    ex = results[0].explain
    assert ex is not None
    assert ex.recency_age_days is None


def test_explain_recency_boost_lt_one_for_old_docs(test_db, fake_embedder):
    """A 365-day-old doc with halflife=180 gets boost ≈ 0.5^(365/180) ≈ 0.249."""
    from datetime import UTC, datetime, timedelta


    # Ingest once normally to get the document id.
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Old Report",
            content="Old Report: annual review document from last year",
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )

    # Back-date the ingested_at to 365 days ago.
    old_ts = datetime.now(tz=UTC) - timedelta(days=365)
    test_db.execute(
        "UPDATE documents SET ingested_at = %s WHERE title = %s",
        (old_ts, "Old Report"),
    )

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="annual review",
        limit=5,
        explain=True,
        recency_halflife_days=180.0,
    )
    assert results
    ex = results[0].explain
    assert ex is not None
    assert ex.recency_age_days is not None
    assert ex.recency_age_days == pytest.approx(365.0, abs=1.0)
    expected_boost = 0.5 ** (365.0 / 180.0)
    assert ex.recency_boost == pytest.approx(expected_boost, rel=0.01)


def test_explain_matched_filters_round_trip(test_db, fake_embedder):
    """matched_filters reflects the source_kind, tag, since_days, fts_only flags."""
    _seed(test_db, fake_embedder, [
        ("Krisp Doc", "meeting transcript agenda", ["standup"], "krisp"),
    ])
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="meeting",
        limit=5,
        explain=True,
        source_kind="krisp",
        tag="standup",
        since_days=7,
        fts_only=False,
    )
    assert results
    ex = results[0].explain
    assert ex is not None
    assert ex.matched_filters["source_kind"] == "krisp"
    assert ex.matched_filters["tag"] == "standup"
    assert ex.matched_filters["since_days"] == 7
    assert ex.matched_filters["fts_only"] is False


def test_explain_reranker_score_is_none_today(test_db, fake_embedder):
    """reranker_score is None in Q1-B (placeholder for Q3-A)."""
    _seed(test_db, fake_embedder, [("Doc Q3A", "placeholder for reranker", [], "manual")])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="placeholder", limit=5, explain=True
    )
    assert results
    ex = results[0].explain
    assert ex is not None
    assert ex.reranker_score is None


def test_explain_best_chunk_id_is_non_empty_string(test_db, fake_embedder):
    """best_chunk_id is a non-empty UUID string."""
    _seed(test_db, fake_embedder, [("Doc Chunk", "multi chunk document body here", [], "manual")])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="chunk document", limit=5, explain=True
    )
    assert results
    ex = results[0].explain
    assert ex is not None
    assert isinstance(ex.best_chunk_id, str)
    assert len(ex.best_chunk_id) > 0


def test_explain_best_chunk_index_is_non_negative(test_db, fake_embedder):
    """best_chunk_index is a non-negative integer."""
    _seed(test_db, fake_embedder, [("Doc Idx", "chunk index tracking test", [], "manual")])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="chunk index", limit=5, explain=True
    )
    assert results
    ex = results[0].explain
    assert ex is not None
    assert isinstance(ex.best_chunk_index, int)
    assert ex.best_chunk_index >= 0


def test_explain_final_score_matches_result_score(test_db, fake_embedder):
    """explain.final_score must equal SearchResult.score."""
    _seed(test_db, fake_embedder, [("Doc Score", "score consistency check data", [], "manual")])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="score consistency", limit=5, explain=True
    )
    assert results
    for r in results:
        assert r.explain is not None
        assert r.explain.final_score == pytest.approx(r.score)


def test_explain_multiple_results_all_have_payloads(test_db, fake_embedder):
    """All returned results have explain payloads when explain=True."""
    _seed(test_db, fake_embedder, [
        (f"Multi{i}", f"multi result term content {i}", [], "manual")
        for i in range(5)
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="multi result term", limit=5, explain=True
    )
    assert len(results) >= 2
    for r in results:
        assert r.explain is not None
