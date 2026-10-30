"""The OCR fallback in ``brain.ingest.pdf`` (roadmap ticket RM-32).

Every PDF here is synthetic and built in the test: Pillow renders a line of
made-up text to an image, and reportlab places it on a page with no text layer.
A fake :class:`~brain.ingest.ocr.OcrEngine` stands in for tesseract, except in
the one ``live_ocr`` test, which runs the real binary and skips without it.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import psycopg
import pypdfium2
import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from typer.testing import CliRunner

from brain.ingest import ingest_document
from brain.ingest.ocr import PAGE_TIMEOUT_S, OcrError, OcrSetup, OcrTimeoutError, resolve_ocr
from brain.ingest.pdf import MIN_CHARS_PER_PAGE, TESSERACT_HINT, extract_pdf, render_page_png
from brain.settings.ocr import OcrSettings
from tests.secret_fixtures import SYNTHETIC_AWS_KEY

AUTO = OcrSettings(mode="auto", lang="eng", max_pages=50)


# ---------------------------------------------------------------------------
# synthetic PDFs
# ---------------------------------------------------------------------------


def _text_image(text: str) -> Image.Image:
    image = Image.new("RGB", (1600, 240), "white")
    font = ImageFont.load_default(size=96)
    ImageDraw.Draw(image).text((40, 60), text, fill="black", font=font)
    return image


def _image_only_pdf(path: Path, page_texts: Sequence[str]) -> Path:
    """One page per entry, each carrying only an image of its text."""
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for text in page_texts:
        pdf.drawImage(ImageReader(_text_image(text)), 36, 500, width=540, height=81)
        pdf.showPage()
    pdf.save()
    return path


def _text_pdf(path: Path, lines: Sequence[str], *, with_image: bool) -> Path:
    """A born-digital page: a real text layer, plus an image when asked."""
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for offset, line in enumerate(lines):
        pdf.drawString(72, 720 - 16 * offset, line)
    if with_image:
        pdf.drawImage(ImageReader(_text_image("LOGO")), 72, 300, width=200, height=30)
    pdf.showPage()
    pdf.save()
    return path


class _Engine:
    """A fake engine: answers page ``n`` (1-based call order) from ``answers``."""

    name = "fake-ocr"

    def __init__(self, answers: Sequence[str | BaseException]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[bytes, str, float]] = []

    def image_to_text(self, png: bytes, *, lang: str, timeout: float) -> str:
        self.calls.append((png, lang, timeout))
        answer = self._answers[len(self.calls) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _setup(engine: _Engine | None, settings: OcrSettings = AUTO, **kw: bool) -> OcrSetup:
    return OcrSetup(settings=settings, engine=engine, **kw)


# ---------------------------------------------------------------------------
# the fallback
# ---------------------------------------------------------------------------


def test_an_image_only_pdf_is_ocrd_with_page_markers_and_metadata(tmp_path: Path) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["SYNTHETIC ONE", "SYNTHETIC TWO"])
    engine = _Engine(["SYNTHETIC RECEIPT 12.34\n", "  Total due 56.78  "])

    doc = extract_pdf(path, ocr=_setup(engine, OcrSettings(mode="auto", lang="deu", max_pages=50)))

    assert doc.content == "[page 1]\nSYNTHETIC RECEIPT 12.34\n\n[page 2]\nTotal due 56.78"
    assert doc.metadata == {"page_count": 2, "ocr": {"pages": 2, "engine": "fake-ocr"}}
    assert [(lang, timeout) for _, lang, timeout in engine.calls] == [
        ("deu", PAGE_TIMEOUT_S),
        ("deu", PAGE_TIMEOUT_S),
    ]
    assert all(png.startswith(b"\x89PNG\r\n\x1a\n") for png, _, _ in engine.calls)


def test_pages_are_rendered_at_200_dpi(tmp_path: Path) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["SYNTHETIC"])
    engine = _Engine(["text"])

    extract_pdf(path, ocr=_setup(engine))

    with Image.open(io.BytesIO(engine.calls[0][0])) as page:
        assert page.size == (1700, 2200)  # US letter, 8.5 x 11 in at 200 dpi


def test_a_born_digital_pdf_with_sparse_text_and_no_image_is_never_ocrd(
    fixtures_dir: Path,
) -> None:
    """``sample.pdf`` averages under 25 characters a page; it has no image to read."""
    engine = _Engine([])

    doc = extract_pdf(fixtures_dir / "sample.pdf", ocr=_setup(engine))

    assert engine.calls == []
    assert "ocr" not in doc.metadata
    assert "Hello from page one" in doc.content


def test_a_born_digital_pdf_with_an_image_and_real_text_is_never_ocrd(tmp_path: Path) -> None:
    lines = ["Synthetic quarterly memo about the widget programme and its budget."] * 3
    path = _text_pdf(tmp_path / "memo.pdf", lines, with_image=True)
    engine = _Engine([])

    doc = extract_pdf(path, ocr=_setup(engine))

    assert engine.calls == []
    assert "ocr" not in doc.metadata
    assert "widget programme" in doc.content


def test_sparse_text_with_an_image_fires_the_fallback(tmp_path: Path) -> None:
    path = _text_pdf(tmp_path / "slide.pdf", ["Q3"], with_image=True)
    engine = _Engine(["Q3 SYNTHETIC CHART LABELS"])

    doc = extract_pdf(path, ocr=_setup(engine))

    assert len("Q3") < MIN_CHARS_PER_PAGE
    assert doc.content == "[page 1]\nQ3 SYNTHETIC CHART LABELS"


def test_the_page_cap_holds(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = _image_only_pdf(tmp_path / "long.pdf", [f"PAGE {n}" for n in range(1, 6)])
    engine = _Engine(["one", "two", "three"])
    capped = OcrSettings(mode="auto", lang="eng", max_pages=3)

    with caplog.at_level(logging.INFO, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine, capped))

    assert len(engine.calls) == 3
    assert doc.content == "[page 1]\none\n\n[page 2]\ntwo\n\n[page 3]\nthree"
    assert doc.metadata == {"page_count": 5, "ocr": {"pages": 3, "engine": "fake-ocr"}}
    assert "3 of 5 pages" in caplog.text


@pytest.mark.parametrize(
    ("failure", "label"),
    [
        (OcrTimeoutError("tesseract ran past 60s"), "OcrTimeoutError"),
        (OcrError("tesseract exited 1", log_detail="exit 1"), "OcrError[exit 1]"),
    ],
    ids=["timeout", "non-zero-exit"],
)
def test_a_failed_page_is_skipped_with_a_class_only_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: OcrError, label: str
) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["A", "B", "C"])
    engine = _Engine(["first", failure, "third"])

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine))

    assert doc.content == "[page 1]\nfirst\n\n[page 3]\nthird"
    assert doc.metadata["ocr"] == {"pages": 2, "engine": "fake-ocr"}
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.getMessage() == f"OCR skipped page 2 of 3: {label}"
    assert "tesseract" not in record.getMessage()


def test_a_page_that_cannot_be_rendered_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["A", "B"])
    engine = _Engine(["second"])

    calls: list[int] = []

    def broken_render(page: pypdfium2.PdfPage) -> bytes:
        calls.append(1)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return render_page_png(page)

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine), render=broken_render)

    assert doc.content == "[page 2]\nsecond"
    assert "OCR skipped page 1 of 2: OSError[ENOSPC]" in caplog.text


def test_ocr_that_recognises_nothing_keeps_the_document_empty(tmp_path: Path) -> None:
    path = _image_only_pdf(tmp_path / "blank.pdf", ["", ""])
    engine = _Engine(["   \n", ""])

    doc = extract_pdf(path, ocr=_setup(engine))

    assert doc.content == ""
    assert doc.metadata == {"page_count": 2}


def test_without_tesseract_the_pdf_stays_empty_with_the_hint(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["SYNTHETIC"])

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(None, missing_binary=True))

    assert doc.content == ""
    assert "ocr" not in doc.metadata
    assert caplog.messages == [TESSERACT_HINT]
    assert "tesseract" in TESSERACT_HINT and "brew install tesseract" in TESSERACT_HINT


def test_ocr_off_stays_empty_and_says_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["SYNTHETIC"])
    off = OcrSettings(mode="off", lang="eng", max_pages=50)

    with caplog.at_level(logging.DEBUG, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(None, off))

    assert doc.content == ""
    assert caplog.records == []


def test_an_unopenable_pdfium_document_keeps_the_extracted_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["SYNTHETIC"])
    engine = _Engine([])

    def refuse(_: Path) -> pypdfium2.PdfDocument:
        raise pypdfium2.PdfiumError("synthetic open failure")

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine), open_pdfium=refuse)

    assert doc.content == ""
    assert engine.calls == []
    assert caplog.messages == ["OCR could not open a PDF: PdfiumError"]


def test_the_default_setup_is_resolved_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["SYNTHETIC"])
    monkeypatch.setenv("BRAIN_OCR", "off")

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path)

    assert doc.content == ""
    assert caplog.records == []


def test_a_pdf_with_a_real_text_layer_never_reads_the_ocr_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid knob would raise if it were read; a full text layer must not read it."""
    lines = ["Synthetic quarterly memo about the widget programme and its budget."] * 3
    path = _text_pdf(tmp_path / "memo.pdf", lines, with_image=True)
    monkeypatch.setenv("BRAIN_OCR", "not-a-mode")

    doc = extract_pdf(path)

    assert "widget programme" in doc.content


# ---------------------------------------------------------------------------
# through the ingest pipeline (real test DB)
# ---------------------------------------------------------------------------


def test_ocr_text_is_stored_and_found_by_full_text_search(
    tmp_path: Path, test_db: psycopg.Connection, fake_embedder: object
) -> None:
    path = _image_only_pdf(tmp_path / "receipt.pdf", ["SYNTHETIC"])
    doc = extract_pdf(path, ocr=_setup(_Engine(["SYNTHETIC RECEIPT 12.34 widgetry"])))

    result = ingest_document(test_db, embedder=fake_embedder, doc=doc, enrich=False)  # type: ignore[arg-type]

    assert result.document_id is not None
    row = test_db.execute(
        "SELECT content, metadata -> 'ocr' FROM documents WHERE id = %s", (result.document_id,)
    ).fetchone()
    assert row == ("[page 1]\nSYNTHETIC RECEIPT 12.34 widgetry", {"pages": 1, "engine": "fake-ocr"})
    hit = test_db.execute(
        "SELECT count(*) FROM chunks"
        " WHERE document_id = %s AND tsv @@ plainto_tsquery('english', %s)",
        (result.document_id, "widgetry"),
    ).fetchone()
    assert hit == (1,)


def test_the_secret_guard_runs_on_ocr_text(
    tmp_path: Path, test_db: psycopg.Connection, fake_embedder: object
) -> None:
    path = _image_only_pdf(tmp_path / "statement.pdf", ["SYNTHETIC"])
    doc = extract_pdf(path, ocr=_setup(_Engine([f"Access key {SYNTHETIC_AWS_KEY} on file"])))

    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=doc,
        enrich=False,
        secret_guard="redact",
    )

    assert result.secret_findings
    stored = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (result.document_id,)
    ).fetchone()
    assert stored is not None and SYNTHETIC_AWS_KEY not in stored[0]


def test_cli_ingest_without_tesseract_reports_an_empty_document_and_the_hint(
    tmp_path: Path,
    test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Acceptance 5, end to end through ``brain ingest``: ``PATH`` holds no ``tesseract``."""
    from brain.cli import app
    from tests.conftest import FakeEmbedder

    path = _image_only_pdf(tmp_path / "scan.pdf", ["SYNTHETIC"])
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    monkeypatch.setenv("BRAIN_OCR", "auto")  # tests/ocr_isolation_plugin.py sets it off
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: None)

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        result = CliRunner().invoke(app, ["ingest", str(path)])

    assert result.exit_code == 0, result.output
    assert "skipped (empty document)" in result.output
    assert TESSERACT_HINT in caplog.messages


# ---------------------------------------------------------------------------
# the real binary
# ---------------------------------------------------------------------------


@pytest.mark.live_ocr
@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_live_tesseract_reads_a_synthetic_receipt(tmp_path: Path) -> None:
    path = _image_only_pdf(tmp_path / "receipt.pdf", ["SYNTHETIC RECEIPT 12.34"])

    doc = extract_pdf(path, ocr=resolve_ocr({}))

    assert "SYNTHETIC RECEIPT 12.34" in doc.content
    assert doc.metadata["ocr"] == {"pages": 1, "engine": "tesseract"}

