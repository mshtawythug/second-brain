"""The attachment parse child and its client, the carry rules, the status errors (sec-117).

Real child processes run here (``ChildExtractor`` with ``run_capped``) on synthetic
reportlab PDFs; the worker's own functions are also called in-process so coverage sees
them. No database.
"""
from __future__ import annotations

import hashlib
import io
import json
import resource
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from brain.ingest import ExtractedDoc
from brain.ingest import attachment_worker as worker
from brain.ingest.attachment_carry import carried_parts, stored_records, stored_sections
from brain.ingest.attachment_errors import (
    EncryptedAttachmentError,
    NoOcrError,
    TooManyPagesError,
    UnreadableAttachmentError,
    error_for_status,
)
from brain.ingest.attachment_isolation import (
    WORKER_MODULE,
    ChildExtractor,
    WorkerLimits,
    stdout_cap,
    worker_env,
)
from brain.ingest.capped_process import CappedRun
from brain.ingest.gmail_attachments import AttachmentRecord
from tests.gmail_attachment_fixtures import (
    MERCHANT,
    bogus_filter_pdf,
    receipt_pdf,
    receipt_png,
    shared_stream_pdf,
)

MID = "18c0ffee0000f001"


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _child(**limits: Any) -> ChildExtractor:
    return ChildExtractor(max_pages=5, max_chars=1000, limits=WorkerLimits(**limits))


# --- the real child -------------------------------------------------------------


def test_the_child_extracts_a_text_pdf(tmp_path: Path) -> None:
    doc = _child()(_write(tmp_path, "attachment.pdf", receipt_pdf()))

    assert MERCHANT in doc.content
    assert doc.metadata == {"page_count": 1}


def test_the_child_reports_its_statuses(tmp_path: Path) -> None:
    with pytest.raises(EncryptedAttachmentError):
        _child()(_write(tmp_path, "attachment.pdf", receipt_pdf(encrypt="pw")))
    with pytest.raises(TooManyPagesError):
        _child()(_write(tmp_path, "attachment.pdf", receipt_pdf(pages=6)))
    with pytest.raises(UnreadableAttachmentError):
        _child()(_write(tmp_path, "attachment.pdf", bogus_filter_pdf()))


def test_the_child_reports_no_ocr_for_an_image_while_ocr_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_OCR", "off")

    with pytest.raises(NoOcrError):
        _child()(_write(tmp_path, "attachment.png", receipt_png()))


@pytest.mark.timeout(60)
def test_the_child_is_stopped_at_its_cpu_limit(tmp_path: Path) -> None:
    """sec-117 F3: a ~3 KB PDF whose pages share one content stream parses for seconds."""
    started = time.monotonic()

    with pytest.raises(UnreadableAttachmentError):
        _child(cpu_s=1, timeout_s=30.0)(_write(tmp_path, "attachment.pdf", shared_stream_pdf()))

    assert time.monotonic() - started < 25


@pytest.mark.timeout(60)
def test_the_child_is_stopped_at_its_wall_clock_limit(tmp_path: Path) -> None:
    started = time.monotonic()

    with pytest.raises(UnreadableAttachmentError):
        _child(timeout_s=1.0)(_write(tmp_path, "attachment.pdf", shared_stream_pdf()))

    assert time.monotonic() - started < 20


@pytest.mark.timeout(60)
def test_the_child_is_stopped_at_its_memory_limit(tmp_path: Path) -> None:
    """The RSS watchdog (macOS refuses RLIMIT_AS) stops a parse past ``max_rss_bytes``."""
    with pytest.raises(UnreadableAttachmentError):
        _child(max_rss_bytes=1)(_write(tmp_path, "attachment.pdf", receipt_pdf()))


def test_the_child_cuts_its_text_past_the_cap_plus_the_slack(tmp_path: Path) -> None:
    lines = [f"Example line {n}" for n in range(40)]
    extractor = ChildExtractor(max_pages=5, max_chars=50)

    doc = extractor(_write(tmp_path, "attachment.pdf", receipt_pdf(lines)))

    assert len(doc.content) == 50 + 1 + worker.TEXT_SLACK_CHARS


def test_the_one_factory_uses_the_one_worker_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """rev-117 nit: the reader's default and the CLI session build the same extractor."""
    from brain.ingest import attachment_isolation
    from brain.ingest.gmail_attachments import AttachmentLimits, child_extractor

    limits = WorkerLimits(timeout_s=7.0)
    monkeypatch.setattr(attachment_isolation, "WORKER_LIMITS", limits)

    extractor = child_extractor(AttachmentLimits(max_pages=3, max_section_chars=9))

    assert isinstance(extractor, ChildExtractor)
    assert extractor.argv(Path("a.pdf"))[-3:] == ["3", "9", str(limits.max_rss_bytes)]
    assert extractor._limits is limits


# --- the client, with a fake runner ------------------------------------------------


def _fake(run: CappedRun, seen: list[dict[str, Any]] | None = None) -> Any:
    def fake(argv: list[str], **kwargs: Any) -> CappedRun:
        if seen is not None:
            seen.append({"argv": argv, **kwargs})
        return run

    return fake


def test_the_client_runs_the_worker_isolated_and_limited(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    answer = json.dumps({"content": "x", "page_count": None, "ocr": "tesseract"}).encode()
    limits = WorkerLimits(timeout_s=9.0, cpu_s=7, address_space_bytes=123, max_rss_bytes=45)
    environ = {"PATH": "/bin", "BRAIN_OCR": "off", "DATABASE_URL": "postgresql://x",
               "HOME": "/h", "BRAIN_VAULT_PATH": "/v"}
    extractor = ChildExtractor(max_pages=3, max_chars=10, limits=limits,
                               run=_fake(CappedRun(answer, 0, False, False), seen),
                               executable="/py", environ=environ)

    doc = extractor(tmp_path / "attachment.png")

    assert doc.metadata == {"ocr": {"engine": "tesseract"}}
    (call,) = seen
    assert call["argv"] == ["/py", "-P", "-s", "-m", WORKER_MODULE,
                            str(tmp_path / "attachment.png"), "3", "10", "45"]
    assert call["timeout"] == 9.0 and call["max_stdout_bytes"] == stdout_cap(10)
    assert call["env"] == {"PATH": "/bin", "BRAIN_OCR": "off", "HOME": "/h"}
    assert call["resource_limits"] == [(resource.RLIMIT_CPU, 7), (resource.RLIMIT_AS, 123)]


@pytest.mark.parametrize(
    "run",
    [
        CappedRun(b"", 0, timed_out=True, oversized=False),
        CappedRun(b"", 0, timed_out=False, oversized=True),
        CappedRun(b"", -24, timed_out=False, oversized=False),
        CappedRun(b"not json", 0, False, False),
        CappedRun(b"[" * 10_000 + b"]" * 10_000, 0, False, False),
        CappedRun(b"[1]", 0, False, False),
        CappedRun(b'{"content": 7}', 0, False, False),
        CappedRun(b'{"error": "something new"}', 0, False, False),
    ],
)
def test_every_bad_answer_is_unreadable(tmp_path: Path, run: CappedRun) -> None:
    extractor = ChildExtractor(max_pages=1, max_chars=10, run=_fake(run))

    with pytest.raises(UnreadableAttachmentError):
        extractor(tmp_path / "attachment.pdf")


def test_a_worker_that_cannot_start_is_unreadable(tmp_path: Path) -> None:
    def cannot(argv: list[str], **kwargs: Any) -> CappedRun:
        raise PermissionError("denied")

    with pytest.raises(UnreadableAttachmentError):
        ChildExtractor(max_pages=1, max_chars=10, run=cannot)(tmp_path / "attachment.pdf")


def test_bad_answer_fields_are_dropped(tmp_path: Path) -> None:
    answer = json.dumps({"content": "x", "page_count": True, "ocr": 3}).encode()
    extractor = ChildExtractor(max_pages=1, max_chars=10,
                               run=_fake(CappedRun(answer, 0, False, False)))

    assert extractor(tmp_path / "a.pdf").metadata == {}


def test_worker_env_keeps_only_what_the_worker_needs() -> None:
    env = worker_env({"PATH": "/bin", "BRAIN_OCR_LANG": "eng", "VOYAGE_API_KEY": "k",
                      "LC_ALL": "C", "PYTHONPATH": "/src"})

    assert env == {"PATH": "/bin", "BRAIN_OCR_LANG": "eng", "LC_ALL": "C", "PYTHONPATH": "/src"}


def test_the_stdout_cap_fits_the_worst_case_answer() -> None:
    answer = json.dumps({"content": "\x01" * 101, "page_count": 10**9, "ocr": "tesseract"},
                        ensure_ascii=False)

    assert len(answer.encode()) <= stdout_cap(100)


# --- the worker, in-process ------------------------------------------------------------


def test_run_returns_text_pages_and_engine(tmp_path: Path) -> None:
    result = worker.run(_write(tmp_path, "attachment.pdf", receipt_pdf()), max_pages=5,
                        max_chars=1000)

    assert MERCHANT in result["content"]
    assert (result["page_count"], result["ocr"]) == (1, None)


def test_run_reduces_every_failure_to_a_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _write(tmp_path, "attachment.pdf", receipt_pdf())

    def boom(path: Path, *, max_pages: int) -> ExtractedDoc:
        raise NotImplementedError("unsupported filter")

    monkeypatch.setattr(worker, "extract_in_process", boom)

    assert worker.run(pdf, max_pages=5, max_chars=10) == {"error": "unreadable"}


def test_run_reports_an_engine_from_the_ocr_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ocrd(path: Path, *, max_pages: int) -> ExtractedDoc:
        return ExtractedDoc("x", "text", "screenshot", None, {"ocr": {"engine": "tesseract"}})

    monkeypatch.setattr(worker, "extract_in_process", ocrd)

    result = worker.run(tmp_path / "a.png", max_pages=1, max_chars=10)

    assert result == {"content": "text", "page_count": None, "ocr": "tesseract"}


def test_check_pdf_wraps_any_parser_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page check's own containment: pypdf can raise anything on a hostile file."""
    def deep(path: str) -> Any:
        raise RecursionError("nested object streams")

    monkeypatch.setattr("pypdf.PdfReader", deep)

    with pytest.raises(UnreadableAttachmentError):
        worker.check_pdf(tmp_path / "a.pdf", max_pages=5)


def test_an_image_skips_the_pdf_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_OCR", "off")

    assert worker.run(_write(tmp_path, "attachment.png", receipt_png()), max_pages=0,
                      max_chars=10) == {"error": "no ocr"}


def test_main_writes_one_json_object(tmp_path: Path) -> None:
    pdf = _write(tmp_path, "attachment.pdf", receipt_pdf())
    out = io.StringIO()
    watched: list[int] = []

    assert worker.main([str(pdf), "5", "1000", "777"], out=out, watchdog=watched.append) == 0

    assert MERCHANT in json.loads(out.getvalue())["content"]
    assert watched == [777]


@pytest.mark.parametrize("argv", [[], ["a", "b", "c"], ["a", "x", "1", "1"]])
def test_main_refuses_bad_arguments(argv: list[str]) -> None:
    assert worker.main(argv, out=io.StringIO(), watchdog=lambda limit: None) == 2


def test_the_watchdog_exits_once_the_peak_passes_the_limit() -> None:
    readings = iter([10, 20, 31])
    exits: list[int] = []

    thread = worker.start_rss_watchdog(30, interval=0.001, peak=lambda: next(readings),
                                       exit_process=exits.append)
    thread.join(timeout=5)

    assert exits == [worker.RSS_EXIT_CODE]


def test_the_worker_runs_as_a_module_with_no_cwd_on_its_path(tmp_path: Path) -> None:
    """``-P``: a planted ``json.py`` in the working directory is not imported."""
    import subprocess

    (tmp_path / "json.py").write_text("raise SystemExit(99)\n")
    pdf = _write(tmp_path, "attachment.pdf", receipt_pdf())

    proc = subprocess.run(
        [sys.executable, "-P", "-s", "-m", WORKER_MODULE, str(pdf), "5", "100", str(1 << 31)],
        cwd=tmp_path, capture_output=True, timeout=60, check=False,
    )

    assert proc.returncode == 0
    assert MERCHANT in json.loads(proc.stdout)["content"]


# --- the status errors -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "cls"),
    [("encrypted", EncryptedAttachmentError), ("too many pages", TooManyPagesError),
     ("no ocr", NoOcrError), ("unreadable", UnreadableAttachmentError),
     ("failed", UnreadableAttachmentError), (None, UnreadableAttachmentError)],
)
def test_error_for_status(status: object, cls: type) -> None:
    assert type(error_for_status(status)) is cls


# --- the carry rules ----------------------------------------------------------------


def _section(text: str) -> str:
    return f"## Attachment: r.pdf\n\n{text}"


def _record(part: str, status: str, section: str | None = None, **kw: Any) -> AttachmentRecord:
    extra: dict[str, Any] = {}
    if section is not None:
        extra = {"chars": len(section),
                 "sha256": hashlib.sha256(section.encode()).hexdigest()}
    return AttachmentRecord(MID, part, 10, status, **{**extra, **kw})


def _body(*sections: str) -> str:
    return "\n\n".join(["## 2026-01-01 — mail\n\nbody", *sections])


def test_stored_sections_are_cut_from_the_end_and_checked() -> None:
    one, two = _section("Total 1.00"), _section("Total 2.00")
    records = [_record("1", "extracted", one), _record("2", "too large"),
               _record("3", "extracted", two)]

    found = stored_sections(_body(one, two), records)

    assert found == {(MID, "1", 10): one, (MID, "3", 10): two}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body, recs: (body.replace("Total 1.00", "Total 1.01"), recs),
        lambda body, recs: (body, [_record("1", "extracted", None)]),
        lambda body, recs: ("short", recs),
        lambda body, recs: (body.replace("\n\n## Attachment", "\n## Attachment", 1), recs),
        lambda body, recs: (body, [_record("1", "extracted", None, chars=0, sha256="x")]),
    ],
)
def test_stored_sections_that_do_not_check_out_are_untrusted(mutate: Any) -> None:
    one = _section("Total 1.00")
    body, records = mutate(_body(one), [_record("1", "extracted", one)])

    assert stored_sections(body, records) is None


def test_two_sections_with_a_broken_separator_are_untrusted() -> None:
    one, two = _section("Total 1.00"), _section("Total 2.00")
    records = [_record("1", "extracted", one), _record("2", "extracted", two)]
    body = _body(one, two).replace(f"{one}\n\n", f"{one}\n-", 1)

    assert stored_sections(body, records) is None


def test_no_extracted_record_means_no_sections() -> None:
    assert stored_sections("anything", [_record("1", "too large")]) == {}


def test_carried_parts_keeps_final_outcomes_only() -> None:
    one = _section("Total 1.00")
    raw = [r.as_metadata() for r in (
        _record("1", "extracted", one), _record("2", "over cap"), _record("3", "unreadable"),
    )] + ["junk", {"message_id": MID}]

    carried = carried_parts(_body(one), raw)

    assert set(carried) == {(MID, "1", 10), (MID, "3", 10)}
    assert carried[(MID, "1", 10)][1] == one
    assert carried[(MID, "3", 10)][1] is None


def test_untrusted_sections_carry_no_extracted_part() -> None:
    one = _section("Total 1.00")
    raw = [_record("1", "extracted", one).as_metadata(), _record("2", "empty").as_metadata()]

    carried = carried_parts(_body(one).replace("1.00", "9.99"), raw)

    assert set(carried) == {(MID, "2", 10)}


def test_stored_records_reads_defensively() -> None:
    good = _record("1", "empty", type="pdf", bytes=3, pages=1, ocr="tesseract")

    assert stored_records("not a list") == []
    assert stored_records([good.as_metadata(), {"message_id": MID, "part_id": "2",
                                                "status": "empty", "size": "10"}]) == [good]
    assert AttachmentRecord.from_metadata(
        {"message_id": MID, "part_id": "1", "size": 1, "status": "x", "type": 5}
    ) == AttachmentRecord(MID, "1", 1, "x")


@pytest.mark.parametrize(
    ("data", "status"),
    [(receipt_pdf(encrypt="pw"), "encrypted"), (receipt_pdf(pages=3), "too many pages")],
)
def test_run_reports_the_page_check_in_process(
    tmp_path: Path, data: bytes, status: str
) -> None:
    assert worker.run(_write(tmp_path, "attachment.pdf", data), max_pages=2,
                      max_chars=10) == {"error": status}


def test_the_peak_rss_reading_is_in_bytes() -> None:
    peak = worker._peak_rss_bytes()

    assert 1 << 20 < peak < 1 << 40


# --- rev-117 round 2: the cut comes after the redaction ---------------------------


def test_a_card_straddling_the_cap_leaves_no_digits_behind(tmp_path: Path) -> None:
    """The child cut its text to max_chars + 1 before the parent redacted it, so a card
    across the cap reached the parent as its first digits."""
    from brain.ingest.attachment_text import render_section

    line = "x" * 95 + " 4111111111111111 ok"
    doc = ChildExtractor(max_pages=5, max_chars=100)(
        _write(tmp_path, "attachment.pdf", receipt_pdf([line]))
    )

    section = render_section("r.pdf", doc.content, max_chars=100)

    assert section is not None
    assert "4111" not in section and "411" not in section.replace("x", "")


def test_the_worker_returns_slack_past_the_cap(tmp_path: Path) -> None:
    lines = [f"Example line {n}" for n in range(40)]

    result = worker.run(_write(tmp_path, "attachment.pdf", receipt_pdf(lines)), max_pages=5,
                        max_chars=50)

    assert len(result["content"]) == 50 + 1 + worker.TEXT_SLACK_CHARS


# --- sec-117 round 2 R1: pypdf's own stream limits, lowered in the worker -------------


@pytest.mark.timeout(120)
def test_a_content_stream_over_the_pdf_limit_is_unreadable(tmp_path: Path) -> None:
    """About 18 MB decoded from a 27 KB file: refused before any page is parsed."""
    pdf = _write(tmp_path, "attachment.pdf", shared_stream_pdf(pages=1, operators=3_000_000))
    started = time.monotonic()

    assert worker.run(pdf, max_pages=5, max_chars=10) == {"error": "unreadable"}

    assert time.monotonic() - started < 10


@pytest.mark.timeout(120)
def test_a_stream_shared_by_many_pages_counts_once_per_page(tmp_path: Path) -> None:
    """Each page re-parses a shared stream, so the budget counts every page reference."""
    pdf = _write(tmp_path, "attachment.pdf", shared_stream_pdf(pages=20, operators=200_000))
    started = time.monotonic()

    assert worker.run(pdf, max_pages=50, max_chars=10) == {"error": "unreadable"}

    assert time.monotonic() - started < 10


def test_the_pdf_limits_are_scoped_to_the_parse(tmp_path: Path) -> None:
    import pypdf

    before = pypdf.get_configuration().zlib_maximum_output_length

    worker.run(_write(tmp_path, "attachment.pdf", receipt_pdf()), max_pages=5, max_chars=10)

    assert pypdf.get_configuration().zlib_maximum_output_length == before


def test_a_receipt_under_the_limits_still_extracts(tmp_path: Path) -> None:
    result = worker.run(_write(tmp_path, "attachment.pdf", receipt_pdf(pages=3)), max_pages=5,
                        max_chars=1000)

    assert MERCHANT in result["content"]



def test_the_lowered_limits_are_in_force_while_the_registry_extracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream past 16 MB that the per-page budget would not see (a font's ToUnicode CMap)
    is bounded by pypdf itself, so the limits must hold during the extraction too."""
    import pypdf

    from brain.ingest import dispatch

    seen: list[int] = []

    def spy(path: Path) -> ExtractedDoc:
        seen.append(pypdf.get_configuration().zlib_maximum_output_length)
        return ExtractedDoc("x", "text", "pdf", None, {})

    monkeypatch.setattr(dispatch, "extract_path", spy)

    worker.extract_in_process(_write(tmp_path, "attachment.pdf", receipt_pdf()), max_pages=5)

    assert seen == [worker.PDF_STREAM_LIMIT_BYTES]


def test_zero_width_characters_cannot_push_a_card_past_the_slack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sec-117 I-r3-4: the worker cut the raw text, the parent redacted the normalized one,
    so zero-width characters before a card spent the slack and left its digits behind."""
    from brain.ingest.attachment_text import render_section

    raw = "a" * 95 + " " + "​" * 300 + "4111111111111111 ok"

    def text(path: Path, *, max_pages: int) -> ExtractedDoc:
        return ExtractedDoc("x", raw, "pdf", None, {})

    monkeypatch.setattr(worker, "extract_in_process", text)

    result = worker.run(tmp_path / "a.pdf", max_pages=1, max_chars=100)
    section = render_section("r.pdf", result["content"], max_chars=100)

    assert section is not None
    assert "4111" not in section and "​" not in result["content"]
