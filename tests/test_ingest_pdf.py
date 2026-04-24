"""Unit tests for the PDF extractor."""
from pathlib import Path

from brain.ingest.pdf import _strip_repeated_lines, extract_pdf


def test_extracts_pdf_text(fixtures_dir: Path) -> None:
    doc = extract_pdf(fixtures_dir / "sample.pdf")
    assert "Hello from page one" in doc.content
    assert "Page two content here" in doc.content
    assert doc.content_type == "pdf"
    assert doc.title == "sample"
    assert doc.metadata.get("page_count") == 2


def test_strip_repeated_lines_removes_headers_and_footers() -> None:
    """Lines appearing on >50% of pages (headers/footers) are stripped."""
    pages = [
        "ACME Report\nPage 1 content here\nFooter 2026",
        "ACME Report\nPage 2 content here\nFooter 2026",
        "ACME Report\nPage 3 content here\nFooter 2026",
    ]
    cleaned = _strip_repeated_lines("\n\n".join(pages))
    assert "ACME Report" not in cleaned
    assert "Footer 2026" not in cleaned
    assert "Page 1 content here" in cleaned
    assert "Page 2 content here" in cleaned
    assert "Page 3 content here" in cleaned


def test_strip_repeated_lines_keeps_unique_lines() -> None:
    """If no line repeats across a majority of pages, text is unchanged."""
    pages = ["alpha", "beta", "gamma"]
    text = "\n\n".join(pages)
    assert _strip_repeated_lines(text) == text
