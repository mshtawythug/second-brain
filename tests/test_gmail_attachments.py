"""Tests for ``brain.ingest.gmail_attachments`` — the hostile-input reader (RM-45).

Unit tests: no database, no real ``gws``. The runner is a :class:`FakeGws` or a
local function; PDFs come from reportlab, PNGs from Pillow, all synthetic.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from brain.errors import DirectoryRefreshError
from brain.ingest import ExtractedDoc
from brain.ingest import gmail_attachments as ga
from brain.ingest.attachment_text import REDACTED_CARD
from brain.ingest.dispatch import extract_path
from brain.ingest.image import OCR_MISSING_HINT, OcrUnavailableError
from tests.gmail_attachment_fixtures import (
    MERCHANT,
    FakeGws,
    attachment_part,
    b64url,
    message_with_parts,
    nested_multipart,
    receipt_pdf,
    receipt_png,
)

MID = "18c0ffee0000a001"
TID = "18c0ffee0000a000"
AID = "ANGjdJ_fake-attachment-id_0001"
PDF_MIME = "application/pdf"
BODY = "## 2026-01-01 00:00 — Example Hardware\n\nThanks for your order."
NO_EXTRACT_CALLS: list[Path] = []


def _limits(**overrides: Any) -> ga.AttachmentLimits:
    return ga.AttachmentLimits(**overrides)


def _pdf_part(part_id: str = "1", *, size: int = 2048, **kw: Any) -> dict[str, Any]:
    return attachment_part(part_id, "Receipt-0001.pdf", PDF_MIME, size=size, **kw)


def _plan(parts: list[dict[str, Any]], **limits: Any) -> ga.ThreadPlan:
    return ga.plan_thread([message_with_parts(MID, TID, parts)], BODY, limits=_limits(**limits))


def _never_extract(path: Path) -> ExtractedDoc:
    raise AssertionError(f"extract must not run: {path}")


def _run_one(
    tmp_path: Path,
    part: dict[str, Any],
    gws: FakeGws | None = None,
    *,
    extract: Any = extract_path,
    run: ga.AttachmentRun | None = None,
) -> tuple[ga.ThreadAttachments, ga.AttachmentRun]:
    run = run or ga.AttachmentRun(_limits())
    plan = _plan([part])
    out = ga.extract_attachments(
        plan.candidates, run=run, runner=gws or FakeGws(), workdir=tmp_path, extract=extract
    )
    return out, run


# --- the MIME walk -----------------------------------------------------------


def test_pdf_png_and_jpeg_parts_are_found_in_message_order() -> None:
    parts = [
        _pdf_part("1", attachment_id=AID),
        attachment_part("2", "photo.png", "image/png", size=10, attachment_id=AID),
        attachment_part("3", "scan.jpg", "image/jpeg", size=10, attachment_id=AID),
        attachment_part("4", "old.jpg", "image/jpg", size=10, attachment_id=AID),
    ]

    plan = _plan(parts)

    assert [c.part_id for c in plan.candidates] == ["1", "2", "3", "4"]
    first = plan.candidates[0]
    assert (first.message_id, first.filename, first.declared_size) == (
        MID,
        "Receipt-0001.pdf",
        2048,
    )
    assert first.attachment_id == AID and first.inline_data is None
    assert plan.malformed == 0


@pytest.mark.parametrize(
    ("filename", "mime", "found"),
    [
        ("receipt.PDF", "application/octet-stream", True),
        ("receipt.jpeg", "application/octet-stream", True),
        ("receipt.txt", "application/octet-stream", False),
        ("receipt.html", "text/html", False),
        ("", PDF_MIME, False),
        ("receipt.pdf", "APPLICATION/PDF", True),
    ],
)
def test_the_prefilter(filename: str, mime: str, found: bool) -> None:
    plan = _plan([attachment_part("1", filename, mime, size=1, attachment_id=AID)])

    assert bool(plan.candidates) is found


@pytest.mark.parametrize(
    ("disposition", "content_id"), [("inline", None), (None, "<logo@shop.example.com>")]
)
def test_an_inline_image_is_not_a_candidate(
    disposition: str | None, content_id: str | None
) -> None:
    logo = attachment_part(
        "1", "logo.png", "image/png", size=1, attachment_id=AID,
        disposition=disposition, content_id=content_id,
    )

    assert _plan([logo]).candidates == []


def test_an_image_whose_headers_are_not_a_list_is_still_a_candidate() -> None:
    part = attachment_part("1", "photo.png", "image/png", size=1, attachment_id=AID)
    part["headers"] = "junk"

    assert len(_plan([part]).candidates) == 1


def test_an_inline_pdf_is_still_a_candidate() -> None:
    plan = _plan([_pdf_part(attachment_id=AID, disposition="inline", content_id="<x>")])

    assert len(plan.candidates) == 1


def test_a_part_with_a_non_string_filename_or_mime_is_ignored() -> None:
    part = _pdf_part(attachment_id=AID)
    part["filename"] = ["Receipt.pdf"]
    other = _pdf_part("2", attachment_id=AID)
    other["mimeType"] = None
    other["filename"] = "receipt.bin"

    assert _plan([part, other]).candidates == []


def test_a_walk_deeper_than_the_cap_stops_and_counts_malformed() -> None:
    deep = nested_multipart(40, _pdf_part(attachment_id=AID))

    plan = _plan([deep], max_mime_depth=32)

    assert plan.candidates == []
    assert plan.malformed == 1


def test_a_walk_within_the_depth_cap_finds_the_leaf() -> None:
    plan = _plan([nested_multipart(10, _pdf_part(attachment_id=AID))])

    assert len(plan.candidates) == 1


def test_a_ten_thousand_level_tree_does_not_recurse() -> None:
    plan = _plan([nested_multipart(10_000, _pdf_part(attachment_id=AID))])

    assert plan.candidates == [] and plan.malformed == 1


def test_a_message_wider_than_the_part_cap_stops_and_counts_malformed() -> None:
    parts = [_pdf_part(str(i), attachment_id=AID) for i in range(1, 30)]

    plan = _plan(parts, max_parts_per_message=10)

    assert len(plan.candidates) < 10
    assert plan.malformed == 1


@pytest.mark.parametrize(
    "breakage",
    [
        lambda p: p.update(body={"size": True, "attachmentId": AID}),
        lambda p: p.update(body={"size": -1, "attachmentId": AID}),
        lambda p: p.update(body={"size": "12", "attachmentId": AID}),
        lambda p: p.update(body="not a mapping"),
        lambda p: p.update(partId="1; rm -rf"),
        lambda p: p.update(partId=""),
        lambda p: p.update(partId=7),
    ],
)
def test_a_malformed_attachment_part_is_skipped_and_counted(breakage: Any) -> None:
    part = _pdf_part(attachment_id=AID)
    breakage(part)

    plan = _plan([part])

    assert plan.candidates == []
    assert plan.malformed == 1


def test_non_mapping_nodes_and_non_list_parts_are_skipped() -> None:
    message = message_with_parts(MID, TID, [_pdf_part(attachment_id=AID)])
    message["payload"]["parts"].append("junk")
    message["payload"]["parts"].append({"partId": "5", "mimeType": "multipart/mixed",
                                        "parts": "junk"})

    plan = ga.plan_thread([message], BODY, limits=_limits())

    assert len(plan.candidates) == 1
    assert plan.malformed == 1


@pytest.mark.parametrize("message_id", [None, 7, "", "id with spaces", "x" * 1025])
def test_a_message_with_a_bad_id_contributes_nothing(message_id: Any) -> None:
    message = message_with_parts(MID, TID, [_pdf_part(attachment_id=AID)])
    message["id"] = message_id

    plan = ga.plan_thread([message], BODY, limits=_limits())

    assert plan.candidates == [] and plan.malformed == 1


def test_a_message_without_a_payload_mapping_contributes_nothing() -> None:
    plan = ga.plan_thread([{"id": MID, "payload": "x"}], BODY, limits=_limits())

    assert plan.candidates == [] and plan.malformed == 1


# --- the fingerprint ---------------------------------------------------------


def test_the_fingerprint_is_stable_and_moves_with_the_body_the_parts_and_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = [_pdf_part(attachment_id=AID)]
    base = _plan(parts).fingerprint

    assert _plan([_pdf_part(attachment_id="ANOTHER-unstable-id")]).fingerprint == base
    assert _plan([_pdf_part(attachment_id=AID, size=4096)]).fingerprint != base
    assert ga.plan_thread([message_with_parts(MID, TID, parts)], BODY + "!",
                          limits=_limits()).fingerprint != base
    monkeypatch.setattr(ga, "ATTACHMENT_POLICY_VERSION", "v-next")
    assert _plan(parts).fingerprint != base


# --- fetch and decode --------------------------------------------------------


def test_inline_data_is_decoded_without_a_gws_call(tmp_path: Path) -> None:
    gws = FakeGws()
    part = _pdf_part(inline=receipt_pdf())

    out, _ = _run_one(tmp_path, part, gws)

    assert out.records[0].status == "extracted"
    assert gws.calls == []


def test_the_fetch_argv(tmp_path: Path) -> None:
    gws = FakeGws(attachments={(MID, AID): receipt_pdf()})

    out, _ = _run_one(tmp_path, _pdf_part(attachment_id=AID), gws)

    assert gws.calls == [[
        "gws", "gmail", "users", "messages", "attachments", "get",
        "--params", json.dumps({"userId": "me", "messageId": MID, "id": AID}),
        "--format", "json",
    ]]
    assert out.records[0].status == "extracted"


@pytest.mark.parametrize("bad_id", ['a"b', "a b", "a\nb", "x" * 1025, ""])
def test_a_bad_attachment_id_is_malformed_and_never_reaches_gws(
    tmp_path: Path, bad_id: str
) -> None:
    gws = FakeGws()

    out, _ = _run_one(tmp_path, _pdf_part(attachment_id=bad_id), gws, extract=_never_extract)

    assert out.records[0].status == "malformed"
    assert gws.calls == []


def test_a_part_with_neither_data_nor_an_id_is_malformed(tmp_path: Path) -> None:
    out, _ = _run_one(tmp_path, _pdf_part(), extract=_never_extract)

    assert out.records[0].status == "malformed"


def test_a_gws_failure_is_failed(tmp_path: Path) -> None:
    def failing(argv: list[str]) -> str:
        raise DirectoryRefreshError("gws command failed (exit 2): account text",
                                    log_detail="exit 2")

    out, run = _run_one(tmp_path, _pdf_part(attachment_id=AID), failing,
                        extract=_never_extract)  # type: ignore[arg-type]

    assert out.records[0].status == "failed"
    assert run.counts["failed"] == 1


def test_an_os_error_starting_gws_is_failed(tmp_path: Path) -> None:
    def failing(argv: list[str]) -> str:
        raise PermissionError("denied")

    out, _ = _run_one(tmp_path, _pdf_part(attachment_id=AID), failing,
                      extract=_never_extract)  # type: ignore[arg-type]

    assert out.records[0].status == "failed"


def test_stdout_over_the_cap_is_too_large(tmp_path: Path) -> None:
    limits = _limits(max_bytes=30)
    oversized = json.dumps({"data": "A" * (ga.gws_stdout_cap(limits) + 1)})
    gws = FakeGws(attachments={(MID, AID): oversized})
    run = ga.AttachmentRun(limits)

    out, _ = _run_one(tmp_path, _pdf_part(attachment_id=AID, size=10), gws,
                      extract=_never_extract, run=run)

    assert out.records[0].status == "too large"


def test_the_stdout_cap_covers_the_base64_of_the_largest_attachment() -> None:
    limits = _limits()
    largest = json.dumps({"size": limits.max_bytes, "data": b64url(b"\xff" * limits.max_bytes)})

    assert len(largest) <= ga.gws_stdout_cap(limits)


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        "[" * 100_000 + "]" * 100_000,
        json.dumps(["data"]),
        json.dumps({"data": 7}),
        json.dumps({"size": 3}),
    ],
)
def test_a_malformed_gws_answer_is_malformed(tmp_path: Path, stdout: str) -> None:
    gws = FakeGws(attachments={(MID, AID): stdout})

    out, _ = _run_one(tmp_path, _pdf_part(attachment_id=AID), gws, extract=_never_extract)

    assert out.records[0].status == "malformed"


@pytest.mark.parametrize("data", ["a+b/", "ab cd", "A", "abc==="])
def test_bad_base64_is_malformed(data: str) -> None:
    with pytest.raises(ga.MalformedAttachmentError):
        ga.decode_base64url(data, max_bytes=100)


def test_base64_longer_than_the_cap_is_refused_before_decoding() -> None:
    with pytest.raises(ga.AttachmentTooLargeError):
        ga.decode_base64url("A" * 1_000, max_bytes=100)


def test_padded_and_unpadded_base64_decode() -> None:
    assert ga.decode_base64url("YWI=", max_bytes=10) == b"ab"
    assert ga.decode_base64url("YWI", max_bytes=10) == b"ab"


def test_a_small_declared_size_does_not_admit_a_large_body(tmp_path: Path) -> None:
    limits = _limits(max_bytes=1000)
    gws = FakeGws(attachments={(MID, AID): b"%PDF-" + b"0" * 1100})

    out, _ = _run_one(tmp_path, _pdf_part(attachment_id=AID, size=10), gws,
                      extract=_never_extract, run=ga.AttachmentRun(limits))

    assert out.records[0].status == "too large"


def test_the_decoded_size_is_checked_against_the_cap() -> None:
    data = b64url(b"x" * 101)  # 136 chars: within 4 * ceil(100 / 3) = 136, decodes to 101

    with pytest.raises(ga.AttachmentTooLargeError):
        ga.decode_base64url(data, max_bytes=100)


# --- sniffing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"%PDF-1.7\n", "pdf"),
        (b"\x89PNG\r\n\x1a\n....", "png"),
        (b"\xff\xd8\xff\xe0....", "jpeg"),
        (b"<html><body>", None),
        (b"junk %PDF-1.7", None),
        (b"", None),
    ],
)
def test_sniff(data: bytes, kind: str | None) -> None:
    assert ga.sniff(data) == kind


def test_html_named_pdf_is_unsupported(tmp_path: Path) -> None:
    out, _ = _run_one(tmp_path, _pdf_part(inline=b"<html>receipt</html>"),
                      extract=_never_extract)

    assert out.records[0].status == "unsupported"


def test_a_png_named_pdf_goes_to_the_image_extractor(tmp_path: Path) -> None:
    seen: list[Path] = []

    def fake_extract(path: Path) -> ExtractedDoc:
        seen.append(path)
        return ExtractedDoc("x", "Example Cafe total 3.00", "screenshot", None,
                            {"ocr": {"engine": "tesseract"}})

    out, _ = _run_one(tmp_path, _pdf_part(inline=receipt_png()), extract=fake_extract)

    assert [p.name for p in seen] == ["attachment.png"]
    assert out.records[0].as_metadata() == {
        "message_id": MID, "part_id": "1", "status": "extracted", "type": "png",
        "bytes": len(receipt_png()), "ocr": "tesseract",
    }


def test_a_jpeg_gets_a_jpg_suffix(tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_extract(path: Path) -> ExtractedDoc:
        seen.append(path.name)
        return ExtractedDoc("x", "", "screenshot", None, {})

    _run_one(tmp_path, _pdf_part(inline=b"\xff\xd8\xff\xe0 jpeg"), extract=fake_extract)

    assert seen == ["attachment.jpg"]


# --- extraction --------------------------------------------------------------


def test_a_text_pdf_becomes_a_redacted_section(tmp_path: Path) -> None:
    data = receipt_pdf()

    out, run = _run_one(tmp_path, _pdf_part(inline=data))

    assert len(out.sections) == 1
    section = out.sections[0]
    assert section.startswith("## Attachment: Receipt-0001.pdf\n\n")
    assert MERCHANT in section and REDACTED_CARD in section
    assert "4111" not in section
    assert out.records[0].as_metadata() == {
        "message_id": MID, "part_id": "1", "status": "extracted", "type": "pdf",
        "bytes": len(data), "pages": 1,
    }
    assert run.counts["extracted"] == 1 and run.processed == 1


def test_an_encrypted_pdf_is_refused_before_extraction(tmp_path: Path) -> None:
    out, _ = _run_one(tmp_path, _pdf_part(inline=receipt_pdf(encrypt="pw")),
                      extract=_never_extract)

    assert out.records[0].status == "encrypted"
    assert out.sections == []


def test_a_pdf_over_the_page_cap_is_refused_before_extraction(tmp_path: Path) -> None:
    run = ga.AttachmentRun(_limits(max_pages=2))

    out, _ = _run_one(tmp_path, _pdf_part(inline=receipt_pdf(pages=3)),
                      extract=_never_extract, run=run)

    assert out.records[0].status == "too many pages"


def test_a_pdf_that_pypdf_cannot_open_is_unreadable(tmp_path: Path) -> None:
    out, _ = _run_one(tmp_path, _pdf_part(inline=b"%PDF-1.7\nbroken"), extract=_never_extract)

    assert out.records[0].status == "unreadable"


def test_a_non_pypdf_error_from_the_page_check_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deep(path: str) -> Any:
        raise RecursionError("nested object streams")

    monkeypatch.setattr("pypdf.PdfReader", deep)

    out, _ = _run_one(tmp_path, _pdf_part(inline=receipt_pdf()), extract=_never_extract)

    assert out.records[0].status == "unreadable"


@pytest.mark.parametrize("error", [ValueError, OSError, RecursionError])
def test_an_extractor_error_is_unreadable_and_carries_no_text(
    tmp_path: Path, error: type[Exception]
) -> None:
    def boom(path: Path) -> ExtractedDoc:
        raise error(f"{MERCHANT} secret content")

    out, run = _run_one(tmp_path, _pdf_part(inline=receipt_png()), extract=boom)

    assert out.records[0].status == "unreadable"
    assert MERCHANT not in repr(out) and run.ocr_hint is None


def test_ocr_unavailable_is_unreadable_and_sets_the_hint(tmp_path: Path) -> None:
    def no_ocr(path: Path) -> ExtractedDoc:
        raise OcrUnavailableError(OCR_MISSING_HINT)

    out, run = _run_one(tmp_path, _pdf_part(inline=receipt_png()), extract=no_ocr)

    assert out.records[0].status == "unreadable"
    assert run.ocr_hint == OCR_MISSING_HINT


def test_the_ocr_hint_is_kept_from_the_first_image(tmp_path: Path) -> None:
    hints = iter(["first hint", "second hint"])

    def no_ocr(path: Path) -> ExtractedDoc:
        raise OcrUnavailableError(next(hints))

    run = ga.AttachmentRun(_limits())
    plan = _plan([_pdf_part(str(i), inline=receipt_png()) for i in (1, 2)])

    ga.extract_attachments(plan.candidates, run=run, runner=FakeGws(), workdir=tmp_path,
                           extract=no_ocr)

    assert run.ocr_hint == "first hint"
    assert run.counts["unreadable"] == 2


def test_empty_text_is_empty_and_gets_no_section(tmp_path: Path) -> None:
    def blank(path: Path) -> ExtractedDoc:
        return ExtractedDoc("x", "  \n ", "pdf", None, {"page_count": "1"})

    out, run = _run_one(tmp_path, _pdf_part(inline=receipt_png()), extract=blank)

    assert out.sections == []
    assert out.records[0].status == "empty"
    assert "pages" not in out.records[0].as_metadata()
    assert run.counts["empty"] == 1


def test_the_temp_file_is_private_fixed_and_removed(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def spy(path: Path) -> ExtractedDoc:
        seen["name"] = path.name
        seen["parent"] = path.parent
        seen["mode"] = stat.S_IMODE(os.lstat(path).st_mode)
        raise ValueError("stop")

    workdir = tmp_path / "w"
    workdir.mkdir(mode=0o700)
    run = ga.AttachmentRun(_limits())
    plan = _plan([attachment_part("1", "../../evil.png", "image/png", size=10,
                                  inline=receipt_png())])

    ga.extract_attachments(plan.candidates, run=run, runner=FakeGws(), workdir=workdir,
                           extract=spy)

    assert seen == {"name": "attachment.png", "parent": workdir, "mode": 0o600}
    assert list(workdir.iterdir()) == []


def test_a_planted_symlink_is_not_followed(tmp_path: Path) -> None:
    workdir = tmp_path / "w"
    workdir.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    (workdir / "attachment.pdf").symlink_to(target)
    run = ga.AttachmentRun(_limits())
    plan = _plan([_pdf_part(inline=receipt_pdf())])

    out = ga.extract_attachments(plan.candidates, run=run, runner=FakeGws(), workdir=workdir,
                                 extract=_never_extract)

    assert out.records[0].status == "unreadable"
    assert target.read_bytes() == b"untouched"


# --- the run's limits --------------------------------------------------------


def test_a_declared_size_over_the_cap_is_skipped_unfetched_and_uncounted(tmp_path: Path) -> None:
    gws = FakeGws()
    limits = _limits()

    out, run = _run_one(tmp_path, _pdf_part(attachment_id=AID, size=limits.max_bytes + 1), gws,
                        extract=_never_extract)

    assert out.records[0].status == "too large"
    assert gws.calls == [] and run.processed == 0
    assert run.counts["too large"] == 1


def test_the_run_cap(tmp_path: Path) -> None:
    gws = FakeGws(attachments={(MID, f"{AID}{i}"): receipt_pdf() for i in range(3)})
    run = ga.AttachmentRun(_limits(max_per_run=2))
    plan = _plan([_pdf_part(str(i + 1), attachment_id=f"{AID}{i}") for i in range(3)])

    out = ga.extract_attachments(plan.candidates, run=run, runner=gws, workdir=tmp_path)

    assert [r.status for r in out.records] == ["extracted", "extracted", "over cap"]
    assert len(gws.attachment_calls()) == 2 and run.processed == 2


def test_the_run_deadline(tmp_path: Path) -> None:
    now = [100.0]
    run = ga.AttachmentRun(_limits(run_deadline_s=10.0), clock=lambda: now[0])
    now[0] = 111.0
    gws = FakeGws()

    out, _ = _run_one(tmp_path, _pdf_part(attachment_id=AID), gws, extract=_never_extract,
                      run=run)

    assert out.records[0].status == "deadline"
    assert gws.calls == []


def test_the_section_cap(tmp_path: Path) -> None:
    def long_text(path: Path) -> ExtractedDoc:
        return ExtractedDoc("x", "word " * 1000, "pdf", None, {"page_count": 1})

    run = ga.AttachmentRun(_limits(max_section_chars=100))

    out, _ = _run_one(tmp_path, _pdf_part(inline=receipt_png()), extract=long_text, run=run)

    assert out.sections[0].endswith("[attachment text truncated]")
    assert len(out.sections[0]) < 200


def test_the_thread_cap_truncates_then_stops_extracting(tmp_path: Path) -> None:
    calls: list[Path] = []

    def text(path: Path) -> ExtractedDoc:
        calls.append(path)
        return ExtractedDoc("x", "word " * 100, "pdf", None, {})

    run = ga.AttachmentRun(_limits(max_section_chars=1000, max_thread_chars=300))
    plan = _plan([_pdf_part(str(i), inline=receipt_png()) for i in (1, 2, 3)])

    out = ga.extract_attachments(plan.candidates, run=run, runner=FakeGws(), workdir=tmp_path,
                                 extract=text)

    assert [r.status for r in out.records] == ["extracted", "truncated", "truncated"]
    assert len(calls) == 1
    assert sum(len(s) for s in out.sections) <= 300 + 200


def test_retryable_statuses() -> None:
    expected = frozenset({"failed", "over cap", "deadline", "unreadable", "truncated"})

    assert expected == ga.RETRYABLE_STATUSES
    assert expected.isdisjoint(ga.FINAL_STATUSES)


# --- the work directory ------------------------------------------------------


def test_the_workdir_is_private_and_removed(tmp_path: Path) -> None:
    with ga.attachment_workdir(root=tmp_path) as workdir:
        assert workdir.parent == tmp_path
        assert workdir.name.startswith(ga.WORKDIR_PREFIX)
        assert stat.S_IMODE(workdir.stat().st_mode) == 0o700
        (workdir / "attachment.pdf").write_bytes(b"x")

    assert not workdir.exists()


def test_the_workdir_is_removed_on_an_exception(tmp_path: Path) -> None:
    with pytest.raises(KeyboardInterrupt), ga.attachment_workdir(root=tmp_path) as workdir:
        raise KeyboardInterrupt

    assert not workdir.exists()


def test_a_stale_workdir_is_swept_and_a_fresh_one_kept(tmp_path: Path) -> None:
    stale = tmp_path / f"{ga.WORKDIR_PREFIX}stale"
    fresh = tmp_path / f"{ga.WORKDIR_PREFIX}fresh"
    stale.mkdir()
    fresh.mkdir()
    old = time.time() - 10 * ga.AttachmentLimits().run_deadline_s
    os.utime(stale, (old, old))

    with ga.attachment_workdir(root=tmp_path):
        pass

    assert not stale.exists() and fresh.exists()


# --- module hygiene ----------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    ["gmail_attachments.py", "attachment_text.py"],
)
def test_the_new_ingest_modules_have_no_network_or_process_code(module: str) -> None:
    path = Path(ga.__file__).with_name(module)
    banned = {"socket", "httpx", "urllib", "requests", "subprocess", "http"}
    tree = ast.parse(path.read_text())

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert imported.isdisjoint(banned)
