"""Terminal-safe text for titles and labels that reach a terminal: one line, no control codes.

Lifted out of :mod:`brain.ingest.github_docs` (RM-31), which re-exports it, when
RM-36's Apple Notes titles and folder names needed the same rule.
"""
from __future__ import annotations

import unicodedata

__all__ = ["one_line"]


def one_line(text: str) -> str:
    """``text`` on one line with every control and format character removed.

    A title is someone else's text and reaches the terminal (``--dry-run``, the
    document lists), where an ESC or other Unicode category ``C*`` character
    could drive the terminal. Whitespace runs collapse to one space first.
    """
    collapsed = " ".join(text.split())
    return "".join(ch for ch in collapsed if not unicodedata.category(ch).startswith("C"))
