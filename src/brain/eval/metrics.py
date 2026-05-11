"""Pure metric functions for ranking evaluation: nDCG@k, MRR, recall@k.

No I/O. No DB. Fully unit-testable with synthetic data.
All functions accept ``actual`` (ranked list of doc IDs, 1-indexed by position)
and ``expected`` (collection of relevant doc IDs). Duplicates in ``actual`` are
deduplicated, keeping the first occurrence.
"""

import math
from collections.abc import Iterable, Sequence

from .errors import EvalMetricError


def _dedup_preserving_order(seq: Sequence[str]) -> list[str]:
    """Return seq with duplicates removed, preserving first-occurrence order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def dcg_at_k(actual: Sequence[str], expected: Iterable[str], k: int = 5) -> float:
    """Discounted Cumulative Gain at k (binary relevance).

    ``dcg = sum(1 / log2(rank + 1) for rank, doc in enumerate(actual[:k], 1)
               if doc in expected_set)``

    Raises:
        EvalMetricError: When ``expected`` is empty.
    """
    expected_set = set(expected)
    if not expected_set:
        raise EvalMetricError(
            "expected must not be empty; use corpus validation to prevent this"
        )
    dcg = 0.0
    for rank, doc in enumerate(actual[:k], start=1):
        if doc in expected_set:
            dcg += 1.0 / math.log2(rank + 1)
    return dcg


def ndcg_at_k(actual: Sequence[str], expected: Iterable[str], k: int = 5) -> float:
    """Normalized DCG at k (binary relevance).

    Returns 0.0 when ``actual`` is empty. IDCG is computed from
    ``min(k, len(expected))`` ideal hits in perfect rank order.

    Raises:
        EvalMetricError: When ``expected`` is empty.
    """
    expected_set = set(expected)
    if not expected_set:
        raise EvalMetricError("expected must not be empty")
    if not actual:
        return 0.0
    actual_deduped = _dedup_preserving_order(actual)
    # Ideal DCG: top-n_relevant docs retrieved in positions 1..n_relevant.
    n_relevant = min(k, len(expected_set))
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, n_relevant + 1))
    if idcg == 0.0:
        return 0.0
    return dcg_at_k(actual_deduped, expected_set, k=k) / idcg


def mrr(actual: Sequence[str], expected: Iterable[str]) -> float:
    """Mean Reciprocal Rank: 1 / rank_of_first_relevant, or 0.0 if none found.

    Raises:
        EvalMetricError: When ``expected`` is empty.
    """
    expected_set = set(expected)
    if not expected_set:
        raise EvalMetricError("expected must not be empty")
    actual_deduped = _dedup_preserving_order(actual)
    for rank, doc in enumerate(actual_deduped, start=1):
        if doc in expected_set:
            return 1.0 / rank
    return 0.0


def recall_at_k(actual: Sequence[str], expected: Iterable[str], k: int = 20) -> float:
    """Recall at k: |relevant ∩ actual[:k]| / |expected|.

    Returns 0.0 when ``actual`` is empty.

    Raises:
        EvalMetricError: When ``expected`` is empty.
    """
    expected_set = set(expected)
    if not expected_set:
        raise EvalMetricError("expected must not be empty")
    if not actual:
        return 0.0
    actual_deduped = _dedup_preserving_order(actual)
    hits = sum(1 for doc in actual_deduped[:k] if doc in expected_set)
    return hits / len(expected_set)
