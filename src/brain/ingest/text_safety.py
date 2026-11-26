"""Terminal-safe text for titles and labels that reach a terminal: one line, no control codes.

Lifted out of :mod:`brain.ingest.github_docs` (RM-31), which re-exports it, when
RM-36's Apple Notes titles and folder names needed the same rule. :func:`encodes_utf8`
moved here from :mod:`brain.ui.ingest_schemas` when
``brain ingest-stdin --metadata`` needed the same strict check (PR-C1 review rev-137b).
"""
from __future__ import annotations

import unicodedata

__all__ = ["encodes_utf8", "one_line"]


def one_line(text: str) -> str:
    """``text`` on one line with every control and format character removed.

    A title is someone else's text and reaches the terminal (``--dry-run``, the
    document lists), where an ESC or other Unicode category ``C*`` character
    could drive the terminal. Whitespace runs collapse to one space first.
    """
    collapsed = " ".join(text.split())
    return "".join(ch for ch in collapsed if not unicodedata.category(ch).startswith("C"))


def encodes_utf8(value: str) -> bool:
    """Strict UTF-8: a lone surrogate (valid JSON, ``"\\ud800"``) is refused here.

    Accepted, it would fail later at the database, which cannot store it. In ``brain
    ui``'s ingest job that surfaced as the "may already have been saved" message for a
    document that never was.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
