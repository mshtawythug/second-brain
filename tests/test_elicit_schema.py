"""Tests for frozen value objects in brain.elicit.schema."""
import pytest

from brain.elicit.schema import ElicitDraft, ElicitOutcome, Gap


def test_gap_is_frozen_and_holds_evidence():
    g = Gap(
        gap_id="11111111-1111-1111-1111-111111111111",
        signal_kind="delta",
        target_type="person",
        target_id="e1",
        score=0.9,
        evidence_ids=["d1", "d2", "d3"],
        evidence_texts=["s1", "s2", "s3"],
        rationale="referenced in 3 calls, never authored",
    )
    assert g.target_type == "person"
    assert len(g.evidence_ids) == 3
    with pytest.raises(AttributeError):
        g.score = 0.1  # frozen


def test_outcome_actions():
    o = ElicitOutcome(gap_id="g1", action="accepted", note_id="n1", snoozed_days=None)
    assert o.action == "accepted"


def test_gap_defaults():
    g = Gap(
        gap_id="22222222-2222-2222-2222-222222222222",
        signal_kind="orphan",
        target_type="org",
        target_id="e2",
        score=0.5,
    )
    assert g.evidence_ids == []
    assert g.evidence_texts == []
    assert g.rationale == ""


def test_elicit_draft_holds_fields():
    d = ElicitDraft(
        gap_id="g2",
        title="Write about Acme",
        draft_text="Acme is a long-time partner that...",
        evidence_ids=["d1"],
        evidence_texts=["Acme was mentioned in the Q1 review"],
    )
    assert d.title == "Write about Acme"
    assert len(d.evidence_ids) == 1


def test_elicit_outcome_snoozed():
    o = ElicitOutcome(gap_id="g3", action="snoozed", note_id=None, snoozed_days=7)
    assert o.snoozed_days == 7
    assert o.note_id is None
