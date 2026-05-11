"""Unit tests for brain.eval.metrics — pure logic, no DB, no embedder."""

import math

import pytest

from brain.eval.errors import EvalMetricError
from brain.eval.metrics import dcg_at_k, mrr, ndcg_at_k, recall_at_k

# ---------------------------------------------------------------------------
# nDCG@5
# ---------------------------------------------------------------------------


def test_ndcg_at_5_perfect_ranking():
    """All expected docs at the top → nDCG == 1.0."""
    expected = ["a", "b", "c"]
    actual = ["a", "b", "c", "d", "e"]
    assert ndcg_at_k(actual, expected, k=5) == pytest.approx(1.0)


def test_ndcg_at_5_reversed_ranking():
    """Relevant doc at rank 5 with 2 expected → dcg/idcg matches closed-form."""
    # actual = [x1, x2, x3, x4, a]; expected = {a, b}
    actual = ["x1", "x2", "x3", "x4", "a"]
    expected = ["a", "b"]
    query_dcg = 1.0 / math.log2(5 + 1)  # hit at rank 5
    idcg = 1.0 / math.log2(2) + 1.0 / math.log2(3)  # 2 hits in positions 1, 2
    expected_ndcg = query_dcg / idcg
    assert ndcg_at_k(actual, expected, k=5) == pytest.approx(expected_ndcg, rel=1e-6)


def test_ndcg_at_5_no_hits():
    """Zero overlap between actual and expected → 0.0."""
    actual = ["x", "y", "z"]
    expected = ["a", "b"]
    assert ndcg_at_k(actual, expected, k=5) == pytest.approx(0.0)


def test_ndcg_at_5_partial_hit_top_position():
    """Single relevant doc at rank 1, 3 total expected → closed-form nDCG."""
    # actual = ["a", "d", "e", "f", "g"]; expected = {a, x, y}
    actual = ["a", "d", "e", "f", "g"]
    expected = ["a", "x", "y"]
    query_dcg = 1.0 / math.log2(2)  # hit at rank 1
    idcg = (
        1.0 / math.log2(2) + 1.0 / math.log2(3) + 1.0 / math.log2(4)
    )  # 3 ideal hits
    expected_ndcg = query_dcg / idcg
    assert ndcg_at_k(actual, expected, k=5) == pytest.approx(expected_ndcg, rel=1e-6)


def test_ndcg_at_5_k_greater_than_actual():
    """k > len(actual) — should not raise; evaluate over available results."""
    actual = ["a"]
    expected = ["a"]
    # Only 1 result; perfect hit at rank 1 → nDCG = 1.0
    assert ndcg_at_k(actual, expected, k=5) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# DCG@k
# ---------------------------------------------------------------------------


def test_dcg_at_k_basic():
    """Single hit at rank 1 → 1/log2(2) = 1.0."""
    assert dcg_at_k(["a"], ["a"], k=5) == pytest.approx(1.0)


def test_dcg_at_k_no_hits():
    """No overlap → 0.0."""
    assert dcg_at_k(["x"], ["a"], k=5) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# MRR
# ---------------------------------------------------------------------------


def test_mrr_first_relevant_at_rank_3():
    """First hit at rank 3 → mrr == 1/3."""
    actual = ["x", "y", "a", "z"]
    expected = ["a"]
    assert mrr(actual, expected) == pytest.approx(1.0 / 3)


def test_mrr_no_relevant():
    """No relevant doc in actual → 0.0."""
    actual = ["x", "y", "z"]
    expected = ["a"]
    assert mrr(actual, expected) == pytest.approx(0.0)


def test_mrr_first_relevant_at_rank_1():
    """Hit at rank 1 → mrr == 1.0."""
    assert mrr(["a", "b"], ["a", "b"]) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Recall@20
# ---------------------------------------------------------------------------


def test_recall_at_20_full():
    """All expected docs in actual → 1.0."""
    actual = [f"doc{i}" for i in range(20)]
    expected = ["doc0", "doc5", "doc10"]
    assert recall_at_k(actual, expected, k=20) == pytest.approx(1.0)


def test_recall_at_20_partial():
    """2 of 4 expected in actual[:20] → 0.5."""
    actual = ["a", "b", "c", "d"] + ["other"] * 16
    expected = ["a", "b", "missing1", "missing2"]
    assert recall_at_k(actual, expected, k=20) == pytest.approx(0.5)


def test_recall_at_20_truncates_to_k():
    """A relevant doc at rank 21 must not be counted when k=20."""
    # Use 20 distinct strings so dedup does not collapse them before the cutoff.
    actual = [f"doc{i}" for i in range(20)] + ["a"]
    expected = ["a"]
    assert recall_at_k(actual, expected, k=20) == pytest.approx(0.0)


def test_recall_at_k_custom_k():
    """recall_at_k with k=3 counts only the first 3 results."""
    actual = ["x", "y", "a", "b"]
    expected = ["a", "b"]
    # a is at rank 3 (counted), b at rank 4 (not counted for k=3)
    assert recall_at_k(actual, expected, k=3) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Edge cases — empty expected → EvalMetricError
# ---------------------------------------------------------------------------


def test_metric_empty_expected_raises_ndcg():
    with pytest.raises(EvalMetricError):
        ndcg_at_k(["a"], [], k=5)


def test_metric_empty_expected_raises_mrr():
    with pytest.raises(EvalMetricError):
        mrr(["a"], [])


def test_metric_empty_expected_raises_recall():
    with pytest.raises(EvalMetricError):
        recall_at_k(["a"], [], k=20)


def test_metric_empty_expected_raises_dcg():
    with pytest.raises(EvalMetricError):
        dcg_at_k(["a"], [], k=5)


# ---------------------------------------------------------------------------
# Edge cases — empty actual → 0.0
# ---------------------------------------------------------------------------


def test_metric_empty_actual_returns_zero_ndcg():
    assert ndcg_at_k([], ["a"], k=5) == pytest.approx(0.0)


def test_metric_empty_actual_returns_zero_mrr():
    assert mrr([], ["a"]) == pytest.approx(0.0)


def test_metric_empty_actual_returns_zero_recall():
    assert recall_at_k([], ["a"], k=20) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Edge cases — duplicates in actual are counted once
# ---------------------------------------------------------------------------


def test_metric_duplicate_actual_counted_once_recall():
    """actual = ["a", "a", "b"], expected = {a, b} → recall_at_20 == 1.0."""
    actual = ["a", "a", "b"]
    expected = ["a", "b"]
    assert recall_at_k(actual, expected, k=20) == pytest.approx(1.0)


def test_metric_duplicate_actual_counted_once_mrr():
    """Duplicate at rank 1 should not be counted again; MRR = 1.0."""
    actual = ["a", "a", "a"]
    expected = ["a"]
    assert mrr(actual, expected) == pytest.approx(1.0)


def test_metric_duplicate_actual_counted_once_ndcg():
    """Duplicates deduped before scoring; deduped=['a','b','c']."""
    actual = ["a", "a", "b", "c"]
    expected = ["a", "b", "c"]
    # Deduped actual = ["a", "b", "c"] → perfect hit for k=3 → nDCG = 1.0
    assert ndcg_at_k(actual, expected, k=3) == pytest.approx(1.0)
