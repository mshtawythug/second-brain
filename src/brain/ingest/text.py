"""Plain-text extractor."""
from pathlib import Path

from . import ExtractedDoc


def extract_text(path: Path) -> ExtractedDoc:
    """Extract an :class:`ExtractedDoc` from a plain-text file on disk."""
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    return ExtractedDoc(
        title=Path(path).stem,
        content=content.strip(),
        content_type="txt",
        source_path=str(Path(path).resolve()),
        metadata={},
    )
