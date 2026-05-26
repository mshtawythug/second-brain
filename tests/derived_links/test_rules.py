"""Tests for brain.vault.derived_links.rules — pure rule functions R1/R2/R3."""
import datetime
from collections.abc import Iterable
from typing import Any, Literal

import pytest

from brain.vault.derived_links.rules import (
    WEIGHT_SAME_DAY_PARTICIPANT,
    WEIGHT_SHARED_PARTICIPANT,
    WEIGHT_SHARED_THREAD,
    DocSnapshot,
    rule_same_day_participant,
    rule_shared_participant,
    rule_shared_thread,
)


def make_doc(
    document_id: str = "doc-1",
    source_kind: Literal["gmail", "krisp", "manual"] | None = "gmail",
    metadata: dict[str, Any] | None = None,
    participants: Iterable[str] = (),
    date: datetime.date | None = None,
) -> DocSnapshot:
    """Compact DocSnapshot factory for readable test cases (no mystery guests)."""
    return DocSnapshot(
        document_id=document_id,
        source_kind=source_kind,
        metadata=metadata or {},
        participant_keys=frozenset(participants),
        date=date,
    )


class TestRuleSharedThread:
    """R1 — Gmail↔Gmail edge when both share `metadata.thread_id`."""

    def test_both_gmail_same_thread_fires(self) -> None:
        a = make_doc(document_id="a", metadata={"thread_id": "t-123"})
        b = make_doc(document_id="b", metadata={"thread_id": "t-123"})

        evidence = rule_shared_thread(a, b)

        assert evidence is not None
        assert evidence.rule == "shared_thread"
        assert evidence.weight == WEIGHT_SHARED_THREAD
        assert evidence.payload == {"thread_id": "t-123"}

    def test_both_gmail_different_threads_returns_none(self) -> None:
        a = make_doc(document_id="a", metadata={"thread_id": "t-1"})
        b = make_doc(document_id="b", metadata={"thread_id": "t-2"})

        assert rule_shared_thread(a, b) is None

    @pytest.mark.parametrize(
        "kind_a, kind_b",
        [
            ("gmail", "krisp"),
            ("krisp", "gmail"),
            ("krisp", "krisp"),
            ("gmail", "manual"),
            ("manual", "gmail"),
            ("gmail", None),
            (None, "gmail"),
        ],
    )
    def test_non_gmail_pair_returns_none(
        self,
        kind_a: Literal["gmail", "krisp", "manual"] | None,
        kind_b: Literal["gmail", "krisp", "manual"] | None,
    ) -> None:
        a = make_doc(document_id="a", source_kind=kind_a, metadata={"thread_id": "t-1"})
        b = make_doc(document_id="b", source_kind=kind_b, metadata={"thread_id": "t-1"})

        assert rule_shared_thread(a, b) is None

    @pytest.mark.parametrize("missing_value", [{}, {"thread_id": ""}, {"thread_id": "   "}])
    def test_missing_or_empty_thread_id_returns_none(
        self, missing_value: dict[str, Any]
    ) -> None:
        a = make_doc(document_id="a", metadata={"thread_id": "t-1"})
        b = make_doc(document_id="b", metadata=missing_value)

        assert rule_shared_thread(a, b) is None
        # And the symmetric direction.
        assert rule_shared_thread(b, a) is None

    def test_self_pair_returns_none(self) -> None:
        snap = make_doc(document_id="same", metadata={"thread_id": "t-1"})

        assert rule_shared_thread(snap, snap) is None

    @pytest.mark.parametrize("bad_value", [None, 42, ["t-1"], {"id": "t-1"}, 3.14])
    def test_non_string_thread_id_returns_none(self, bad_value: Any) -> None:
        a = make_doc(document_id="a", metadata={"thread_id": bad_value})
        b = make_doc(document_id="b", metadata={"thread_id": "t-1"})

        assert rule_shared_thread(a, b) is None
        assert rule_shared_thread(b, a) is None


class TestRuleSharedParticipant:
    """R2 — edge when participant_keys intersect (any source kind)."""

    def test_single_shared_participant_fires(self) -> None:
        a = make_doc(document_id="a", participants={"person-a@example.com"})
        b = make_doc(document_id="b", participants={"person-a@example.com", "pat@example.com"})

        evidence = rule_shared_participant(a, b)

        assert evidence is not None
        assert evidence.rule == "shared_participant"
        assert evidence.weight == WEIGHT_SHARED_PARTICIPANT
        assert evidence.payload == {
            "participant": "person-a@example.com",
            "shared_count": 1,
        }

    def test_multiple_shared_picks_lowest_sorted_key(self) -> None:
        a = make_doc(
            document_id="a",
            participants={"person-a@example.com", "pat@example.com", "zoe@example.com"},
        )
        b = make_doc(
            document_id="b",
            participants={"person-a@example.com", "pat@example.com", "zoe@example.com"},
        )

        evidence = rule_shared_participant(a, b)

        assert evidence is not None
        # sorted({...})[0] over these is "pat@example.com".
        assert evidence.payload == {"participant": "pat@example.com", "shared_count": 3}

    def test_disjoint_participants_returns_none(self) -> None:
        a = make_doc(document_id="a", participants={"alice@example.com"})
        b = make_doc(document_id="b", participants={"bob@example.com"})

        assert rule_shared_participant(a, b) is None

    def test_self_pair_returns_none(self) -> None:
        snap = make_doc(document_id="same", participants={"person-a@example.com"})

        assert rule_shared_participant(snap, snap) is None

    def test_empty_participants_on_a_returns_none(self) -> None:
        a = make_doc(document_id="a", participants=set())
        b = make_doc(document_id="b", participants={"person-a@example.com"})

        assert rule_shared_participant(a, b) is None

    def test_empty_participants_on_b_returns_none(self) -> None:
        a = make_doc(document_id="a", participants={"person-a@example.com"})
        b = make_doc(document_id="b", participants=set())

        assert rule_shared_participant(a, b) is None

    def test_both_empty_returns_none(self) -> None:
        a = make_doc(document_id="a", participants=set())
        b = make_doc(document_id="b", participants=set())

        assert rule_shared_participant(a, b) is None

    @pytest.mark.parametrize(
        "kind_a, kind_b",
        [
            ("krisp", "gmail"),
            ("krisp", "krisp"),
            ("gmail", "gmail"),
            ("manual", "gmail"),
            ("manual", "manual"),
            (None, "gmail"),
            ("krisp", None),
        ],
    )
    def test_works_across_any_source_kind_pair(
        self,
        kind_a: Literal["gmail", "krisp", "manual"] | None,
        kind_b: Literal["gmail", "krisp", "manual"] | None,
    ) -> None:
        # R2 doesn't care about source — verify cross-kind, same-kind, and None.
        a = make_doc(document_id="a", source_kind=kind_a, participants={"person-a@example.com"})
        b = make_doc(document_id="b", source_kind=kind_b, participants={"person-a@example.com"})

        evidence = rule_shared_participant(a, b)

        assert evidence is not None
        assert evidence.rule == "shared_participant"
        assert evidence.payload["participant"] == "person-a@example.com"

    def test_name_only_participant_works(self) -> None:
        # Names (no email) are valid participant keys when no canonical email exists.
        a = make_doc(document_id="a", participants={"person-a last-a"})
        b = make_doc(document_id="b", participants={"person-a last-a"})

        evidence = rule_shared_participant(a, b)

        assert evidence is not None
        assert evidence.payload["participant"] == "person-a last-a"


class TestRuleSameDayParticipant:
    """R3 — Krisp↔Gmail, shared participant, dates within ±1 day."""

    def test_krisp_and_gmail_same_date_fires(self) -> None:
        krisp = make_doc(
            document_id="k",
            source_kind="krisp",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )
        gmail = make_doc(
            document_id="g",
            source_kind="gmail",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )

        evidence = rule_same_day_participant(krisp, gmail)

        assert evidence is not None
        assert evidence.rule == "same_day_participant"
        assert evidence.weight == WEIGHT_SAME_DAY_PARTICIPANT
        assert evidence.payload == {
            "participant": "person-a@example.com",
            "krisp_date": "2026-04-15",
            "gmail_date": "2026-04-15",
            "day_delta": 0,
        }

    @pytest.mark.parametrize(
        "krisp_date, gmail_date, expected_delta",
        [
            (datetime.date(2026, 4, 15), datetime.date(2026, 4, 14), 1),  # gmail prior day
            (datetime.date(2026, 4, 15), datetime.date(2026, 4, 16), 1),  # gmail next day
            (datetime.date(2026, 4, 15), datetime.date(2026, 4, 15), 0),  # same day
        ],
    )
    def test_dates_within_one_day_fire(
        self,
        krisp_date: datetime.date,
        gmail_date: datetime.date,
        expected_delta: int,
    ) -> None:
        krisp = make_doc(
            document_id="k",
            source_kind="krisp",
            participants={"person-a@example.com"},
            date=krisp_date,
        )
        gmail = make_doc(
            document_id="g",
            source_kind="gmail",
            participants={"person-a@example.com"},
            date=gmail_date,
        )

        evidence = rule_same_day_participant(krisp, gmail)

        assert evidence is not None
        assert evidence.payload["day_delta"] == expected_delta
        assert evidence.payload["krisp_date"] == krisp_date.isoformat()
        assert evidence.payload["gmail_date"] == gmail_date.isoformat()

    @pytest.mark.parametrize(
        "krisp_date, gmail_date",
        [
            (datetime.date(2026, 4, 15), datetime.date(2026, 4, 13)),  # 2 days prior
            (datetime.date(2026, 4, 15), datetime.date(2026, 4, 17)),  # 2 days after
            (datetime.date(2026, 4, 15), datetime.date(2026, 5, 15)),  # ~30 days
            (datetime.date(2026, 4, 15), datetime.date(2025, 4, 15)),  # year apart
        ],
    )
    def test_dates_more_than_one_day_apart_returns_none(
        self, krisp_date: datetime.date, gmail_date: datetime.date
    ) -> None:
        krisp = make_doc(
            document_id="k",
            source_kind="krisp",
            participants={"person-a@example.com"},
            date=krisp_date,
        )
        gmail = make_doc(
            document_id="g",
            source_kind="gmail",
            participants={"person-a@example.com"},
            date=gmail_date,
        )

        assert rule_same_day_participant(krisp, gmail) is None

    def test_no_shared_participant_returns_none(self) -> None:
        krisp = make_doc(
            document_id="k",
            source_kind="krisp",
            participants={"alice@example.com"},
            date=datetime.date(2026, 4, 15),
        )
        gmail = make_doc(
            document_id="g",
            source_kind="gmail",
            participants={"bob@example.com"},
            date=datetime.date(2026, 4, 15),
        )

        assert rule_same_day_participant(krisp, gmail) is None

    def test_krisp_krisp_same_date_returns_none(self) -> None:
        a = make_doc(
            document_id="a",
            source_kind="krisp",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )
        b = make_doc(
            document_id="b",
            source_kind="krisp",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )

        assert rule_same_day_participant(a, b) is None

    def test_gmail_gmail_same_date_returns_none(self) -> None:
        a = make_doc(
            document_id="a",
            source_kind="gmail",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )
        b = make_doc(
            document_id="b",
            source_kind="gmail",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )

        assert rule_same_day_participant(a, b) is None

    @pytest.mark.parametrize(
        "kind_a, kind_b",
        [
            ("manual", "gmail"),
            ("krisp", "manual"),
            ("manual", "manual"),
            (None, "gmail"),
            ("krisp", None),
            (None, None),
        ],
    )
    def test_non_krisp_gmail_pair_returns_none(
        self,
        kind_a: Literal["gmail", "krisp", "manual"] | None,
        kind_b: Literal["gmail", "krisp", "manual"] | None,
    ) -> None:
        a = make_doc(
            document_id="a",
            source_kind=kind_a,
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )
        b = make_doc(
            document_id="b",
            source_kind=kind_b,
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )

        assert rule_same_day_participant(a, b) is None

    def test_krisp_missing_date_returns_none(self) -> None:
        krisp = make_doc(
            document_id="k",
            source_kind="krisp",
            participants={"person-a@example.com"},
            date=None,
        )
        gmail = make_doc(
            document_id="g",
            source_kind="gmail",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )

        assert rule_same_day_participant(krisp, gmail) is None

    def test_gmail_missing_date_returns_none(self) -> None:
        krisp = make_doc(
            document_id="k",
            source_kind="krisp",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )
        gmail = make_doc(
            document_id="g",
            source_kind="gmail",
            participants={"person-a@example.com"},
            date=None,
        )

        assert rule_same_day_participant(krisp, gmail) is None

    def test_self_pair_returns_none(self) -> None:
        # Same document with both source_kinds — shouldn't matter, document_id
        # equality short-circuits first.
        snap = make_doc(
            document_id="same",
            source_kind="krisp",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )

        assert rule_same_day_participant(snap, snap) is None

    @pytest.mark.parametrize(
        "argument_order",
        ["krisp_first", "gmail_first"],
    )
    def test_commutative_on_input_order(self, argument_order: str) -> None:
        # Either-direction order should produce identical evidence (modulo the
        # rule itself never reading the order). The payload's krisp_date and
        # gmail_date are pinned by source_kind, not by argument position.
        krisp = make_doc(
            document_id="k",
            source_kind="krisp",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 15),
        )
        gmail = make_doc(
            document_id="g",
            source_kind="gmail",
            participants={"person-a@example.com"},
            date=datetime.date(2026, 4, 16),
        )

        if argument_order == "krisp_first":
            evidence = rule_same_day_participant(krisp, gmail)
        else:
            evidence = rule_same_day_participant(gmail, krisp)

        assert evidence is not None
        assert evidence.rule == "same_day_participant"
        assert evidence.weight == WEIGHT_SAME_DAY_PARTICIPANT
        assert evidence.payload == {
            "participant": "person-a@example.com",
            "krisp_date": "2026-04-15",
            "gmail_date": "2026-04-16",
            "day_delta": 1,
        }

    def test_multiple_shared_participants_picks_lowest_sorted_key(self) -> None:
        # Verify representative-key selection works in R3 too.
        krisp = make_doc(
            document_id="k",
            source_kind="krisp",
            participants={"person-a@example.com", "pat@example.com", "zoe@example.com"},
            date=datetime.date(2026, 4, 15),
        )
        gmail = make_doc(
            document_id="g",
            source_kind="gmail",
            participants={"person-a@example.com", "pat@example.com"},
            date=datetime.date(2026, 4, 15),
        )

        evidence = rule_same_day_participant(krisp, gmail)

        assert evidence is not None
        assert evidence.payload["participant"] == "pat@example.com"
