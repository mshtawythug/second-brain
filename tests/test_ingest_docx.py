"""Unit tests for the DOCX extractor."""
from pathlib import Path

from brain.ingest.docx import extract_docx


def test_extracts_docx_paragraphs_and_tables(fixtures_dir: Path) -> None:
    doc = extract_docx(fixtures_dir / "sample.docx")
    assert "Sample Heading" in doc.content
    assert "First paragraph of body text" in doc.content
    assert "Second paragraph here" in doc.content
    assert "A1" in doc.content and "B2" in doc.content
    assert doc.content_type == "docx"
    assert doc.title == "Sample Heading"


def test_title_falls_back_to_stem_when_no_heading(fixtures_dir: Path) -> None:
    doc = extract_docx(fixtures_dir / "sample_no_heading.docx")
    assert doc.title == "sample_no_heading"
    assert "Just a plain paragraph" in doc.content
    assert doc.content_type == "docx"
