"""Pure unit tests for brain.set_similarity.jaccard. Synthetic keys only."""
from __future__ import annotations

import math

from brain.set_similarity import jaccard


def test_jaccard_identical_sets_is_one() -> None:
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint_sets_is_zero() -> None:
    assert jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_both_empty_is_zero() -> None:
    # Defined contract: empty union → 0.0 (never div-by-zero).
    assert jaccard(set(), set()) == 0.0
    assert jaccard(frozenset(), frozenset()) == 0.0


def test_jaccard_partial_overlap() -> None:
    # |{a,b} ∩ {a,c}| / |{a,b} ∪ {a,c}| = 1 / 3.
    assert math.isclose(jaccard({"a", "b"}, {"a", "c"}), 1 / 3)


def test_jaccard_subset() -> None:
    # |{a} ∩ {a,b,c}| / |{a} ∪ {a,b,c}| = 1 / 3.
    assert math.isclose(jaccard({"a"}, {"a", "b", "c"}), 1 / 3)


def test_jaccard_one_empty_one_nonempty_is_zero() -> None:
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a"}, set()) == 0.0


def test_jaccard_is_symmetric() -> None:
    a = {"a", "b", "c"}
    b = {"b", "c", "d"}
    assert jaccard(a, b) == jaccard(b, a)


def test_jaccard_accepts_frozenset_inputs() -> None:
    # The eval scorer passes frozensets; both set and frozenset must work.
    assert math.isclose(jaccard(frozenset({"x", "y"}), frozenset({"y", "z"})), 1 / 3)
