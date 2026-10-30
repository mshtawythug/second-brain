"""PDF extractor — pypdf primary, pdfplumber fallback, OCR for image-only pages (RM-32)."""
import ctypes
import io
import logging
import math
import os
import time
from collections.abc import Callable
from itertools import islice
from pathlib import Path
from typing import Any

import pypdfium2
import pypdfium2.raw as pdfium_c
from pypdf import PdfReader

from brain.errors import describe_failure

from . import ExtractedDoc
from .ocr import PAGE_TIMEOUT_S, RENDER_DPI, OcrEngine, OcrError, OcrSetup, resolve_ocr

logger = logging.getLogger(__name__)

#: Below this many characters per page on average, the text layer is treated as missing.
MIN_CHARS_PER_PAGE = 25

#: The hint when a PDF needed OCR and ``BRAIN_OCR=auto`` found no ``tesseract``. The CLI
#: configures no logging, so ``logging.lastResort`` prints it on stderr next to "skipped
#: (empty document)"; ``cli_ingest.py`` (over the ceiling) is not touched for it.
TESSERACT_HINT = (
    "an image-only PDF was not OCR'd: the tesseract binary is not on PATH "
    "(brew install tesseract), or set BRAIN_OCR=off to silence this"
)

#: The most pixels one page's bitmap may hold, counted as pdfium allocates it (each side
#: rounded UP to a whole pixel): A0 at 200 dpi is about 62 Mpx. A page's MediaBox is the
#: untrusted file's to set (200,000 pt square asks for ~309 Gpx), and neither pdfium nor
#: ``to_pil()`` applies Pillow's decompression-bomb limit, so the cap is enforced here.
MAX_PIXELS = 70_000_000
#: The longest side, in pixels, a bitmap may have: tesseract's own limit. A thin page
#: (0.01 x 3.5e8 pt) is small in area but rounds up to a 1 x 972,222,223 px bitmap.
MAX_SIDE_PX = 32_767
#: A page that only fits under :data:`MAX_PIXELS` below 50 dpi is skipped: text that small
#: is not legible to tesseract, so rendering it would cost memory and read nothing.
MIN_RENDER_DPI = 50
#: The total OCR time one document may take. ``brain ui`` runs ingest on one job worker
#: that cannot be cancelled, so 50 pages at the 60 s page limit (or 1,000 at the knob's
#: maximum) must not hold it for hours. Each page's timeout is cut to what remains.
OCR_DEADLINE_S = 600.0

PageRenderer = Callable[[pypdfium2.PdfPage], bytes]
PdfiumOpener = Callable[[Path], pypdfium2.PdfDocument]
Clock = Callable[[], float]


class PageTooLargeError(OcrError):
    """A page would render below :data:`MIN_RENDER_DPI` to fit :data:`MAX_PIXELS`."""


def _fit_scale(width_pt: float, height_pt: float, max_pixels: int) -> float:
    """The largest scale ``s`` with ``(w*s + 1) * (h*s + 1) <= max_pixels``.

    pdfium rounds each side up, so ``ceil(w*s) * ceil(h*s)`` is what it allocates, and that
    is at most ``(w*s + 1) * (h*s + 1)``. Solving that quadratic for ``s`` in the stable
    form (no cancellation for a thin page) keeps the real bitmap under the cap, where
    ``sqrt(max_pixels / (w*h))`` let the rounding push it over: badly, for a thin page.
    """
    budget = max_pixels - 1
    perimeter = width_pt + height_pt
    root = math.sqrt(perimeter * perimeter + 4 * width_pt * height_pt * budget)
    return 2 * budget / (perimeter + root)


def render_scale(width_pt: float, height_pt: float, *, max_pixels: int = MAX_PIXELS) -> float:
    """The render scale for a page of this size: 200 dpi, cut so the bitmap fits both caps.

    The bitmap pdfium allocates (each side rounded up) never has more than ``max_pixels``
    pixels. Raises :class:`PageTooLargeError` when the scale would be below
    :data:`MIN_RENDER_DPI`, when a side of that bitmap would be longer than
    :data:`MAX_SIDE_PX`, or when either side is not a positive finite number (a degenerate
    MediaBox, ``-100 x -100`` included).
    """
    sides_ok = all(math.isfinite(side) and side > 0 for side in (width_pt, height_pt))
    if not sides_ok:
        raise PageTooLargeError("page has no positive finite size")
    scale = min(RENDER_DPI / 72, _fit_scale(width_pt, height_pt, max_pixels))
    if scale < MIN_RENDER_DPI / 72:
        raise PageTooLargeError("page too large to render legibly")
    width_px, height_px = math.ceil(width_pt * scale), math.ceil(height_pt * scale)
    if max(width_px, height_px) > MAX_SIDE_PX:
        raise PageTooLargeError("page side too long to render")
    # No separate ``width_px * height_px > max_pixels`` check: _fit_scale already sizes the
    # scale from the rounded-up bitmap, so that check could never fire (see the grid test in
    # tests/test_ingest_pdf_ocr_limits.py, which asserts the post-condition).
    return scale


def render_page_png(page: pypdfium2.PdfPage, *, max_pixels: int = MAX_PIXELS) -> bytes:
    """Render ``page`` at up to 200 dpi within ``max_pixels``, and encode it as PNG."""
    width, height = page.get_size()
    scale = render_scale(width, height, max_pixels=max_pixels)
    # Grayscale: one byte a pixel instead of three, and tesseract binarises anyway.
    image = page.render(scale=scale, grayscale=True).to_pil()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _open_pdfium(path: Path) -> pypdfium2.PdfDocument:
    return pypdfium2.PdfDocument(str(path))


def extract_pdf(
    path: Path,
    *,
    ocr: OcrSetup | None = None,
    render: PageRenderer = render_page_png,
    open_pdfium: PdfiumOpener = _open_pdfium,
    clock: Clock = time.monotonic,
) -> ExtractedDoc:
    """Extract an :class:`ExtractedDoc` from a PDF file on disk.

    Uses ``pypdf`` as the primary extractor. If no text is recovered, falls back
    to ``pdfplumber``, which handles some layouts ``pypdf`` misses. Strips
    repeated header/footer lines that appear on a majority of pages.

    When the text still averages under :data:`MIN_CHARS_PER_PAGE` per page and a
    page within the cap carries an image, the pages are OCR'd (:func:`_ocr_pages`).
    ``ocr`` is resolved from ``os.environ`` only then, so a PDF with a real text layer
    never reads the ``BRAIN_OCR*`` knobs, and with ``BRAIN_OCR=off`` pdfium never
    opens the file. ``ocr``, ``render``, ``open_pdfium`` and ``clock`` are test seams.
    """
    reader = PdfReader(str(path))
    pages_text: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages_text.append(text.strip())

    full_text = _strip_repeated_lines("\n\n".join(pages_text))

    if not full_text.strip():
        full_text = _fallback_pdfplumber(path)

    page_count = len(reader.pages)
    content = full_text.strip()
    metadata: dict[str, Any] = {"page_count": page_count}
    if len(content) < MIN_CHARS_PER_PAGE * max(page_count, 1):
        recognised = _ocr_fallback(
            path, ocr, render=render, open_pdfium=open_pdfium, clock=clock
        )
        if recognised is not None:
            content, metadata["ocr"] = recognised

    return ExtractedDoc(
        title=Path(path).stem,
        content=content,
        content_type="pdf",
        source_path=str(Path(path).resolve()),
        metadata=metadata,
    )


def _ocr_fallback(
    path: Path,
    ocr: OcrSetup | None,
    *,
    render: PageRenderer,
    open_pdfium: PdfiumOpener,
    clock: Clock,
) -> tuple[str, dict[str, Any]] | None:
    """OCR an image-bearing PDF: ``(text, metadata.ocr)``, or ``None`` when nothing was read."""
    setup = ocr if ocr is not None else resolve_ocr(os.environ)
    if not setup.settings.enabled:
        # Before pdfium: with OCR off, the untrusted file is never handed to a second parser.
        return None
    try:
        document = open_pdfium(path)
    except pypdfium2.PdfiumError as exc:
        logger.warning("OCR could not open a PDF: %s", describe_failure(exc))
        return None
    try:
        if not _has_image(document, setup.settings.max_pages):
            return None
        if setup.engine is None:
            if setup.missing_binary:
                logger.warning(TESSERACT_HINT)
            return None
        return _ocr_pages(document, setup.engine, setup, render, clock)
    finally:
        document.close()


def _has_image(document: pypdfium2.PdfDocument, max_pages: int) -> bool:
    """True when a page within the cap carries an image object; one with none has nothing to read.

    Only the first ``max_pages`` pages are loaded: those are the only ones OCR would render.
    """
    for page in islice(document, max_pages):
        if next(page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,)), None) is not None:
            return True
    return False


def _ocr_pages(
    document: pypdfium2.PdfDocument,
    engine: OcrEngine,
    setup: OcrSetup,
    render: PageRenderer,
    clock: Clock,
) -> tuple[str, dict[str, Any]] | None:
    """Render and recognise pages up to the cap and the deadline; a failed page is skipped."""
    total = len(document)
    limit = min(total, setup.settings.max_pages)
    deadline = clock() + OCR_DEADLINE_S
    blocks: list[str] = []
    for index in range(limit):
        remaining = deadline - clock()
        if remaining <= 0:
            logger.warning(
                "OCR stopped at its %gs deadline: %d of %d pages tried",
                OCR_DEADLINE_S,
                index,
                total,
            )
            break
        try:
            png = render(document[index])
            text = engine.image_to_text(
                png, lang=setup.settings.lang, timeout=min(PAGE_TIMEOUT_S, remaining)
            )
        except (
            OcrError,
            pypdfium2.PdfiumError,
            OSError,
            MemoryError,
            ArithmeticError,
            ValueError,
            ctypes.ArgumentError,
        ) as exc:
            # The last three are what a pathological page size can raise on its way into
            # pdfium's ctypes layer (a side past a C int is an ArgumentError); each skips
            # the page like any other failure.
            # Class only: an OcrError never carries recognised text, and nothing else may.
            logger.warning("OCR skipped page %d of %d: %s", index + 1, total, describe_failure(exc))
            continue
        if text.strip():
            blocks.append(f"[page {index + 1}]\n{text.strip()}")
    if limit < total:
        logger.info("OCR stopped at BRAIN_OCR_MAX_PAGES: %d of %d pages", limit, total)
    if not blocks:
        return None
    return "\n\n".join(blocks), {"pages": len(blocks), "engine": engine.name}


def _fallback_pdfplumber(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        return "\n\n".join((p.extract_text() or "").strip() for p in pdf.pages)


def _strip_repeated_lines(text: str) -> str:
    """Remove header/footer lines that appear on >50% of pages."""
    pages = text.split("\n\n")
    if len(pages) < 3:
        return text
    line_counts: dict[str, int] = {}
    for page in pages:
        for line in {ln.strip() for ln in page.splitlines() if ln.strip()}:
            line_counts[line] = line_counts.get(line, 0) + 1
    threshold = len(pages) // 2
    repeated = {line for line, count in line_counts.items() if count > threshold}
    if not repeated:
        return text
    cleaned_pages = [
        "\n".join(ln for ln in page.splitlines() if ln.strip() not in repeated)
        for page in pages
    ]
    return "\n\n".join(cleaned_pages)
