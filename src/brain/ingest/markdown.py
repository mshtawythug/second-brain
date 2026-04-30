"""Markdown extractor — preserves source syntax, surfaces heading list as metadata."""
import re
from pathlib import Path

from . import ExtractedDoc

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def extract_markdown(path: Path) -> ExtractedDoc:
    """Extract an :class:`ExtractedDoc` from a Markdown file on disk.

    The body is preserved verbatim — code fences (``` ``` ```), heading
    markers (``#``), bullet markers (``-``/``*``/``+``), inline emphasis,
    and link URLs all survive into ``ExtractedDoc.content``. Earlier
    versions of this extractor ran a flatten-to-plaintext pass before
    storing, which silently erased code blocks, headings, and list
    structure from the corpus — callers that round-trip through
    ``brain vault export`` lost rendering fidelity for any markdown that
    wasn't pure prose.

    Headings are still surfaced separately in ``metadata["headings"]`` for
    callers that want a structured outline (search snippet builders,
    etc.); the title is the first heading found, falling back to the
    file's stem.
    """
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    headings = [m.group(2).strip() for m in _HEADING_RE.finditer(raw)]
    title = headings[0] if headings else Path(path).stem

    return ExtractedDoc(
        title=title,
        content=raw.strip(),
        content_type="markdown",
        source_path=str(Path(path).resolve()),
        metadata={"headings": headings},
    )
