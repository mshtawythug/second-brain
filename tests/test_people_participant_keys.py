"""``people._doc_participant_keys``'s ``calendar`` branch (RM-30, rev-96 R2; aud-96)."""
from __future__ import annotations

from typing import Any

import pytest

from brain.people import _doc_participant_keys

AVERY = "avery@example.com"


def test_calendar_participants_become_lowercased_keys() -> None:
    metadata = {"participants": ["Avery@Example.com ", "blake@example.com"]}

    keys = _doc_participant_keys(source_kind="calendar", metadata=metadata)

    assert keys == {AVERY, "blake@example.com"}


@pytest.mark.parametrize("participants", [None, AVERY, {"a": AVERY}, 42])
def test_calendar_participants_that_are_not_a_list_give_no_keys(participants: Any) -> None:
    metadata = {"participants": participants}

    assert _doc_participant_keys(source_kind="calendar", metadata=metadata) == set()


def test_calendar_entries_that_are_not_addresses_are_ignored() -> None:
    metadata = {"participants": [AVERY, 42, None, "Avery Sample", "", {"email": AVERY}]}

    assert _doc_participant_keys(source_kind="calendar", metadata=metadata) == {AVERY}


def test_other_kinds_ignore_participants() -> None:
    assert _doc_participant_keys(source_kind="slack", metadata={"participants": [AVERY]}) == set()
