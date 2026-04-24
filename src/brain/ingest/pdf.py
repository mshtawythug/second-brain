"""PDF extractor — pypdf primary, pdfplumber fallback."""
from pathlib import Path

from pypdf import PdfReader

from . import ExtractedDoc


def extract_pdf(path: Path) -> ExtractedDoc:
    """Extract an :class:`ExtractedDoc` from a PDF file on disk.

    Uses ``pypdf`` as the primary extractor. If no text is recovered (e.g. a
    scanned PDF with images only), falls back to ``pdfplumber``, which handles
    some layouts ``pypdf`` misses. Strips repeated header/footer lines that
    appear on a majority of pages.
    """
    reader = PdfReader(str(path))
    pages_text: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages_text.append(text.strip())

    full_text = _strip_repeated_lines("\n\n".join(pages_text))

    if not full_text.strip():  # pragma: no cover - only triggered by image-only PDFs
        full_text = _fallback_pdfplumber(path)

    return ExtractedDoc(
        title=Path(path).stem,
        content=full_text.strip(),
        content_type="pdf",
        source_path=str(Path(path).resolve()),
        metadata={"page_count": len(reader.pages)},
    )


def _fallback_pdfplumber(path: Path) -> str:  # pragma: no cover - image-only PDFs
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
