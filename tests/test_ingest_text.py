"""Unit tests for the plain-text extractor."""
from pathlib import Path

from brain.ingest.text import extract_text


def test_extracts_plain_text(fixtures_dir: Path) -> None:
    doc = extract_text(fixtures_dir / "sample.txt")
    assert "First paragraph." in doc.content
    assert "Second paragraph" in doc.content
    assert doc.content_type == "txt"
    assert doc.title == "sample"
    assert str(fixtures_dir / "sample.txt") == doc.source_path
