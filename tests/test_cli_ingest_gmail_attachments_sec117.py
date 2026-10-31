"""sec-117 regressions for `brain ingest-gmail --with-attachments` (RM-45, PR #117).

Same harness as ``test_cli_ingest_gmail_attachments.py``: the 5434 test database,
``FakeGws`` for every ``gws`` call, synthetic receipts only.
"""
from __future__ import annotations

import functools
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import cli_gmail_attachments as cga
from brain.ingest.gmail_attachments import AttachmentLimits
from tests.gmail_attachment_fixtures import (
    MERCHANT,
    FakeGws,
    bogus_filter_pdf,
    message_with_parts,
    receipt_pdf,
    shared_stream_pdf,
)
from tests.test_cli_ingest_gmail_attachments import (
    _gmail,
    _pdf,
    _receipts,
    _serve,
    vault,  # noqa: F401 — the fixture
)

VICTIM_TID = "18c0ffee0000d000"
VICTIM_MID = "18c0ffee0000d001"
HOSTILE_TID = "18c0ffee0000e000"
HOSTILE_MID = "18c0ffee0000e001"


def _two_threads(
    hostile_parts: list[dict[str, Any]], hostile: dict[str, bytes], victim: bytes
) -> FakeGws:
    """A hostile thread listed first (Gmail lists newest first), then a victim."""
    first = message_with_parts(HOSTILE_MID, HOSTILE_TID, hostile_parts)
    second = message_with_parts(VICTIM_MID, VICTIM_TID, [_pdf("1", attachment_id="victim")])
    attachments: dict[tuple[str, str], bytes | str] = {
        (HOSTILE_MID, aid): data for aid, data in hostile.items()
    }
    attachments[(VICTIM_MID, "victim")] = victim
    return FakeGws(messages={HOSTILE_MID: first, VICTIM_MID: second}, attachments=attachments)


def _doc(conn: psycopg.Connection, tid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT content, metadata FROM documents WHERE thread_id = %s", (tid,)
    ).fetchone()
    assert row is not None, tid
    return {"content": row[0], "statuses": [r["status"] for r in row[1]["attachments"]]}


def _limit(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    monkeypatch.setattr(cga, "AttachmentLimits", functools.partial(AttachmentLimits, **overrides))


# --- F1: an unlisted exception from a hostile PDF -------------------------------


def test_a_pdf_that_breaks_pdfplumber_does_not_stop_the_run(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection  # noqa: F811
) -> None:
    gws = _serve(monkeypatch, _two_threads(
        [_pdf("1", attachment_id="bogus")], {"bogus": bogus_filter_pdf()}, receipt_pdf()
    ))

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert _doc(test_db, HOSTILE_TID)["statuses"] == ["unreadable"]
    victim = _doc(test_db, VICTIM_TID)
    assert victim["statuses"] == ["extracted"] and MERCHANT in victim["content"]
    assert "2 ingested, 0 skipped (unchanged), 0 failed" in result.output
    assert len(gws.attachment_calls()) == 2


# --- F3: a parse that has no bound in-process -----------------------------------


@pytest.mark.timeout(120)
def test_a_pdf_past_the_worker_cpu_limit_is_unreadable_and_the_run_goes_on(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection  # noqa: F811
) -> None:
    from brain.ingest.attachment_isolation import WorkerLimits

    monkeypatch.setattr("brain.ingest.attachment_isolation.WORKER_LIMITS",
                        WorkerLimits(timeout_s=30.0, cpu_s=1))
    _serve(monkeypatch, _two_threads(
        [_pdf("1", attachment_id="shared")], {"shared": shared_stream_pdf()}, receipt_pdf()
    ))
    started = time.monotonic()

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert time.monotonic() - started < 30
    assert _doc(test_db, HOSTILE_TID)["statuses"] == ["unreadable"]
    assert _doc(test_db, VICTIM_TID)["statuses"] == ["extracted"]


# --- F2: progress, no starvation, no lost text -----------------------------------


def test_a_thread_over_the_run_cap_makes_progress_and_later_threads_are_reached(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection  # noqa: F811
) -> None:
    _limit(monkeypatch, max_per_run=3)
    parts = [_pdf(str(i), attachment_id=f"h{i}") for i in range(1, 5)]
    gws = _serve(monkeypatch, _two_threads(
        parts, {f"h{i}": receipt_pdf([f"Example Store {i}"]) for i in range(1, 5)},
        receipt_pdf(),
    ))

    first = _receipts()
    assert "attachments: 3 extracted, 2 over cap, 0 failed" in first.output
    assert len(gws.attachment_calls()) == 3

    second = _receipts()
    assert len(gws.attachment_calls()) == 5  # the 4th hostile part and the victim, only
    assert _doc(test_db, HOSTILE_TID)["statuses"] == ["extracted"] * 4
    hostile = _doc(test_db, HOSTILE_TID)["content"]
    assert all(f"Example Store {i}" in hostile for i in range(1, 5))
    assert _doc(test_db, VICTIM_TID)["statuses"] == ["extracted"]
    assert "3 kept" in second.output

    third = _receipts()
    assert len(gws.attachment_calls()) == 5
    assert third.output.count("(unchanged)") >= 2


def test_thread_truncation_is_final_and_its_text_is_stored(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection  # noqa: F811
) -> None:
    _limit(monkeypatch, max_thread_chars=120)
    long_text = [f"Example line {n} of a long statement" for n in range(20)]
    parts = [_pdf(str(i), attachment_id=f"t{i}") for i in range(1, 4)]
    gws = _serve(monkeypatch, _two_threads(
        parts, {f"t{i}": receipt_pdf(long_text) for i in range(1, 4)}, receipt_pdf()
    ))
    assert _receipts().exit_code == 0
    stored = _doc(test_db, HOSTILE_TID)
    assert stored["statuses"] == ["extracted", "truncated", "truncated"]
    assert "[attachment text truncated]" in stored["content"]
    calls = len(gws.attachment_calls())

    result = _receipts()

    # Skipped before any planning, not rebuilt and found unchanged: nothing is kept,
    # carried or counted (a non-final 'truncated' would re-plan the thread every run).
    assert f"skipped thread {HOSTILE_TID} (unchanged)" in result.output
    assert "attachments: 0 extracted, 0 failed" in result.output
    assert len(gws.attachment_calls()) == calls


def test_a_flagged_run_keeps_a_section_it_stored_before(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection  # noqa: F811
) -> None:
    gws = _serve(monkeypatch, _two_threads(
        [_pdf("1", attachment_id="kept")], {"kept": receipt_pdf(["Example Kept Receipt"])},
        receipt_pdf(),
    ))
    assert _receipts().exit_code == 0
    reply = message_with_parts("18c0ffee0000e002", HOSTILE_TID, [_pdf("1", attachment_id="new")],
                               internal_date="1767312000000", text="A second order.")
    gws.messages[reply["id"]] = reply
    gws.attachments[(reply["id"], "new")] = receipt_pdf(["Example New Receipt"])
    _limit(monkeypatch, max_per_run=0)

    result = _receipts()

    assert result.exit_code == 0, result.output
    stored = _doc(test_db, HOSTILE_TID)
    assert "Example Kept Receipt" in stored["content"]
    assert "A second order." in stored["content"]
    assert stored["statuses"] == ["extracted", "over cap"]


def test_a_section_the_guard_rewrote_is_re_extracted_not_dropped(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection  # noqa: F811
) -> None:
    """A stored section that no longer matches its hash (the secret guard redacted it) is
    re-extracted; when the budget runs out first, the stored body is kept as it is."""
    from tests.secret_fixtures import SYNTHETIC_POSITIVES

    monkeypatch.setenv("BRAIN_SECRET_GUARD", "redact")
    token = SYNTHETIC_POSITIVES["github_pat"]
    gws = _serve(monkeypatch, _two_threads(
        [_pdf("1", attachment_id="secret")], {"secret": receipt_pdf([MERCHANT, token])},
        receipt_pdf(),
    ))
    assert _receipts().exit_code == 0
    before = _doc(test_db, HOSTILE_TID)["content"]
    reply = message_with_parts("18c0ffee0000e003", HOSTILE_TID, [],
                               internal_date="1767312000000", text="A follow-up.")
    gws.messages[reply["id"]] = reply
    _limit(monkeypatch, max_per_run=0)

    result = _receipts()

    assert f"kept thread {HOSTILE_TID}" in result.output
    assert _doc(test_db, HOSTILE_TID)["content"] == before


def test_a_thread_past_the_record_cap_stores_at_most_that_many_records(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection  # noqa: F811
) -> None:
    """I3: one record per candidate had no cap per thread."""
    _limit(monkeypatch, max_per_run=0, max_candidates_per_thread=5)
    parts = [_pdf(str(i), attachment_id=f"c{i}") for i in range(1, 9)]
    _serve(monkeypatch, _two_threads(parts, {}, receipt_pdf()))

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert len(_doc(test_db, HOSTILE_TID)["statuses"]) == 5
    assert "3 too many" in result.output


# --- F5: SIGTERM and SIGHUP remove the private directory -------------------------


class _UnhandledSignalError(Exception):
    pass


def _fallback(signum: int, frame: object) -> None:
    raise _UnhandledSignalError(signum)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_inside_the_session_removes_the_work_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: int
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    previous = signal.signal(signum, _fallback)  # what an unguarded session would reach
    try:
        attachments = cga.AttachmentPass(True, vault_root=tmp_path)
        with pytest.raises(SystemExit), attachments.session():
            assert [p.name for p in tmp_path.iterdir() if p.name.startswith("brain-gmail-att-")]
            os.kill(os.getpid(), signum)
            time.sleep(1)
    finally:
        signal.signal(signum, previous)

    assert [p for p in tmp_path.iterdir() if p.name.startswith("brain-gmail-att-")] == []
    assert signal.getsignal(signum) is previous


def test_without_the_flag_no_signal_handler_is_installed(tmp_path: Path) -> None:
    before = signal.getsignal(signal.SIGTERM)

    with cga.AttachmentPass(False, vault_root=tmp_path).session():
        assert signal.getsignal(signal.SIGTERM) is before


def test_a_plain_run_still_works(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _serve(monkeypatch, _two_threads([], {}, receipt_pdf()))

    assert _gmail("--label", "receipts").exit_code == 0
