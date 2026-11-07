"""``search_doc_leg.doc_leg_order``: the document leg's pure fusion (roadmap ticket RM-20).

The order is a permutation of pipeline indices. Rows with a document vector are
re-ranked among themselves by ``rrf(pipeline index) + weight * rrf(cosine rank)``;
a row without one keeps its exact index (plan D6). All values are synthetic.
"""
from __future__ import annotations

import pytest

from brain.rank_fusion import rrf_contribution
from brain.search_doc_leg import doc_leg_order

pytestmark = pytest.mark.nodb


def test_no_vectors_is_the_identity() -> None:
    assert doc_leg_order(4, {}, weight=1.0) == [0, 1, 2, 3]


def test_empty_input_is_empty() -> None:
    assert doc_leg_order(0, {}, weight=1.0) == []


def test_weight_zero_keeps_the_pipeline_order() -> None:
    cosines = {0: 0.1, 1: 0.2, 2: 0.9}

    assert doc_leg_order(3, cosines, weight=0.0) == [0, 1, 2]


def test_a_hand_computed_case_with_an_exact_tie_keeps_the_earlier_row_first() -> None:
    # Cosine ranks: row 2 -> 0, row 1 -> 1, row 0 -> 2. Rows 0 and 2 fuse to
    # exactly the same sum (1/61 + 1/63), so the stable sort keeps row 0 first.
    cosines = {0: 0.1, 1: 0.5, 2: 0.9}
    fused = {
        0: rrf_contribution(0) + rrf_contribution(2),
        1: rrf_contribution(1) + rrf_contribution(1),
        2: rrf_contribution(2) + rrf_contribution(0),
    }
    assert fused[0] == fused[2] > fused[1]

    assert doc_leg_order(3, cosines, weight=1.0) == [0, 2, 1]


def test_a_low_ranked_row_whose_document_fits_rises() -> None:
    cosines = {i: 0.5 - i * 0.001 for i in range(40)}
    cosines[30] = 0.99

    order = doc_leg_order(40, cosines, weight=1.0)

    assert order.index(30) < 15
    assert sorted(order) == list(range(40))


def test_a_heavier_weight_lifts_it_further() -> None:
    cosines = {i: 0.5 - i * 0.001 for i in range(40)}
    cosines[30] = 0.99

    light = doc_leg_order(40, cosines, weight=1.0).index(30)
    heavy = doc_leg_order(40, cosines, weight=10.0).index(30)

    assert heavy < light


def test_a_row_without_a_vector_keeps_its_exact_index() -> None:
    # Row 1 has no vector (not backfilled, or confidential under a hosted
    # embedder). Rows 0, 2 and 3 reorder among slots 0, 2 and 3 only.
    cosines = {0: 0.1, 2: 0.2, 3: 0.99}

    order = doc_leg_order(4, cosines, weight=10.0)

    assert order[1] == 1
    assert order[0] == 3
    assert sorted(order) == [0, 1, 2, 3]


@pytest.mark.parametrize("missing", [0, 2, 5])
def test_every_vectorless_index_is_a_fixed_point(missing: int) -> None:
    cosines = {i: float(i) for i in range(6) if i != missing}

    assert doc_leg_order(6, cosines, weight=10.0)[missing] == missing


def test_equal_cosines_keep_the_pipeline_order() -> None:
    assert doc_leg_order(3, {0: 0.4, 1: 0.4, 2: 0.4}, weight=1.0) == [0, 1, 2]
