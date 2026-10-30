"""PDF extractor — pypdf primary, pdfplumber fallback, OCR for image-only pages (RM-32)."""
import io
import logging
import os
from collections.abc import Callable
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

PageRenderer = Callable[[pypdfium2.PdfPage], bytes]
PdfiumOpener = Callable[[Path], pypdfium2.PdfDocument]


def render_page_png(page: pypdfium2.PdfPage) -> bytes:
    """Render ``page`` at :data:`~brain.ingest.ocr.RENDER_DPI` and encode it as PNG."""
    image = page.render(scale=RENDER_DPI / 72).to_pil()
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
) -> ExtractedDoc:
    """Extract an :class:`ExtractedDoc` from a PDF file on disk.

    Uses ``pypdf`` as the primary extractor. If no text is recovered, falls back
    to ``pdfplumber``, which handles some layouts ``pypdf`` misses. Strips
    repeated header/footer lines that appear on a majority of pages.

    When the text still averages under :data:`MIN_CHARS_PER_PAGE` per page and a
    page carries an image, the pages are OCR'd (:func:`_ocr_pages`). ``ocr`` is
    resolved from ``os.environ`` only then, so a born-digital PDF never reads the
    ``BRAIN_OCR*`` knobs; ``ocr``, ``render`` and ``open_pdfium`` are test seams.
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
        recognised = _ocr_fallback(path, ocr, render=render, open_pdfium=open_pdfium)
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
) -> tuple[str, dict[str, Any]] | None:
    """OCR an image-bearing PDF: ``(text, metadata.ocr)``, or ``None`` when nothing was read."""
    try:
        document = open_pdfium(path)
    except pypdfium2.PdfiumError as exc:
        logger.warning("OCR could not open a PDF: %s", describe_failure(exc))
        return None
    try:
        if not _has_image(document):
            return None
        setup = ocr if ocr is not None else resolve_ocr(os.environ)
        if setup.engine is None:
            if setup.missing_binary:
                logger.warning(TESSERACT_HINT)
            return None
        return _ocr_pages(document, setup.engine, setup, render)
    finally:
        document.close()


def _has_image(document: pypdfium2.PdfDocument) -> bool:
    """True when any page carries an image object; a page with none has nothing to recognise."""
    for page in document:
        if next(page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,)), None) is not None:
            return True
    return False


def _ocr_pages(
    document: pypdfium2.PdfDocument,
    engine: OcrEngine,
    setup: OcrSetup,
    render: PageRenderer,
) -> tuple[str, dict[str, Any]] | None:
    """Render and recognise pages up to the cap; a failed page is skipped, class-only."""
    total = len(document)
    limit = min(total, setup.settings.max_pages)
    blocks: list[str] = []
    for index in range(limit):
        try:
            png = render(document[index])
            text = engine.image_to_text(png, lang=setup.settings.lang, timeout=PAGE_TIMEOUT_S)
        except (OcrError, pypdfium2.PdfiumError, OSError) as exc:
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
