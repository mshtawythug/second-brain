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
    assert doc.metadata.get("headings") == ["Heading One", "Heading Two", "Heading Three"]


def test_preserves_code_fences(fixtures_dir: Path) -> None:
    """Triple-backtick code blocks must survive into doc.content.

    Regression: the previous flatten-to-plaintext pass stripped every
    backtick (single AND triple), so ``` ```erb / ``` ``` ``` markers were
    erased and the language hint plus the closing fence both became dust.
    Vault export then served unfenced HTML/ERB to Quartz, whose markdown
    pipeline treated the would-be code body as inline content and crashed
    on the first malformed `href`.
    """
    doc = extract_markdown(fixtures_dir / "sample.md")
    assert "```erb" in doc.content
    assert "```" in doc.content.split("```erb", 1)[1]  # closing fence too
    assert "<%= name %>" in doc.content  # body inside the fence intact


def test_preserves_heading_markers_in_body(fixtures_dir: Path) -> None:
    """Heading ``#`` markers must survive into doc.content.

    The headings list in metadata is a separate structured copy; the body
    needs the markers too so vault export reproduces a renderable markdown
    file rather than a wall of unstructured prose.
    """
    doc = extract_markdown(fixtures_dir / "sample.md")
    assert "# Heading One" in doc.content
    assert "## Heading Two" in doc.content


def test_preserves_bullet_markers(fixtures_dir: Path) -> None:
    """Bullet ``- ``/``* ``/``+ `` markers must survive into doc.content."""
    doc = extract_markdown(fixtures_dir / "sample.md")
    assert "- bullet one" in doc.content
    assert "- bullet two" in doc.content


def test_preserves_inline_emphasis(fixtures_dir: Path) -> None:
    """Inline ``*``/``_`` emphasis must survive — they were stripped before."""
    doc = extract_markdown(fixtures_dir / "sample.md")
    assert "**bold**" in doc.content
    assert "*italic*" in doc.content


def test_preserves_link_urls(fixtures_dir: Path) -> None:
    """Markdown link targets must survive — used to be flattened to bare text."""
    doc = extract_markdown(fixtures_dir / "sample.md")
    assert "[the docs](https://example.com/docs)" in doc.content
