"""Unit tests for the markdown extractor."""
from pathlib import Path

from brain.ingest.markdown import extract_markdown


def test_extracts_headings_and_body(fixtures_dir: Path) -> None:
    doc = extract_markdown(fixtures_dir / "sample.md")
    assert "Heading One" in doc.content
    assert "Body paragraph" in doc.content
    assert "bullet one" in doc.content
    assert doc.content_type == "markdown"
    assert doc.title == "Heading One"  # first H1 wins
    assert doc.metadata.get("headings") == ["Heading One", "Heading Two"]
