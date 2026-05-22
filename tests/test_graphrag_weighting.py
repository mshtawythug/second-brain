"""Unit tests for :mod:`brain.graph_rag.weighting` (wave G1, pure logic, no DB).

Covers the normalized-lift formula against hand-computed values, the ``(0, 1]``
range guarantee, the generic-entity document-frequency cap (including the
spec's ``round`` semantics), drop-on-suppression behavior, the validation
errors, and the version-constant exposure that feeds
``graph_index_state.suppress_ver``.
"""
from __future__ import annotations

import pytest

from brain.errors import WeightingError
from brain.graph_rag.weighting import (
    DEFAULT_GENERIC_DF,
    WEIGHTING_VERSION,
    edge_weight,
    generic_df_cap,
    is_generic_entity,
    is_suppressed_edge,
    normalized_lift,
    suppress_ver,
)


# --------------------------------------------------------------------------- #
# normalized_lift — formula correctness vs hand-computed values
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("co_doc", "src_doc", "dst_doc", "expected"),
    [
        (2, 4, 5, 0.5),  # 2 / min(4, 5) = 0.5
        (3, 3, 10, 1.0),  # subset: A always with B -> 1.0 ceiling
        (1, 100, 100, 0.01),  # rare co-occurrence stays strictly positive
        (5, 5, 5, 1.0),  # identical document sets
        (3, 6, 4, 0.75),  # 3 / min(6, 4) = 3/4
    ],
)
def test_normalized_lift_matches_hand_computed(
    co_doc: int, src_doc: int, dst_doc: int, expected: float
) -> None:
    assert normalized_lift(co_doc, src_doc, dst_doc) == pytest.approx(expected)


def test_normalized_lift_is_symmetric_in_marginals() -> None:
    assert normalized_lift(2, 4, 8) == pytest.approx(normalized_lift(2, 8, 4))


def test_normalized_lift_always_in_unit_interval() -> None:
    # Spot-check the (0, 1] range guarantee across many valid inputs.
    for src in range(1, 12):
        for dst in range(1, 12):
            min_df = min(src, dst)
            for co in range(1, min_df + 1):
                weight = normalized_lift(co, src, dst)
                assert 0.0 < weight <= 1.0


@pytest.mark.parametrize("bad_co", [0, -1])
def test_normalized_lift_rejects_non_positive_co_doc(bad_co: int) -> None:
    with pytest.raises(WeightingError, match="co_doc_count must be >= 1"):
        normalized_lift(bad_co, 5, 5)


@pytest.mark.parametrize(("src", "dst"), [(0, 5), (5, 0), (0, 0)])
def test_normalized_lift_rejects_non_positive_marginal(src: int, dst: int) -> None:
    with pytest.raises(WeightingError, match="document frequencies must be >= 1"):
        normalized_lift(1, src, dst)


def test_normalized_lift_rejects_co_doc_above_min_marginal() -> None:
    # A pair cannot co-occur in more documents than its rarer entity appears in.
    with pytest.raises(WeightingError, match="cannot exceed the rarer endpoint"):
        normalized_lift(5, 3, 10)


# --------------------------------------------------------------------------- #
# generic_df_cap — absolute cap + round semantics
# --------------------------------------------------------------------------- #
def test_generic_df_cap_default_ratio() -> None:
    # round(100 * 0.30) = 30.
    assert generic_df_cap(100) == 30
    assert generic_df_cap(100, DEFAULT_GENERIC_DF) == 30


def test_generic_df_cap_custom_ratio() -> None:
    assert generic_df_cap(200, 0.10) == 20
    assert generic_df_cap(1000, 1.0) == 1000


def test_generic_df_cap_zero_corpus() -> None:
    assert generic_df_cap(0) == 0


def test_generic_df_cap_uses_bankers_rounding() -> None:
    # Spec says plain "round"; Python's round() is round-half-to-even.
    assert generic_df_cap(5, 0.5) == 2  # round(2.5) -> 2
    assert generic_df_cap(7, 0.5) == 4  # round(3.5) -> 4


def test_generic_df_cap_rejects_negative_corpus() -> None:
    with pytest.raises(WeightingError, match="corpus_doc_count must be >= 0"):
        generic_df_cap(-1)


@pytest.mark.parametrize("bad_ratio", [0.0, -0.1, 1.5, 2.0])
def test_generic_df_cap_rejects_out_of_range_ratio(bad_ratio: float) -> None:
    with pytest.raises(WeightingError, match=r"generic_df_ratio must be in \(0, 1\]"):
        generic_df_cap(100, bad_ratio)


# --------------------------------------------------------------------------- #
# is_generic_entity / is_suppressed_edge
# --------------------------------------------------------------------------- #
def test_is_generic_entity_strict_greater_than_cap() -> None:
    assert is_generic_entity(31, 30) is True
    assert is_generic_entity(30, 30) is False  # exactly at cap is kept
    assert is_generic_entity(0, 30) is False


def test_is_suppressed_edge_either_endpoint_generic() -> None:
    assert is_suppressed_edge(31, 5, 30) is True  # src generic
    assert is_suppressed_edge(5, 31, 30) is True  # dst generic
    assert is_suppressed_edge(40, 40, 30) is True  # both generic
    assert is_suppressed_edge(5, 5, 30) is False  # neither generic


# --------------------------------------------------------------------------- #
# edge_weight — suppression drops the edge (None), else lift
# --------------------------------------------------------------------------- #
def test_edge_weight_returns_lift_when_not_suppressed() -> None:
    assert edge_weight(2, 4, 5, cap=30) == pytest.approx(0.5)


def test_edge_weight_drops_suppressed_edge() -> None:
    # src df 40 > cap 30 -> generic -> edge dropped.
    assert edge_weight(2, 40, 5, cap=30) is None
    assert edge_weight(2, 5, 40, cap=30) is None


def test_edge_weight_propagates_weighting_error_when_not_suppressed() -> None:
    # Non-suppressed but impossible counts still raise (no silent bad weight).
    with pytest.raises(WeightingError, match="cannot exceed the rarer endpoint"):
        edge_weight(5, 3, 10, cap=30)


def test_edge_weight_suppression_takes_precedence_over_bad_counts() -> None:
    # When suppressed, the lift is never computed, so impossible co-doc is moot.
    assert edge_weight(999, 40, 5, cap=30) is None


def test_edge_weight_keeps_endpoint_exactly_at_cap() -> None:
    # Suppression is strict greater-than (is_generic_entity), so an endpoint
    # whose document frequency lands *exactly* on the cap is kept and weighted,
    # never dropped. This boundary is the difference between materializing an
    # edge and silently suppressing it, so it gets its own edge_weight test.
    # df(src)=30 == cap 30, df(dst)=5 -> lift = 2 / min(30, 5) = 0.4.
    assert edge_weight(2, 30, 5, cap=30) == pytest.approx(0.4)
    # Both endpoints exactly at the cap -> still kept: 3 / min(30, 30) = 0.1.
    assert edge_weight(3, 30, 30, cap=30) == pytest.approx(0.1)
    # One tick above the cap -> suppressed (returns None).
    assert edge_weight(2, 31, 5, cap=30) is None


# --------------------------------------------------------------------------- #
# Version constants + suppress_ver composition
# --------------------------------------------------------------------------- #
def test_weighting_version_constant_exposed() -> None:
    assert WEIGHTING_VERSION == "nlift-v1"
    assert DEFAULT_GENERIC_DF == 0.30


def test_suppress_ver_default_ratio() -> None:
    assert suppress_ver() == "nlift-v1:gdf=0.3"
    assert suppress_ver(0.30) == "nlift-v1:gdf=0.3"  # stable float spelling


def test_suppress_ver_custom_ratio() -> None:
    assert suppress_ver(0.25) == "nlift-v1:gdf=0.25"
    assert suppress_ver(0.5) == "nlift-v1:gdf=0.5"


def test_suppress_ver_embeds_algorithm_version() -> None:
    assert WEIGHTING_VERSION in suppress_ver()


def test_suppress_ver_distinct_ratios_do_not_collide() -> None:
    # Two distinct ratios that the old 6-significant-figure ``:g`` format folded
    # to the SAME string ("0.123456") must now yield distinct suppress_ver keys.
    # Otherwise a genuine GENERIC_DF change would collide with the prior key and
    # silently skip the corpus-wide re-derive (the watermark would match).
    ratio_a = 0.1234561
    ratio_b = 0.1234562
    assert ratio_a != ratio_b  # distinct floats
    assert format(ratio_a, "g") == format(ratio_b, "g")  # the old format collided
    assert suppress_ver(ratio_a) != suppress_ver(ratio_b)  # repr keeps them apart
