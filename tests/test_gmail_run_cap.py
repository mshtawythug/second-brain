"""BL-39: every ``gws gmail`` call has a timeout, a stdout cap and a class-only error.

Plan: ``docs/plans/2026-11-07-bl-39-gmail-run-cap.md``. Every test drives the production path
(no ``runner``) through ``list_messages`` / ``read_message`` against a throwaway ``gws`` script
(``tests/gws_stub_fixtures.py``), never the installed CLI. No database: marked ``nodb``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from brain.ingest import gmail
from brain.ingest.gmail import GmailError
from tests.gws_stub_fixtures import (
    FAILING,
    FAST_SECONDS,
    FLOOD,
    HUNG,
    SYNTH_ADDRESS,
    SYNTH_STDERR,
    answering,
    gws_on_path,
)

pytestmark = pytest.mark.nodb

LIST_JSON = '{"messages":[{"id":"m-synth-1","threadId":"t-synth-1"}]}'
GET_JSON = '{"id":"m-synth-1","threadId":"t-synth-1"}'


def _lower(monkeypatch: pytest.MonkeyPatch, *, timeout: float | None = None,
           cap: int | None = None) -> None:
    # ``raising=False`` so the two regression tests also run against master, which has neither
    # constant, and fail there by the stub's bound (rule 12's red run). T9 pins that they exist.
    if timeout is not None:
        monkeypatch.setattr(gmail, "GWS_TIMEOUT_SECONDS", timeout, raising=False)
    if cap is not None:
        monkeypatch.setattr(gmail, "GWS_MAX_STDOUT_BYTES", cap, raising=False)


def test_a_hung_gws_read_is_killed_at_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1, the original bug: a ``gws`` that never answers blocked ``ingest-gmail`` forever."""
    gws_on_path(tmp_path, monkeypatch, HUNG)
    _lower(monkeypatch, timeout=0.3)
    started = time.monotonic()

    with pytest.raises(GmailError) as info:
        gmail.read_message("m-synth-1")

    assert time.monotonic() - started < FAST_SECONDS
    assert str(info.value) == "`gws gmail users messages get` timed out after 0.3s"
    assert isinstance(info.value, gmail.GmailTimeoutError)


def test_a_gws_answer_over_the_cap_is_refused_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2, the unbounded read: the whole answer was held in memory before anything checked it."""
    gws_on_path(tmp_path, monkeypatch, FLOOD)
    _lower(monkeypatch, cap=1000)
    started = time.monotonic()

    with pytest.raises(GmailError) as info:
        gmail.read_message("m-synth-1")

    assert time.monotonic() - started < FAST_SECONDS
    assert str(info.value) == "`gws gmail users messages get` stdout over 1000 bytes"
    assert not isinstance(info.value, gmail.GmailTimeoutError)


def test_an_ordinary_list_and_get_return_their_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3: at the production limits, a prompt answer comes back unchanged."""
    gws_on_path(tmp_path, monkeypatch, answering(LIST_JSON, GET_JSON))

    stubs = gmail.list_messages(label="test-label")
    message = gmail.read_message("m-synth-1")

    assert stubs == [{"id": "m-synth-1", "threadId": "t-synth-1"}]
    assert message == json.loads(GET_JSON)


def test_an_answer_exactly_at_the_cap_is_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T4: the cap refuses what is over it, not what reaches it."""
    gws_on_path(tmp_path, monkeypatch, answering(LIST_JSON, GET_JSON))
    _lower(monkeypatch, cap=len(GET_JSON))

    assert gmail.read_message("m-synth-1") == json.loads(GET_JSON)


def test_a_failing_gws_is_reported_by_exit_code_never_stderr_or_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5: master's message quoted the argv (the query, an address) and ``gws``'s stderr."""
    gws_on_path(tmp_path, monkeypatch, FAILING)

    with pytest.raises(GmailError) as info:
        gmail.list_messages(from_addr=SYNTH_ADDRESS)

    message = str(info.value)
    assert message == "`gws gmail users messages list` exit 3"
    assert SYNTH_STDERR not in message and SYNTH_ADDRESS not in message
    assert not isinstance(info.value, gmail.GmailTimeoutError)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_a_timeout_message_never_carries_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6: no part of the ``--params`` reaches the message (the runner's no longer holds it)."""
    gws_on_path(tmp_path, monkeypatch, HUNG)
    _lower(monkeypatch, timeout=0.3)

    with pytest.raises(gmail.GmailTimeoutError) as info:
        gmail.list_messages(from_addr=SYNTH_ADDRESS)

    assert SYNTH_ADDRESS not in str(info.value) and "--params" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_a_missing_gws_is_a_gmail_error_naming_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T7."""
    gws_on_path(tmp_path, monkeypatch, None)

    with pytest.raises(GmailError) as info:
        gmail.list_messages(label="test-label")

    assert str(info.value) == "`gws gmail users messages list` gws CLI not found on PATH"


def test_an_injected_runner_still_bypasses_the_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T8: the test seam every CLI test relies on is unchanged (no ``gws`` exists here)."""
    gws_on_path(tmp_path, monkeypatch, None)

    stubs = gmail.list_messages(label="test-label", runner=lambda _cmd: LIST_JSON)

    assert stubs == [{"id": "m-synth-1", "threadId": "t-synth-1"}]


def test_gmail_gws_limits_are_pinned() -> None:
    """T9: the values the BL-39 ruling set (60 s; 48 MiB, the reason beside the constant)."""
    assert gmail.GWS_TIMEOUT_SECONDS == 60.0
    assert gmail.GWS_MAX_STDOUT_BYTES == 48 * 1024 * 1024
