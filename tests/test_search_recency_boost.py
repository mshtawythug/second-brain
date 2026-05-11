"""Tests for the Q1-A recency boost in hybrid_search.

Wave Q1-A (2026-05-11) adds a multiplicative exponential-decay boost applied
after RRF: ``score *= 0.5 ** (age_days / halflife_days)`` where ``age_days``
comes from ``coalesce(sent_at, ingested_at)`` and is clamped to [0, +∞) so
future-dated rows receive ``boost = 1.0``.

The boost is opt-in: callers that omit ``recency_halflife_days`` (or pass
``None``) get the pre-Q1-A ranking unchanged.
"""
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.search import hybrid_search

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ingest(
    conn: psycopg.Connection,
    embedder: Any,
    *,
    title: str,
    content: str,
    sent_at: datetime | None = None,
) -> str:
    """Ingest a doc and return its document_id.

    If ``sent_at`` is provided, it is written directly to the row after ingest
    so tests can control the recency timestamp without needing real Gmail dates.
    """
    doc = ExtractedDoc(
        title=title,
        content=f"{title}: {content}",
        content_type="note",
        source_path=None,
        metadata={},
    )
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=doc,
        source_kind="manual",
        source_external_id=f"recency-test:{title}",
    )
    assert result.document_id is not None
    if sent_at is not None:
        conn.execute(
            "UPDATE documents SET sent_at = %s WHERE id = %s",
            (sent_at, result.document_id),
        )
    return result.document_id


# ---------------------------------------------------------------------------
# Test 1: boost is disabled by default (None)
# ---------------------------------------------------------------------------


def test_recency_boost_disabled_by_default(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """Omitting ``recency_halflife_days`` (default None) skips the boost.

    Ingests a very old doc (500 days) and compares its score with boost
    disabled vs enabled.  Without boost the score is the raw RRF value;
    with a 180-day half-life the score is multiplied by
    ``0.5 ** (500 / 180) ≈ 0.148``, so the unboosted score should be
    approximately 6.8× higher.  We use a conservative lower bound of 3×
    to guard against minor FTS/vector-rank noise.
    """
    now = datetime.now(tz=UTC)
    _ingest(
        test_db, fake_embedder,
        title="AncientDoc", content="boost disabled verification",
        sent_at=now - timedelta(days=500),
    )

    # Without boost — score is the raw RRF value (no decay applied).
    results_no_boost = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="boost disabled",
        limit=5,
        recency_halflife_days=None,
    )
    assert results_no_boost, "expected AncientDoc in results (no boost)"
    score_no_boost = results_no_boost[0].score

    # With aggressive boost — 500-day doc at 180-day halflife → factor ≈ 0.148.
    results_with_boost = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="boost disabled",
        limit=5,
        recency_halflife_days=180.0,
    )
    assert results_with_boost, "expected AncientDoc in results (with boost)"
    score_with_boost = results_with_boost[0].score

    # Without boost the raw score must be significantly higher than the decayed
    # score — ratio ≥ 3.0 is a conservative threshold (expected ≈ 6.8×).
    assert score_no_boost > score_with_boost * 3.0, (
        f"without boost, AncientDoc score {score_no_boost:.6f} should be "
        f"much higher than boosted score {score_with_boost:.6f} "
        f"(ratio = {score_no_boost / score_with_boost:.2f}×, expected ≥ 3.0×)"
    )


# ---------------------------------------------------------------------------
# Test 2: newer doc outranks older at equal relevance with boost enabled
# ---------------------------------------------------------------------------


def test_recency_boost_prefers_newer_doc_at_equal_relevance(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """With recency boost, the newer doc outranks an older one at equal relevance.

    Both docs have identical FTS/vector signal. The newer doc's boost is ~1.0
    (sent today); the older doc's boost is 0.5 ** (365 / 180) ≈ 0.247. The
    newer doc should appear first in the ranked results.
    """
    now = datetime.now(tz=UTC)
    _ingest(
        test_db, fake_embedder,
        title="OldDoc", content="common keyword",
        sent_at=now - timedelta(days=365),
    )
    _ingest(test_db, fake_embedder, title="NewDoc", content="common keyword", sent_at=now)

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="common keyword",
        limit=5,
        recency_halflife_days=180.0,
    )
    assert results, "expected at least one result"
    titles = [r.title for r in results]
    assert "OldDoc" in titles
    assert "NewDoc" in titles
    scores = {r.title: r.score for r in results}
    # NewDoc should score higher than OldDoc.
    assert scores["NewDoc"] > scores["OldDoc"], (
        f"expected NewDoc score {scores['NewDoc']:.6f} > "
        f"OldDoc score {scores['OldDoc']:.6f}"
    )
    # Rough magnitude check: ratio ≈ 0.5^(365/180) ≈ 0.247
    ratio = scores["OldDoc"] / scores["NewDoc"]
    assert 0.20 < ratio < 0.35, (
        f"expected score ratio ≈ 0.247 (365-day doc vs today), got {ratio:.4f}"
    )


# ---------------------------------------------------------------------------
# Test 3: sent_at takes precedence over ingested_at
# ---------------------------------------------------------------------------


def test_recency_boost_uses_sent_at_when_present(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """A doc with recent ``sent_at`` but ancient ``ingested_at`` uses ``sent_at``.

    Conversely, a doc with ``sent_at IS NULL`` falls back to ``ingested_at``.
    Both docs have equal FTS/vector signal; the one with the recent ``sent_at``
    should outscore the one that relies on a stale ``ingested_at``.
    """
    now = datetime.now(tz=UTC)

    # Doc A: recent sent_at, but we'll manually set ingested_at to be ancient
    # via a direct UPDATE (normally ingested_at = NOW() at insert time).
    doc_a_id = _ingest(test_db, fake_embedder, title="SentRecentA", content="boost compare")
    test_db.execute(
        "UPDATE documents SET sent_at = %s, ingested_at = %s WHERE id = %s",
        (now - timedelta(days=10), now - timedelta(days=500), doc_a_id),
    )

    # Doc B: no sent_at (NULL), ingested_at is ancient.
    doc_b_id = _ingest(test_db, fake_embedder, title="IngestedOldB", content="boost compare")
    test_db.execute(
        "UPDATE documents SET sent_at = NULL, ingested_at = %s WHERE id = %s",
        (now - timedelta(days=500), doc_b_id),
    )

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="boost compare",
        limit=5,
        recency_halflife_days=180.0,
    )
    scores = {r.title: r.score for r in results}
    assert "SentRecentA" in scores and "IngestedOldB" in scores
    # Doc A (sent_at=10 days ago) should outscore Doc B (ingested_at=500 days ago).
    assert scores["SentRecentA"] > scores["IngestedOldB"]


# ---------------------------------------------------------------------------
# Test 4: future-dated rows are clamped to boost = 1.0
# ---------------------------------------------------------------------------


def test_recency_boost_clamps_future_sent_at(
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """A doc with ``sent_at`` in the future gets boost = 1.0, not > 1.0.

    Prevents a misconfigured Krisp transcript (e.g. ``sent_at = NOW() + 30d``)
    from receiving an amplified score that rockets it to the top of every query.
    """
    now = datetime.now(tz=UTC)
    future_doc_id = _ingest(test_db, fake_embedder, title="FutureDoc", content="time warp test")
    today_doc_id = _ingest(test_db, fake_embedder, title="TodayDoc", content="time warp test")

    # Set future_doc's sent_at 30 days ahead.
    test_db.execute(
        "UPDATE documents SET sent_at = %s WHERE id = %s",
        (now + timedelta(days=30), future_doc_id),
    )
    # Today doc uses today's ingested_at (unchanged).
    test_db.execute(
        "UPDATE documents SET ingested_at = %s WHERE id = %s",
        (now, today_doc_id),
    )

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="time warp",
        limit=5,
        recency_halflife_days=180.0,
    )
    scores = {r.title: r.score for r in results}
    assert "FutureDoc" in scores and "TodayDoc" in scores
    # Both docs are recent (today or future). The future doc's boost is 1.0
    # (clamped); the today doc's boost is also ~1.0. The future doc must not
    # score MORE than the today doc (boost must not exceed 1.0).
    assert scores["FutureDoc"] <= scores["TodayDoc"] * 1.001, (
        f"FutureDoc score {scores['FutureDoc']:.6f} exceeds "
        f"TodayDoc score {scores['TodayDoc']:.6f} — boost > 1.0 is not allowed"
    )
