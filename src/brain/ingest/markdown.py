"""Markdown extractor — flattens to plain text, preserves heading list in metadata."""
import re
from pathlib import Path

from . import ExtractedDoc

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_INLINE_STRIP = re.compile(r"[*_`]+")


def extract_markdown(path: Path) -> ExtractedDoc:
    """Extract an :class:`ExtractedDoc` from a Markdown file on disk."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    headings = [m.group(2).strip() for m in _HEADING_RE.finditer(raw)]

    text = _to_plain(raw)
    title = headings[0] if headings else Path(path).stem

    return ExtractedDoc(
        title=title,
        content=text.strip(),
        content_type="markdown",
        source_path=str(Path(path).resolve()),
        metadata={"headings": headings},
    )


def _to_plain(md: str) -> str:
    """Strip markdown syntax but preserve text + paragraph breaks."""
    out_lines: list[str] = []
    for raw_line in md.splitlines():
        line = _HEADING_RE.sub(lambda m: m.group(2), raw_line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)              # bullets
        line = re.sub(r"^\s*\d+\.\s+", "", line)              # numbered
        line = _INLINE_STRIP.sub("", line)                     # inline emphasis
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)  # links → text
        out_lines.append(line)
    return "\n".join(out_lines)
