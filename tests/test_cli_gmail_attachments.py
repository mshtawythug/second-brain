"""Unit tests for ``brain.cli_gmail_attachments`` edges the CLI tests do not reach (RM-45)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import cli_gmail_attachments as cga
from brain.ingest import IngestResult
from brain.ingest.gmail_attachments import AttachmentLimits, gws_stdout_cap
from tests.gmail_attachment_fixtures import message_with_parts

TID = "18c0ffee0000c000"


def test_the_production_runner_passes_the_timeout_and_the_stdout_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def spy(args: list[str], **kwargs: Any) -> str:
        seen["args"], seen["kwargs"] = args, kwargs
        return "{}"

    monkeypatch.setattr(cga, "real_gws_runner", spy)

    assert cga.gws_runner(["gws", "x"]) == "{}"
    assert seen == {
        "args": ["gws", "x"],
        "kwargs": {
            "timeout": cga.GWS_TIMEOUT_SECONDS,
            "max_stdout_bytes": gws_stdout_cap(AttachmentLimits()),
        },
    }


@pytest.mark.parametrize("records", ["x", None, 7, [{"status": "extracted"}, "junk"]])
def test_stored_attachment_records_are_read_defensively(records: Any) -> None:
    holds = cga._holds_receipt_text({"attachments": records})

    assert holds is (isinstance(records, list))


def test_unchanged_needs_the_fingerprint_and_final_statuses() -> None:
    final = {"attachments_fingerprint": "f", "attachments": [{"status": "extracted"}]}
    retry = {"attachments_fingerprint": "f", "attachments": [{"status": "failed"}]}

    assert cga._unchanged(final, "f")
    assert not cga._unchanged(final, "g")
    assert not cga._unchanged(retry, "f")


def test_without_the_flag_the_pass_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    attachments = cga.AttachmentPass(False, vault_root=Path("/nonexistent"))

    attachments.dry_run_note()
    attachments.report()
    with attachments.session():
        pass

    assert capsys.readouterr().out == ""
    assert attachments.sensitivity == "normal"


def test_augment_outside_a_session_is_a_programming_error(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    attachments = cga.AttachmentPass(True, vault_root=tmp_path)

    with pytest.raises(RuntimeError, match="outside session"):
        attachments.augment(test_db, TID, [message_with_parts("m1", TID, [])])


def test_after_ingest_without_a_document_id_does_nothing(
    test_db: psycopg.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    attachments = cga.AttachmentPass(True, vault_root=tmp_path)

    attachments.after_ingest(test_db, TID, IngestResult(document_id=None, created=False))

    assert capsys.readouterr().out == ""


MISSING_ID = "00000000-0000-4000-8000-000000000001"
OTHER_ID = "00000000-0000-4000-8000-000000000002"


@pytest.mark.parametrize(
    "pending",
    [
        ("another-thread", (MISSING_ID, {}), {"attachments": []}),
        (TID, None, {"attachments": []}),
        (TID, (OTHER_ID, {}), {"attachments": []}),
    ],
)
def test_after_ingest_patches_only_the_thread_it_augmented(
    test_db: psycopg.Connection, tmp_path: Path, pending: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched: list[Any] = []
    monkeypatch.setattr(cga, "refresh_volatile", lambda *a, **k: patched.append(k))
    attachments = cga.AttachmentPass(True, vault_root=tmp_path)
    attachments._pending = pending

    attachments.after_ingest(test_db, TID, IngestResult(document_id=MISSING_ID, created=False))

    assert patched == []
    assert attachments._pending is None
