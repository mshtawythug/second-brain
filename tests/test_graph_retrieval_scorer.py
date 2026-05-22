"""Pure unit tests for the graph-retrieval eval scorers (wave G2-j).

No DB, no Ollama — exact numbers for :func:`brain.eval.graph_retrieval.
score_local_docs` (reused ranking metrics over a ranked doc list) and
:func:`~brain.eval.graph_retrieval.score_themes` (the graph-appropriate
Jaccard-matched theme-set precision/recall/F1). Synthetic IDs / keys only.
"""
from __future__ import annotations

import math

import pytest

from brain.eval.errors import EvalMetricError
from brain.eval.graph_retrieval import (
    DEFAULT_THEME_JACCARD,
    score_local_docs,
    score_themes,
)


# --------------------------------------------------------------------------- #
# score_local_docs — reuses nDCG / MRR / Recall over a ranked doc list
# --------------------------------------------------------------------------- #
def test_local_perfect_ranking() -> None:
    score = score_local_docs(["d1", "d2", "d3"], ["d1", "d2", "d3"])
    assert score.recall_at_k == 1.0
    assert score.ndcg_at_k == 1.0
    assert score.mrr == 1.0
    assert score.ndcg_k == 5
    assert score.recall_k == 20


def test_local_partial_recall_and_mrr() -> None:
    score = score_local_docs(["x", "d1"], ["d1", "d2"])
    assert score.recall_at_k == 0.5  # 1 of 2 expected retrieved
    assert score.mrr == 0.5  # first relevant at rank 2


def test_local_empty_actual_scores_zero() -> None:
    score = score_local_docs([], ["d1"])
    assert score.recall_at_k == 0.0
    assert score.ndcg_at_k == 0.0
    assert score.mrr == 0.0


def test_local_empty_expected_raises() -> None:
    with pytest.raises(EvalMetricError):
        score_local_docs(["d1"], [])


def test_local_custom_k_passed_through() -> None:
    score = score_local_docs(["d1"], ["d1"], ndcg_k=3, recall_k=10)
    assert score.ndcg_k == 3
    assert score.recall_k == 10


# --------------------------------------------------------------------------- #
# score_themes — Jaccard-matched theme-set precision / recall / F1
# --------------------------------------------------------------------------- #
def test_themes_perfect_two_clusters() -> None:
    expected = [{"pricing", "billing"}, {"roadmap", "analytics"}]
    actual = [{"roadmap", "analytics"}, {"pricing", "billing"}]
    score = score_themes(actual, expected)
    assert score.matched == 2
    assert score.precision == 1.0
    assert score.recall == 1.0
    assert score.f1 == 1.0


def test_themes_below_threshold_no_match() -> None:
    # Jaccard({a,b},{a,c}) = 1/3 < 0.5 → unmatched.
    score = score_themes([{"a", "c"}], [{"a", "b"}])
    assert score.matched == 0
    assert score.precision == 0.0
    assert score.recall == 0.0
    assert score.f1 == 0.0


def test_themes_partial_recall() -> None:
    # One of two expected clusters surfaces.
    expected = [{"pricing", "billing"}, {"roadmap", "analytics"}]
    actual = [{"pricing", "billing"}]
    score = score_themes(actual, expected)
    assert score.matched == 1
    assert score.recall == 0.5
    assert score.precision == 1.0
    assert math.isclose(score.f1, 2 / 3)


def test_themes_precision_penalty_for_extra_cluster() -> None:
    expected = [{"pricing", "billing"}]
    actual = [{"pricing", "billing"}, {"unrelated", "noise"}]
    score = score_themes(actual, expected)
    assert score.matched == 1
    assert score.recall == 1.0
    assert score.precision == 0.5
    assert math.isclose(score.f1, 2 / 3)


def test_themes_both_empty_is_perfect() -> None:
    score = score_themes([], [])
    assert score.precision == 1.0
    assert score.recall == 1.0
    assert score.f1 == 1.0
    assert score.matched == 0


def test_themes_expected_empty_actual_nonempty_is_zero() -> None:
    score = score_themes([{"a", "b"}], [])
    assert score.precision == 0.0
    assert score.recall == 0.0
    assert score.f1 == 0.0


def test_themes_actual_empty_expected_nonempty_is_zero() -> None:
    score = score_themes([], [{"a", "b"}])
    assert score.precision == 0.0
    assert score.recall == 0.0


def test_themes_greedy_prefers_highest_jaccard() -> None:
    # Both candidate actuals clear the threshold against the single expected
    # cluster ({a,b} → 2/3, {a,b,c} → 1.0); greedy must pick the exact match.
    expected = [{"a", "b", "c"}]
    actual = [{"a", "b"}, {"a", "b", "c"}]
    score = score_themes(actual, expected)
    assert score.matched == 1
    assert score.recall == 1.0
    assert score.precision == 0.5  # one of two actual clusters matched


def test_themes_custom_threshold_admits_weaker_match() -> None:
    # Jaccard({a,b},{a,c}) = 1/3; default 0.5 rejects, threshold 0.3 admits.
    assert score_themes([{"a", "c"}], [{"a", "b"}]).matched == 0
    loosened = score_themes([{"a", "c"}], [{"a", "b"}], jaccard_threshold=0.3)
    assert loosened.matched == 1


def test_themes_normalizes_keys_and_drops_empty_clusters() -> None:
    # Keys lower-cased + whitespace-collapsed; an empty actual cluster is dropped
    # so it does not inflate the precision denominator.
    expected = [{"Pricing", "  Billing "}]
    actual = [set(), {"pricing", "billing"}]
    score = score_themes(actual, expected)
    assert score.n_actual == 1
    assert score.matched == 1
    assert score.precision == 1.0
    assert score.recall == 1.0


def test_default_jaccard_constant() -> None:
    assert DEFAULT_THEME_JACCARD == 0.5
