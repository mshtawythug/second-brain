"""Generic stdin ingester for Claude-orchestrated sources (Krisp, Slack, etc)."""
from typing import Any

from . import ExtractedDoc


def make_doc(
    *,
    content: str,
    title: str,
    content_type: str,
    source_path: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ExtractedDoc:
    """Build an :class:`ExtractedDoc` from raw stdin content plus metadata."""
    return ExtractedDoc(
        title=title,
        content=content.strip(),
        content_type=content_type,
        source_path=source_path,
        metadata=metadata or {},
    )
