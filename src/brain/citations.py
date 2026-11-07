"""``[N]`` citation markers in model-written text: drop the dangling ones, list the real ones.

Lifted out of :mod:`brain.ask` (RM-55) so ``brain ask`` and ``brain tags summarize`` keep one rule
for citation integrity: a marker whose number names no source the model was given (``N`` < 1 or
``N`` > ``count``) is removed from the text, and is never reported as a citation.
"""
from __future__ import annotations

import re

#: A ``[N]`` citation marker (1+ digits).
CITATION_RE = re.compile(r"\[(\d+)\]")

#: A ``[N]`` marker plus its leading whitespace, so " foo [99] bar" collapses to "foo bar".
_MARKER_WITH_LEAD_RE = re.compile(r"\s*\[(\d+)\]")


def strip_dangling_citations(text: str, count: int) -> str:
    """``text`` without any out-of-range ``[N]`` marker (and the space before it), stripped.

    In-range markers are kept verbatim, leading space included, so :func:`cited_refs` still
    finds them.
    """

    def _replace(match: re.Match[str]) -> str:
        return match.group(0) if 1 <= int(match.group(1)) <= count else ""

    return _MARKER_WITH_LEAD_RE.sub(_replace, text).strip()


def cited_refs(text: str, count: int) -> tuple[int, ...]:
    """The in-range ``[N]`` numbers in ``text``, de-duplicated, in order of first appearance."""
    refs: list[int] = []
    for match in CITATION_RE.finditer(text):
        ref = int(match.group(1))
        if 1 <= ref <= count and ref not in refs:
            refs.append(ref)
    return tuple(refs)
