"""The OCR fallback's resource bounds (RM-32, the #99 security review).

A PDF is untrusted input: its MediaBox sets how large a page renders, and its
page count how long OCR runs. These tests pin the four bounds: a pixel cap on
each rendered page, a per-document deadline, no second parser while OCR is off,
and an image probe limited to the pages OCR would read. Every PDF is synthetic.
"""
from __future__ import annotations

import ctypes
import functools
import io
import logging
import math
from pathlib import Path

import psycopg
import pypdfium2
import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from brain.ingest import ingest_document
from brain.ingest.ocr import PAGE_TIMEOUT_S, OcrSetup
from brain.ingest.pdf import (
    MAX_PIXELS,
    MAX_SIDE_PX,
    MIN_RENDER_DPI,
    OCR_DEADLINE_S,
    PageTooLargeError,
    extract_pdf,
    render_page_png,
    render_scale,
)
from brain.settings.ocr import OcrSettings
from tests.test_ingest_pdf_ocr import AUTO, _Engine, _image_only_pdf, _setup

DPI_200 = 200 / 72


def _square_pdf(
    path: Path, side_pt: float, *, image_on: tuple[int, ...] = (1,), pages: int = 1
) -> Path:
    """``pages`` square pages of ``side_pt`` points; a 1x1 image on each page in ``image_on``."""
    pdf = canvas.Canvas(str(path), pagesize=(side_pt, side_pt))
    dot = ImageReader(Image.new("RGB", (1, 1), "white"))
    for number in range(1, pages + 1):
        if number in image_on:
            pdf.drawImage(dot, 10, 10, width=1, height=1)
        pdf.showPage()
    pdf.save()
    return path


def _thin_pdf(path: Path, width_pt: float, height_pt: float) -> Path:
    """One page of the given MediaBox with a 1x1 image, written by hand: reportlab rounds."""
    image = b"\xff"
    draw = b"q 1 0 0 1 0 0 cm /Im0 Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt:f} {height_pt:f}] "
            f"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>"
        ).encode(),
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 1 >>\nstream\n" + image + b"\nendstream",
        f"<< /Length {len(draw)} >>\nstream\n".encode() + draw + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    out += trailer.encode()
    path.write_bytes(bytes(out))
    return path


# ---------------------------------------------------------------------------
# render size (HIGH)
# ---------------------------------------------------------------------------


def test_a_normal_page_renders_at_200_dpi() -> None:
    assert render_scale(612, 792) == DPI_200
    assert MAX_PIXELS == 70_000_000


def _bitmap(width_pt: float, height_pt: float, scale: float) -> tuple[int, int]:
    """The bitmap pdfium allocates: each side rounded UP to a whole pixel."""
    return math.ceil(width_pt * scale), math.ceil(height_pt * scale)


@pytest.mark.parametrize(("width", "height"), [(5000, 5000), (5000, 3000), (3000, 9000)])
def test_a_large_page_is_scaled_down_so_the_rounded_bitmap_fits_the_cap(
    width: float, height: float
) -> None:
    """sec-99b: ``sqrt(MAX_PIXELS / area)`` alone gives 8,367 x 8,367 for 5,000 pt, over the cap."""
    scale = render_scale(width, height)

    width_px, height_px = _bitmap(width, height, scale)
    assert width_px * height_px <= MAX_PIXELS
    assert scale == pytest.approx(math.sqrt(MAX_PIXELS / (width * height)), rel=1e-3)
    assert scale < DPI_200


@pytest.mark.parametrize(
    ("width", "height"),
    [(0.01, 3.5e8), (1e-30, 1e30), (3.5e8, 0.01)],
    ids=["thin-tall", "overflow-int", "thin-wide"],
)
def test_a_thin_page_cannot_round_past_the_cap(width: float, height: float) -> None:
    """sec-99b: 0.01 x 3.5e8 pt was 3.5e6 pt2 of area but a 1 x 972,222,223 px bitmap."""
    with pytest.raises(PageTooLargeError):
        render_scale(width, height)


def test_a_side_past_tesseracts_limit_is_refused_even_under_the_area_cap() -> None:
    """1 x 40,000 pt is 3 x 111,112 px at 200 dpi: 333 kpx, but one side over 32,767."""
    assert MAX_SIDE_PX == 32_767
    with pytest.raises(PageTooLargeError, match="side"):
        render_scale(1, 40_000)
    assert max(_bitmap(1, 10_000, render_scale(1, 10_000))) <= MAX_SIDE_PX


@pytest.mark.parametrize(
    ("width", "height"),
    [(w, h) for w in (0.001, 0.5, 72, 612, 2_000, 9_999, 47_000) for h in (0.3, 612, 14_400, 1e6)],
)
def test_every_scale_returned_fits_both_caps(width: float, height: float) -> None:
    """The post-condition over a grid: whenever a scale comes back, pdfium's bitmap fits."""
    try:
        scale = render_scale(width, height)
    except PageTooLargeError:
        return
    width_px, height_px = _bitmap(width, height, scale)
    assert width_px * height_px <= MAX_PIXELS
    assert max(width_px, height_px) <= MAX_SIDE_PX
    assert scale >= MIN_RENDER_DPI / 72


@pytest.mark.parametrize("side", [14_400.0, 200_000.0])
def test_a_page_too_large_to_render_legibly_is_refused(side: float) -> None:
    with pytest.raises(PageTooLargeError):
        render_scale(side, side)


@pytest.mark.parametrize(
    ("width", "height"),
    [(0.0, 792.0), (612.0, -1.0), (-100.0, -100.0), (math.nan, 792.0), (math.inf, 792.0)],
    ids=["zero", "negative", "both-negative", "nan", "inf"],
)
def test_a_degenerate_page_size_is_refused(width: float, height: float) -> None:
    with pytest.raises(PageTooLargeError):
        render_scale(width, height)


def test_the_floor_is_50_dpi_exactly() -> None:
    """A 1-inch page at 50 dpi needs (50 + 1) ** 2 = 2,601 px of rounding headroom."""
    assert MIN_RENDER_DPI == 50
    assert render_scale(72, 72, max_pixels=2_602) >= MIN_RENDER_DPI / 72
    with pytest.raises(PageTooLargeError, match="legibly"):
        render_scale(72, 72, max_pixels=2_600)


def test_render_page_png_keeps_the_bitmap_under_the_cap(tmp_path: Path) -> None:
    path = _square_pdf(tmp_path / "big.pdf", 1_500)
    engine = _Engine(["text"])
    small_cap = functools.partial(render_page_png, max_pixels=2_000_000)

    extract_pdf(path, ocr=_setup(engine), render=small_cap)

    with Image.open(io.BytesIO(engine.calls[0][0])) as page:
        width, height = page.size
    assert width * height <= 2_000_000  # counted as pdfium rounds, each side up
    assert width == pytest.approx(1_414, abs=2)  # 1,500 pt at about 68 dpi, not 200


def test_a_huge_mediabox_is_skipped_without_rendering_and_ingest_completes(
    tmp_path: Path,
    test_db: psycopg.Connection,
    fake_embedder: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 200,000-pt page with a 1x1 image would ask pdfium for ~309 Gpx (~926 GB)."""
    path = _square_pdf(tmp_path / "bomb.pdf", 200_000)
    engine = _Engine([])
    rendered: list[BaseException] = []

    def spy(page: pypdfium2.PdfPage) -> bytes:
        try:
            return render_page_png(page)
        except PageTooLargeError as exc:
            rendered.append(exc)
            raise

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine), render=spy)
    result = ingest_document(
        test_db, embedder=fake_embedder, doc=doc, enrich=False  # type: ignore[arg-type]
    )

    assert len(rendered) == 1, "the default renderer must refuse the page before rendering it"
    assert engine.calls == []
    assert caplog.messages == ["OCR skipped page 1 of 1: PageTooLargeError"]
    assert result.document_id is None  # empty document, ingest completed


def test_a_thin_page_pdf_is_skipped_before_any_render(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A real 0.01 x 3.5e8 pt page through the default renderer: refused by size, never drawn."""
    path = _thin_pdf(tmp_path / "thin.pdf", 0.01, 3.5e8)
    engine = _Engine([])

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine))

    assert engine.calls == []
    assert doc.content == ""
    assert caplog.messages == ["OCR skipped page 1 of 1: PageTooLargeError"]


@pytest.mark.parametrize(
    "failure",
    [OverflowError(), ValueError(), ctypes.ArgumentError(), ZeroDivisionError()],
    ids=["overflow", "value", "ctypes-argument", "zero-division"],
)
def test_a_size_error_from_the_render_path_skips_that_page(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["A", "B"])
    engine = _Engine(["second"])
    calls: list[int] = []

    def fails_once(page: pypdfium2.PdfPage) -> bytes:
        calls.append(1)
        if len(calls) == 1:
            raise failure
        return render_page_png(page)

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine), render=fails_once)

    assert doc.content == "[page 2]\nsecond"
    assert caplog.messages == [f"OCR skipped page 1 of 2: {type(failure).__name__}"]


def test_a_memory_error_while_rendering_skips_that_page(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["A", "B"])
    engine = _Engine(["second"])
    calls: list[int] = []

    def exhausted_once(page: pypdfium2.PdfPage) -> bytes:
        calls.append(1)
        if len(calls) == 1:
            raise MemoryError
        return render_page_png(page)

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine), render=exhausted_once)

    assert doc.content == "[page 2]\nsecond"
    assert caplog.messages == ["OCR skipped page 1 of 2: MemoryError"]


# ---------------------------------------------------------------------------
# per-document deadline (MEDIUM)
# ---------------------------------------------------------------------------


def test_ocr_stops_at_the_document_deadline_and_keeps_the_pages_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _image_only_pdf(tmp_path / "long.pdf", ["A", "B", "C", "D"])
    engine = _Engine(["one", "two", "three"])
    # Read once to set the deadline, then once before each page.
    readings = iter([0.0, 0.0, 300.0, OCR_DEADLINE_S - 30.0, OCR_DEADLINE_S])

    with caplog.at_level(logging.WARNING, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=_setup(engine), clock=lambda: next(readings))

    assert OCR_DEADLINE_S == 600.0
    assert [timeout for _, _, timeout in engine.calls] == [PAGE_TIMEOUT_S, PAGE_TIMEOUT_S, 30.0]
    assert doc.content == "[page 1]\none\n\n[page 2]\ntwo\n\n[page 3]\nthree"
    assert doc.metadata["ocr"] == {"pages": 3, "engine": "fake-ocr"}
    assert caplog.messages == ["OCR stopped at its 600s deadline: 3 of 4 pages tried"]


def test_a_deadline_already_past_reads_no_page(tmp_path: Path) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["A"])
    engine = _Engine([])
    readings = iter([0.0, OCR_DEADLINE_S + 1])

    doc = extract_pdf(path, ocr=_setup(engine), clock=lambda: next(readings))

    assert engine.calls == []
    assert doc.content == ""
    assert "ocr" not in doc.metadata


# ---------------------------------------------------------------------------
# OCR off never opens pdfium; the image probe stays inside the cap (LOW)
# ---------------------------------------------------------------------------


def test_with_ocr_off_pdfium_never_opens_the_file(tmp_path: Path) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["SYNTHETIC"])
    off = OcrSettings(mode="off", lang="eng", max_pages=50)

    def must_not_open(_: Path) -> pypdfium2.PdfDocument:
        raise AssertionError("pdfium opened a PDF while BRAIN_OCR=off")

    doc = extract_pdf(path, ocr=OcrSetup(settings=off, engine=None), open_pdfium=must_not_open)

    assert doc.content == ""


def test_an_enabled_setup_without_an_engine_or_a_missing_binary_says_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _image_only_pdf(tmp_path / "scan.pdf", ["SYNTHETIC"])

    with caplog.at_level(logging.DEBUG, logger="brain.ingest.pdf"):
        doc = extract_pdf(path, ocr=OcrSetup(settings=AUTO, engine=None))

    assert doc.content == ""
    assert caplog.records == []


def test_the_image_probe_reads_only_the_pages_within_the_cap(tmp_path: Path) -> None:
    """The only image is on page 3; with a cap of 2 there is nothing OCR could read."""
    path = _square_pdf(tmp_path / "late.pdf", 612, image_on=(3,), pages=3)
    engine = _Engine(["never"])
    capped = OcrSettings(mode="auto", lang="eng", max_pages=2)

    doc = extract_pdf(path, ocr=_setup(engine, capped))

    assert engine.calls == []
    assert doc.content == ""


def test_an_image_within_the_cap_is_found(tmp_path: Path) -> None:
    path = _square_pdf(tmp_path / "early.pdf", 612, image_on=(2,), pages=3)
    engine = _Engine(["", "page two", ""])

    doc = extract_pdf(path, ocr=_setup(engine, AUTO))

    assert doc.content == "[page 2]\npage two"
