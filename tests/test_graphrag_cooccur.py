"""Unit tests for :mod:`brain.graph_rag.cooccur` (wave G1, pure logic, no DB).

Covers the sliding-window predicate (inclusive max-distance), canonical
``src < dst`` pair ordering, the repeated-mention / overlapping-window /
no-co-occurrer edge cases, the per-document max-entities cap (with deterministic
tie-breaking), the ``EdgeContribution`` adapter, and the validation errors.
UUIDs are synthetic, lowercase-canonical strings whose lexicographic order
(A < B < C < D) mirrors PostgreSQL's ``uuid`` byte ordering.
"""
from __future__ import annotations

import dataclasses

import pytest

from brain.errors import CooccurrenceError
from brain.graph_rag.cooccur import (
    DEFAULT_COOCCUR_WINDOW,
    DEFAULT_MAX_ENTITIES_PER_DOC,
    EntityOccurrence,
    cooccurrence_counts,
    to_contributions,
)
from brain.graph_rag.schema import EdgeContribution

_A = "11111111-1111-4111-8111-111111111111"
_B = "22222222-2222-4222-8222-222222222222"
_C = "33333333-3333-4333-8333-333333333333"
_DOC = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _occ(entity_id: str, position: int) -> EntityOccurrence:
    return EntityOccurrence(entity_id=entity_id, position=position)


# --------------------------------------------------------------------------- #
# EntityOccurrence value object
# --------------------------------------------------------------------------- #
def test_entity_occurrence_is_frozen() -> None:
    occ = _occ(_A, 0)
    assert occ.entity_id == _A
    assert occ.position == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        occ.position = 5  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Module constants mirror the spec defaults
# --------------------------------------------------------------------------- #
def test_default_window_and_cap_match_spec() -> None:
    # spec §10: BRAIN_GRAPH_COOCCUR_WINDOW=3, BRAIN_GRAPH_MAX_ENTITIES_PER_DOC=40.
    assert DEFAULT_COOCCUR_WINDOW == 3
    assert DEFAULT_MAX_ENTITIES_PER_DOC == 40


# --------------------------------------------------------------------------- #
# Empty / single / no-co-occurrer inputs
# --------------------------------------------------------------------------- #
def test_empty_input_returns_empty_dict() -> None:
    assert cooccurrence_counts([]) == {}


def test_single_occurrence_has_no_pairs() -> None:
    assert cooccurrence_counts([_occ(_A, 0)]) == {}


def test_mention_with_no_co_occurrer_is_dropped() -> None:
    # B sits far outside A's window — no pair emitted.
    counts = cooccurrence_counts([_occ(_A, 0), _occ(_B, 100)], window=3)
    assert counts == {}


# --------------------------------------------------------------------------- #
# Window boundary (inclusive max distance)
# --------------------------------------------------------------------------- #
def test_window_boundary_is_inclusive() -> None:
    # distance == window counts; distance == window + 1 does not.
    counts = cooccurrence_counts(
        [_occ(_A, 0), _occ(_B, 3), _occ(_C, 4)], window=3
    )
    assert counts == {(_A, _B): 1, (_B, _C): 1}  # (A,C) dist 4 excluded


def test_distance_just_over_window_excluded() -> None:
    counts = cooccurrence_counts([_occ(_A, 0), _occ(_B, 4)], window=3)
    assert counts == {}


def test_zero_distance_distinct_entities_count() -> None:
    # Two distinct entities reported at the same position still co-occur.
    counts = cooccurrence_counts([_occ(_A, 5), _occ(_B, 5)], window=1)
    assert counts == {(_A, _B): 1}


# --------------------------------------------------------------------------- #
# Canonical ordering (src < dst) regardless of input order
# --------------------------------------------------------------------------- #
def test_canonical_pair_ordering_independent_of_input_order() -> None:
    forward = cooccurrence_counts([_occ(_A, 0), _occ(_B, 1)], window=3)
    reversed_input = cooccurrence_counts([_occ(_B, 1), _occ(_A, 0)], window=3)
    assert forward == {(_A, _B): 1}
    assert reversed_input == {(_A, _B): 1}  # still (A, B), never (B, A)
    for src, dst in forward:
        assert src < dst


def test_higher_id_first_in_position_still_canonical() -> None:
    # B (higher id) appears at the earlier position; key must still be (A, B).
    counts = cooccurrence_counts([_occ(_B, 0), _occ(_A, 2)], window=3)
    assert counts == {(_A, _B): 1}


# --------------------------------------------------------------------------- #
# Same entity multiple times + overlapping windows (no double counting)
# --------------------------------------------------------------------------- #
def test_same_entity_repeated_never_self_pairs() -> None:
    # A appears three times; no (A, A) edge can exist.
    counts = cooccurrence_counts(
        [_occ(_A, 0), _occ(_A, 1), _occ(_A, 2)], window=3
    )
    assert counts == {}


def test_repeated_mentions_accumulate_pair_counts() -> None:
    # A@0 pairs with B@1 (d1) and B@2 (d2); the B@1–B@2 self-pair is skipped.
    counts = cooccurrence_counts(
        [_occ(_A, 0), _occ(_B, 1), _occ(_B, 2)], window=3
    )
    assert counts == {(_A, _B): 2}


def test_overlapping_windows_count_each_pair_once() -> None:
    # Each unordered occurrence-pair within window is counted exactly once even
    # though a sliding window would visit overlapping spans. A{0,1}, B{2}:
    # (A@0,B@2) d2, (A@1,B@2) d1 -> 2; the A@0–A@1 self-pair is skipped.
    counts = cooccurrence_counts(
        [_occ(_A, 0), _occ(_A, 1), _occ(_B, 2)], window=3
    )
    assert counts == {(_A, _B): 2}


def test_sweep_excludes_far_pairs_with_near_ones_present() -> None:
    # Sorted-sweep early-break must not drop valid near pairs nor admit far ones.
    counts = cooccurrence_counts(
        [_occ(_A, 0), _occ(_B, 2), _occ(_C, 9)], window=3
    )
    assert counts == {(_A, _B): 1}  # (A,C) d9 and (B,C) d7 both excluded


def test_three_entities_all_in_window() -> None:
    counts = cooccurrence_counts(
        [_occ(_A, 0), _occ(_B, 1), _occ(_C, 2)], window=3
    )
    assert counts == {(_A, _B): 1, (_A, _C): 1, (_B, _C): 1}


# --------------------------------------------------------------------------- #
# Max-entities cap
# --------------------------------------------------------------------------- #
def test_cap_disabled_keeps_all_entities() -> None:
    occ = [
        _occ(_A, 0),
        _occ(_A, 1),
        _occ(_A, 2),
        _occ(_B, 3),
        _occ(_B, 4),
        _occ(_C, 5),
    ]
    counts = cooccurrence_counts(occ, window=10, max_entities=None)
    assert counts == {(_A, _B): 6, (_A, _C): 3, (_B, _C): 2}


def test_cap_keeps_most_mentioned_entities() -> None:
    occ = [
        _occ(_A, 0),
        _occ(_A, 1),
        _occ(_A, 2),  # A: 3 mentions
        _occ(_B, 3),
        _occ(_B, 4),  # B: 2 mentions
        _occ(_C, 5),  # C: 1 mention -> dropped at cap=2
    ]
    counts = cooccurrence_counts(occ, window=10, max_entities=2)
    assert counts == {(_A, _B): 6}


def test_cap_tie_break_is_deterministic_by_entity_id() -> None:
    # All three entities have equal frequency; cap=2 keeps the two smallest ids.
    occ = [_occ(_C, 0), _occ(_B, 1), _occ(_A, 2)]
    counts = cooccurrence_counts(occ, window=10, max_entities=2)
    assert counts == {(_A, _B): 1}  # C dropped (largest id loses the tie)


def test_cap_not_triggered_when_within_limit() -> None:
    occ = [_occ(_A, 0), _occ(_B, 1)]
    counts = cooccurrence_counts(occ, window=3, max_entities=5)
    assert counts == {(_A, _B): 1}


# --------------------------------------------------------------------------- #
# Validation errors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_window", [0, -1, -10])
def test_non_positive_window_raises(bad_window: int) -> None:
    with pytest.raises(CooccurrenceError, match="window must be a positive"):
        cooccurrence_counts([_occ(_A, 0), _occ(_B, 1)], window=bad_window)


@pytest.mark.parametrize("bad_cap", [0, -1, -5])
def test_non_positive_max_entities_raises(bad_cap: int) -> None:
    with pytest.raises(CooccurrenceError, match="max_entities must be a positive"):
        cooccurrence_counts([_occ(_A, 0)], max_entities=bad_cap)


# --------------------------------------------------------------------------- #
# to_contributions adapter
# --------------------------------------------------------------------------- #
def test_to_contributions_default_tenant_and_ordering() -> None:
    counts = {(_B, _C): 1, (_A, _B): 2}  # intentionally out of order
    rows = to_contributions(counts, document_id=_DOC)
    assert rows == [
        EdgeContribution(
            document_id=_DOC, src_id=_A, dst_id=_B, tenant_id="default", cooccur_count=2
        ),
        EdgeContribution(
            document_id=_DOC, src_id=_B, dst_id=_C, tenant_id="default", cooccur_count=1
        ),
    ]


def test_to_contributions_respects_explicit_tenant() -> None:
    rows = to_contributions({(_A, _B): 1}, document_id=_DOC, tenant_id="acme")
    assert len(rows) == 1
    assert rows[0].tenant_id == "acme"
    assert rows[0].src_id == _A
    assert rows[0].dst_id == _B
    assert rows[0].cooccur_count == 1


def test_to_contributions_empty_counts() -> None:
    assert to_contributions({}, document_id=_DOC) == []


def test_pipeline_counts_into_contributions() -> None:
    # End-to-end pure pipeline: occurrences -> counts -> contribution rows.
    counts = cooccurrence_counts(
        [_occ(_A, 0), _occ(_B, 1), _occ(_C, 2)], window=3
    )
    rows = to_contributions(counts, document_id=_DOC, tenant_id="t1")
    assert [(r.src_id, r.dst_id, r.cooccur_count) for r in rows] == [
        (_A, _B, 1),
        (_A, _C, 1),
        (_B, _C, 1),
    ]
    assert all(r.document_id == _DOC and r.tenant_id == "t1" for r in rows)
    assert all(r.src_id < r.dst_id for r in rows)
