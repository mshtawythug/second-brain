"""BL-39: ``ingest-gmail`` stops after three ``gws`` timeouts in a row, and only then.

Pure: no database, no process (marked ``nodb``). The CLI wiring is pinned end to end in
``tests/test_cli_ingest_gmail_bl39.py``.
"""
from __future__ import annotations

import pytest

from brain.ingest.gmail import GmailError, GmailTimeoutError
from brain.ingest.gmail_timeouts import (
    MAX_CONSECUTIVE_TIMEOUTS,
    STOP_MESSAGE,
    ConsecutiveTimeouts,
)

pytestmark = pytest.mark.nodb

TIMEOUT = GmailTimeoutError("`gws gmail users messages get` timed out after 60s")
FAILURE = GmailError("`gws gmail users messages get` exit 3")


def _feed(events: list[str]) -> ConsecutiveTimeouts:
    """``T`` a timeout, ``E`` another failure, ``R`` a read that returned."""
    timeouts = ConsecutiveTimeouts()
    for event in events:
        if event == "R":
            timeouts.read_returned()
        else:
            timeouts.failed(TIMEOUT if event == "T" else FAILURE)
    return timeouts


def test_three_timeouts_in_a_row_stop_the_run() -> None:
    timeouts = ConsecutiveTimeouts()

    answers = [timeouts.failed(TIMEOUT) for _ in range(3)]

    assert answers == [False, False, True]
    assert timeouts.tripped


def test_timeouts_broken_up_by_returned_reads_keep_going() -> None:
    assert not _feed(["T", "T", "R", "T", "T", "R", "T", "T"]).tripped


def test_timeouts_broken_up_by_other_failures_keep_going() -> None:
    assert not _feed(["T", "T", "E", "T", "T"]).tripped


def test_other_failures_alone_never_stop_the_run() -> None:
    assert not _feed(["E"] * 10).tripped


def test_the_stop_rule_is_three_and_its_message_is_class_only() -> None:
    assert MAX_CONSECUTIVE_TIMEOUTS == 3
    assert STOP_MESSAGE == (
        "ingest-gmail: stopped after 3 gws calls in a row timed out; check gws, then re-run"
    )
