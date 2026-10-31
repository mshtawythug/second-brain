"""`brain ingest-gmail --with-attachments` end to end against the test database (RM-45).

The ``gws`` CLI is never run: :class:`FakeGws` replaces both the message reader
(``brain.ingest.gmail._run``) and the attachment runner
(``brain.cli_gmail_attachments.gws_runner``). Receipts are synthetic.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import psycopg
import pytest
from click.testing import Result
from typer.testing import CliRunner

from brain.cli import app
from brain.errors import DirectoryRefreshError
from brain.ingest.attachment_text import REDACTED_CARD
from tests.conftest import TEST_DATABASE_URL
from tests.gmail_attachment_fixtures import (
    MERCHANT,
    FakeGws,
    attachment_part,
    message_with_parts,
    receipt_pdf,
    receipt_png,
)
from tests.secret_fixtures import SYNTHETIC_POSITIVES

TID = "18c0ffee0000b000"
MID = "18c0ffee0000b001"
AID = "ANGjdJ_fake-receipt-id_0001"
PDF_NAME = "Receipt-0001.pdf"
PDF_MIME = "application/pdf"


@pytest.fixture
def vault(
    monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection, fake_embedder: object,
    tmp_path: Path,
) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(root))
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return root


def _serve(monkeypatch: pytest.MonkeyPatch, gws: FakeGws) -> FakeGws:
    monkeypatch.setattr("brain.ingest.gmail._run", lambda cmd, runner=None: gws(cmd))
    monkeypatch.setattr("brain.cli_gmail_attachments.gws_runner", gws)
    return gws


def _gmail(*args: str) -> Result:
    return CliRunner().invoke(app, ["ingest-gmail", *args])


def _receipts(*args: str) -> Result:
    return _gmail("--label", "receipts", "--with-attachments", *args)


def _pdf(part_id: str = "1", attachment_id: str = AID, **kw: Any) -> dict[str, Any]:
    kw.setdefault("size", 4096)
    return attachment_part(part_id, PDF_NAME, PDF_MIME, attachment_id=attachment_id, **kw)


def _thread_gws(parts: list[dict[str, Any]], **attachments: bytes) -> FakeGws:
    message = message_with_parts(MID, TID, parts)
    return FakeGws(
        messages={MID: message},
        attachments={(MID, aid): data for aid, data in attachments.items()},
    )


def _row(conn: psycopg.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id::text, content, sensitivity, metadata, vault_path FROM documents "
        "WHERE thread_id = %s",
        (TID,),
    ).fetchone()
    assert row is not None
    return {"id": row[0], "content": row[1], "sensitivity": row[2], "metadata": row[3],
            "vault_path": row[4]}


# --- acceptance 1: the section ------------------------------------------------


def test_a_pdf_attachment_becomes_a_searchable_section(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    gws = _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf()}))

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert f"ingested thread {TID}" in result.output
    assert "attachments: 1 extracted, 0 failed" in result.output
    row = _row(test_db)
    assert f"## Attachment: {PDF_NAME}" in row["content"]
    assert MERCHANT in row["content"] and REDACTED_CARD in row["content"]
    assert "4111" not in row["content"]
    hits = test_db.execute(
        "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE d.thread_id = %s AND c.tsv @@ plainto_tsquery('english', 'Exampleville')",
        (TID,),
    ).fetchone()
    assert hits is not None and hits[0] >= 1
    assert row["metadata"]["attachments"] == [{
        "message_id": MID, "part_id": "1", "status": "extracted", "type": "pdf",
        "bytes": len(receipt_pdf()), "pages": 1,
    }]
    assert len(gws.attachment_calls()) == 1


# --- acceptance 2: the size cap -----------------------------------------------


def test_an_attachment_over_ten_mib_is_skipped_unfetched_and_counted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gws = _serve(monkeypatch, _thread_gws([_pdf(size=11 * 1024 * 1024)]))

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert f"attachment 1 of thread {TID}: too large" in result.output
    assert "1 too large" in result.output
    assert gws.attachment_calls() == []


# --- acceptance 3: the scope refusal ------------------------------------------


@pytest.mark.parametrize("scope", [["--since", "2026/01/01"], ["--from", "shop@example.com"]])
def test_with_attachments_needs_a_label_or_a_query(
    vault: Path, monkeypatch: pytest.MonkeyPatch, scope: list[str]
) -> None:
    gws = _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf()}))

    result = _gmail(*scope, "--with-attachments")

    assert result.exit_code == 2
    assert "--with-attachments needs --label or --query" in result.output
    assert gws.calls == []


def test_with_attachments_accepts_a_query(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gws = _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf()}))

    result = _gmail("--query", "has:attachment receipt", "--with-attachments")

    assert result.exit_code == 0, result.output
    assert len(gws.attachment_calls()) == 1


# --- acceptance 4: confidential -----------------------------------------------


def test_a_new_thread_is_stored_confidential(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf()}))

    assert _receipts().exit_code == 0

    assert _row(test_db)["sensitivity"] == "confidential"


def test_a_thread_without_attachments_is_still_stored_confidential(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    _serve(monkeypatch, _thread_gws([]))

    assert _receipts().exit_code == 0

    assert _row(test_db)["sensitivity"] == "confidential"


def test_a_stored_normal_thread_gaining_a_section_is_escalated(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf()}))
    assert _gmail("--label", "receipts").exit_code == 0
    assert _row(test_db)["sensitivity"] == "normal"

    result = _receipts()

    assert result.exit_code == 0, result.output
    row = _row(test_db)
    assert row["sensitivity"] == "confidential"
    assert "## Attachment:" in row["content"]


def test_an_unchanged_stored_normal_thread_and_its_mirror_are_escalated(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    _serve(monkeypatch, _thread_gws([]))
    assert _gmail("--label", "receipts").exit_code == 0
    assert _row(test_db)["sensitivity"] == "normal"

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert f"marked confidential thread {TID}" in result.output
    row = _row(test_db)
    assert row["sensitivity"] == "confidential"
    (mirror,) = vault.rglob("*.md")
    assert "sensitivity: confidential" in mirror.read_text(encoding="utf-8")


def test_a_plain_run_never_lowers_a_confidential_thread(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    gws = _serve(monkeypatch, _thread_gws([]))
    assert _receipts().exit_code == 0
    gws.messages[MID]["payload"]["parts"][0]["body"]["data"] = "TmV3IHRleHQ"  # "New text"

    assert _gmail("--label", "receipts").exit_code == 0

    assert _row(test_db)["sensitivity"] == "confidential"


# --- acceptance 5: the run cap ------------------------------------------------


def test_at_most_fifty_attachments_are_fetched_per_run(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = receipt_pdf()
    parts = [_pdf(str(i + 1), attachment_id=f"{AID}-{i}") for i in range(51)]
    gws = _serve(monkeypatch, _thread_gws(parts, **{f"{AID}-{i}": pdf for i in range(51)}))

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert "attachments: 50 extracted, 1 over cap, 0 failed" in result.output
    assert len(gws.attachment_calls()) == 50


# --- re-runs ------------------------------------------------------------------


def test_an_identical_re_run_downloads_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    gws = _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf()}))
    assert _receipts().exit_code == 0
    before = _row(test_db)

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert f"skipped thread {TID} (unchanged)" in result.output
    assert "0 ingested, 1 skipped (unchanged), 0 failed" in result.output
    assert len(gws.attachment_calls()) == 1
    assert _row(test_db)["content"] == before["content"]


def test_a_retryable_status_is_fetched_again(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    second = f"{AID}-2"
    gws = _thread_gws([_pdf("1"), _pdf("2", attachment_id=second)], **{AID: receipt_pdf()})

    def flaky(argv: list[str]) -> str:
        if second in argv[7]:
            raise DirectoryRefreshError("gws command failed (exit 1): x", log_detail="exit 1")
        return gws(argv)

    _serve(monkeypatch, gws)
    monkeypatch.setattr("brain.cli_gmail_attachments.gws_runner", flaky)
    first = _receipts()
    assert f"attachment 2 of thread {TID}: failed" in first.output
    gws.attachments[(MID, second)] = receipt_pdf(["Example Cafe", "Total 3.00"])
    monkeypatch.setattr("brain.cli_gmail_attachments.gws_runner", gws)

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert "attachments: 2 extracted, 0 failed" in result.output
    assert "Example Cafe" in _row(test_db)["content"]


def test_a_plain_run_skips_a_thread_holding_receipt_text(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    gws = _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf()}))
    assert _receipts().exit_code == 0
    gws.messages[MID]["payload"]["parts"][0]["body"]["data"] = "TmV3IHRleHQ"
    before = _row(test_db)["content"]

    result = _gmail("--label", "receipts")

    assert result.exit_code == 0, result.output
    assert (
        f"skipped thread {TID} (has attachments: re-run with --with-attachments)"
        in result.output
    )
    assert _row(test_db)["content"] == before


def test_a_plain_run_rebuilds_a_thread_whose_attachments_gave_no_text(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    gws = _serve(monkeypatch, _thread_gws([_pdf(), _pdf("2", attachment_id="other")],
                                          **{AID: receipt_pdf(), "other": b"<html>"}))
    assert _receipts().exit_code == 0
    stored = _row(test_db)["metadata"]["attachments"]
    assert [r["status"] for r in stored] == ["extracted", "unsupported"]
    gws.messages[MID]["payload"]["parts"] = gws.messages[MID]["payload"]["parts"][:1]
    gws.messages[MID]["payload"]["parts"][0]["body"]["data"] = "TmV3IHRleHQ"
    test_db.execute(
        "UPDATE documents SET metadata = jsonb_set(metadata, '{attachments,0,status}', "
        "'\"empty\"') WHERE thread_id = %s",
        (TID,),
    )
    test_db.commit()

    result = _gmail("--label", "receipts")

    assert f"ingested thread {TID}" in result.output
    assert "New text" in _row(test_db)["content"]


# --- privacy ------------------------------------------------------------------


def test_the_filename_reaches_neither_metadata_nor_the_terminal(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    name = "Receipt-UniqueMarkerName-0001.pdf"
    parts = [attachment_part("1", name, PDF_MIME, size=10, attachment_id=AID),
             attachment_part("2", name, PDF_MIME, size=11 * 1024 * 1024, attachment_id=AID)]
    _serve(monkeypatch, _thread_gws(parts, **{AID: receipt_pdf()}))

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert "UniqueMarker" not in result.output
    row = _row(test_db)
    assert "UniqueMarker" not in json.dumps(row["metadata"])
    assert f"## Attachment: {name}" in row["content"]


def test_the_secret_guard_redacts_attachment_text(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    token = SYNTHETIC_POSITIVES["github_pat"]
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "redact")
    _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf([MERCHANT, token])}))

    result = _receipts()

    assert result.exit_code == 0, result.output
    content = _row(test_db)["content"]
    assert token not in content and "[REDACTED:github_pat]" in content


def test_the_secret_guard_rejects_a_thread_whose_attachment_holds_a_secret(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    token = SYNTHETIC_POSITIVES["github_pat"]
    _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf([MERCHANT, token])}))

    result = _receipts()

    assert f"refused thread {TID}" in result.output
    assert "0 ingested, 0 skipped (unchanged), 1 failed" in result.output
    count = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert count is not None and count[0] == 0


def test_a_draft_reply_contributes_no_attachment(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    sent = message_with_parts(MID, TID, [])
    draft = message_with_parts("18c0ffee0000b002", TID, [_pdf()], labels=["DRAFT"],
                               internal_date="1767312000000")
    gws = _serve(monkeypatch, FakeGws(messages={MID: sent, draft["id"]: draft},
                                      attachments={(draft["id"], AID): receipt_pdf()}))

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert gws.attachment_calls() == []
    assert "## Attachment:" not in _row(test_db)["content"]


def test_an_image_without_ocr_is_no_ocr_with_the_hint_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = [attachment_part(str(i), f"photo{i}.png", "image/png", size=100,
                             inline=receipt_png()) for i in (1, 2)]
    _serve(monkeypatch, _thread_gws(parts))

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert f"attachment 1 of thread {TID}: no ocr" in result.output
    assert "2 no ocr" in result.output
    assert result.output.count("OCR is off") == 1


def test_dry_run_fetches_no_attachment(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    gws = _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf()}))

    result = _receipts("--dry-run")

    assert result.exit_code == 0, result.output
    assert "--with-attachments: --dry-run fetches no attachments" in result.output
    assert gws.attachment_calls() == []
    count = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert count is not None and count[0] == 0


def test_a_malformed_part_is_counted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _pdf()
    broken["body"]["size"] = "big"
    _serve(monkeypatch, _thread_gws([broken]))

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert "attachments: 0 extracted, 1 malformed, 0 failed" in result.output


def test_without_the_flag_there_is_no_attachment_line(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gws = _serve(monkeypatch, _thread_gws([_pdf()], **{AID: receipt_pdf()}))

    result = _gmail("--label", "receipts")

    assert result.exit_code == 0, result.output
    assert "attachments:" not in result.output
    assert gws.attachment_calls() == []


def test_a_broken_pdf_is_not_fetched_again_even_when_it_adds_no_text(
    vault: Path, monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection
) -> None:
    """Regression (RM-45 e2e): a thread whose attachments gave no text kept its body, so
    ``ingest_document`` skipped it and the attachment records were never stored; every run
    downloaded them again. The records are now patched in metadata only, and ``unreadable``
    is final."""
    gws = _serve(monkeypatch, _thread_gws([_pdf(attachment_id="broken")],
                                          **{"broken": b"%PDF-1.7\nbroken"}))
    assert _gmail("--label", "receipts").exit_code == 0
    assert "1 unreadable" in _receipts().output
    assert [r["status"] for r in _row(test_db)["metadata"]["attachments"]] == ["unreadable"]

    result = _receipts()

    assert result.exit_code == 0, result.output
    assert f"skipped thread {TID} (unchanged)" in result.output
    assert len(gws.attachment_calls()) == 1


def test_a_re_run_retries_a_no_ocr_image(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = attachment_part("1", "photo.png", "image/png", size=100, attachment_id="image")
    gws = _serve(monkeypatch, _thread_gws([image], image=receipt_png()))
    assert "1 no ocr" in _receipts().output

    result = _receipts()

    assert "1 no ocr" in result.output
    assert len(gws.attachment_calls()) == 2
