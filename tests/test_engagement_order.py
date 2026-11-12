"""The engagement stage's pure parts: multiplier, order, reach and the stage over a fake conn.

RM-22b. No database: the multiplier and order are pure, and
:class:`FakeEngagementConn` answers the stage's one statement from a dict. All
data is synthetic.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import replace
from typing import Any

import pytest

from brain.search import RRF_K, SearchResult
from brain.search_engagement import (
    ENGAGEMENT_STAGE_NAME,
    MAX_MULTIPLIER,
    MIN_MULTIPLIER,
    DocEngagement,
    EngagementStage,
    engagement_multiplier,
    engagement_order,
    engagement_reach,
)
from tests.doc_leg_fakes import result

pytestmark = pytest.mark.nodb


def _m(opens: float = 0.0, useful: int = 0, irrelevant: int = 0, *, a: float, b: float) -> float:
    return engagement_multiplier(
        DocEngagement(opens_decayed=opens, useful=useful, irrelevant=irrelevant),
        alpha=a,
        beta=b,
    )


# --- the multiplier and its clamp ---------------------------------------------------


def test_the_bounds_are_the_specs() -> None:
    assert (MIN_MULTIPLIER, MAX_MULTIPLIER) == (0.8, 1.25)


def test_no_engagement_is_one() -> None:
    assert _m(a=1.0, b=1.0) == 1.0


def test_alpha_scales_log1p_of_decayed_opens() -> None:
    assert _m(opens=0.1, a=1.0, b=0.0) == pytest.approx(1 + math.log1p(0.1))


def test_beta_scales_net_ratings() -> None:
    assert _m(useful=1, a=0.0, b=0.1) == pytest.approx(1.1)
    assert _m(irrelevant=1, a=0.0, b=0.1) == pytest.approx(0.9)


def test_the_boost_is_capped() -> None:
    assert _m(opens=500.0, useful=40, a=1.0, b=1.0) == MAX_MULTIPLIER


def test_the_demotion_is_floored() -> None:
    # Spec RM-22b.D1: demote on rated_irrelevant, within the clamp.
    assert _m(irrelevant=40, a=0.0, b=1.0) == MIN_MULTIPLIER


def test_values_inside_the_clamp_pass_through_unchanged() -> None:
    # The ordinary twin of the two clamp tests: the clamp bites only at its ends.
    assert _m(useful=1, a=0.0, b=0.1) == pytest.approx(1.1)
    assert _m(irrelevant=1, a=0.0, b=0.1) == pytest.approx(0.9)


def test_zero_weights_give_one_whatever_the_data() -> None:
    assert _m(opens=50.0, useful=3, irrelevant=1, a=0.0, b=0.0) == 1.0


# --- the order ------------------------------------------------------------------------


def test_all_ones_is_the_identity() -> None:
    assert engagement_order(6, {}) == list(range(6))
    assert engagement_order(6, dict.fromkeys(range(6), 1.0)) == list(range(6))


def test_a_boosted_row_rises_by_its_positional_base() -> None:
    # Index 4 at 1.25 beats index 0 at 1.0: 1.25/65 > 1/61.
    assert engagement_order(5, {4: MAX_MULTIPLIER}) == [4, 0, 1, 2, 3]


def test_a_demoted_row_falls() -> None:
    # 0.8/61 < 1/(61+k) for k <= 15, so index 0 drops below indices 1..4.
    assert engagement_order(5, {0: MIN_MULTIPLIER}) == [1, 2, 3, 4, 0]


def test_ties_keep_the_incoming_order() -> None:
    # Two rows boosted equally keep their relative order.
    order = engagement_order(4, {2: MAX_MULTIPLIER, 3: MAX_MULTIPLIER})

    assert order.index(2) < order.index(3)


def test_a_small_boost_moves_a_row_only_as_far_as_its_base_allows() -> None:
    # 1.1 lifts index 10 past index 4 (1.1/71 > 1/65) but not past index 3 (1/64).
    order = engagement_order(12, {10: 1.1})

    assert order.index(10) == 4


# --- the reach (D2's depth) -----------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 5, 10, 20, 50])
def test_the_reach_is_the_deepest_row_that_can_enter_the_top(limit: int) -> None:
    reach = engagement_reach(limit)
    worst_above = dict.fromkeys(range(reach + 1), MIN_MULTIPLIER)

    can = engagement_order(reach + 1, {**worst_above, reach - 1: MAX_MULTIPLIER})
    cannot = engagement_order(reach + 1, {**worst_above, reach: MAX_MULTIPLIER})

    assert can.index(reach - 1) < limit
    assert cannot.index(reach) >= limit


def test_the_reach_values_the_plan_records() -> None:
    assert [engagement_reach(n) for n in (5, 10, 20)] == [41, 49, 64]
    assert RRF_K == 60


def test_the_stage_asks_for_at_least_its_reach() -> None:
    stage = EngagementStage(alpha=1.0, beta=0.0)

    assert stage.candidate_limit(10) == engagement_reach(10) == 49
    assert stage.candidate_limit(1000) >= 1000


# --- the stage over a fake connection ---------------------------------------------------


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeEngagementConn:
    """Answers the stage's statement: ``(doc_id, opens_decayed, useful, irrelevant)`` rows."""

    def __init__(self, data: dict[str, tuple[float, int, int]]) -> None:
        self.data = data
        self.calls: list[dict[str, Any]] = []

    def execute(self, sql: str, params: dict[str, Any]) -> _Rows:
        self.calls.append(params)
        return _Rows([(i, *self.data[i]) for i in params["ids"] if i in self.data])


def _ids(rows: list[SearchResult]) -> list[str]:
    return [r.document_id for r in rows]


def _reversed_scores(n: int) -> list[SearchResult]:
    """Rows whose ``score`` RISES down the list, as after a reranker or a doc leg."""
    return [replace(result(i), score=float(i)) for i in range(n)]


def test_the_incoming_order_survives_when_nothing_is_engaged() -> None:
    # D2, pinned by team-lead's ruling: a reranker / doc-leg / rewrite order has
    # ``score`` out of step with position. A stage sorting on ``score`` would
    # reverse this list; the positional base keeps it.
    rows = _reversed_scores(5)
    stage = EngagementStage(alpha=1.0, beta=1.0)

    outcome = stage.apply(FakeEngagementConn({}), query="q", session_id=None, results=rows)  # type: ignore[arg-type]

    assert _ids(outcome.results) == _ids(rows)
    assert outcome.report.applied
    assert outcome.report.skipped_reason is None


def test_the_score_is_never_overwritten() -> None:
    rows = _reversed_scores(5)
    conn = FakeEngagementConn({"doc-4": (50.0, 0, 0)})

    outcome = EngagementStage(alpha=1.0, beta=0.0).apply(
        conn, query="q", session_id=None, results=rows  # type: ignore[arg-type]
    )

    assert _ids(outcome.results)[0] == "doc-4"
    assert {r.document_id: r.score for r in outcome.results} == {
        r.document_id: r.score for r in rows
    }


def test_an_empty_input_applies_without_a_query() -> None:
    conn = FakeEngagementConn({})

    outcome = EngagementStage(alpha=1.0, beta=0.0).apply(
        conn, query="q", session_id=None, results=[]  # type: ignore[arg-type]
    )

    assert outcome.results == []
    assert outcome.report.applied
    assert conn.calls == []


def test_the_stage_passes_its_session_as_a_uuid() -> None:
    conn = FakeEngagementConn({})
    session = uuid.uuid4()

    EngagementStage(alpha=1.0, beta=0.0).apply(
        conn, query="q", session_id=str(session), results=[result(0)]  # type: ignore[arg-type]
    )

    assert conn.calls[0]["session"] == session


def test_a_malformed_session_excludes_nothing() -> None:
    conn = FakeEngagementConn({})

    EngagementStage(alpha=1.0, beta=0.0).apply(
        conn, query="q", session_id="not-a-uuid", results=[result(0)]  # type: ignore[arg-type]
    )

    assert conn.calls[0]["session"] is None


def test_the_stage_names_itself_and_signs_both_weights() -> None:
    stage = EngagementStage(alpha=0.5, beta=0.25)

    assert stage.name == ENGAGEMENT_STAGE_NAME == "engagement"
    assert stage.signature() == {"engagement_alpha": 0.5, "engagement_beta": 0.25}


def test_the_stage_does_not_need_the_query_vector() -> None:
    assert not getattr(EngagementStage(alpha=1.0, beta=0.0), "needs_query_vector", False)
