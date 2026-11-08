"""``[N]`` citation markers in model-written text: drop the dangling ones, list the real ones.

Lifted out of :mod:`brain.ask` (RM-55) so ``brain ask`` and ``brain tags summarize`` keep one rule
for citation integrity: a marker whose number names no source the model was given (``N`` < 1 or
``N`` > ``count``) is removed from the text, and is never reported as a citation.

A citation number is ASCII digits, at most :data:`MAX_REF_DIGITS` of them. Anything else in the
brackets that is still digits (an over-long run, or digits from another script, which ``int``
would accept) is removed as dangling without being converted, so a 5,000-digit marker cannot raise
``ValueError`` past Python's integer-string limit.
"""
from __future__ import annotations

import re

#: The most digits a citation number may have.
MAX_REF_DIGITS = 6

#: A ``[N]`` citation marker: 1 to :data:`MAX_REF_DIGITS` ASCII digits.
CITATION_RE = re.compile(rf"\[([0-9]{{1,{MAX_REF_DIGITS}}})\]")

#: Any bracketed run of digits plus its leading whitespace, so " foo [99] bar" collapses to
#: "foo bar". Wider than :data:`CITATION_RE` on purpose: what it matches and CITATION_RE does not
#: is always dangling.
_MARKER_WITH_LEAD_RE = re.compile(r"\s*\[(\d+)\]")


def strip_dangling_citations(text: str, count: int) -> str:
    """``text`` without any out-of-range ``[N]`` marker (and the space before it), stripped.

    In-range markers are kept verbatim, leading space included, so :func:`cited_refs` still
    finds them.
    """

    def _replace(match: re.Match[str]) -> str:
        digits = match.group(1)
        in_range = CITATION_RE.fullmatch(f"[{digits}]") is not None and 1 <= int(digits) <= count
        return match.group(0) if in_range else ""

    return _MARKER_WITH_LEAD_RE.sub(_replace, text).strip()


def cited_refs(text: str, count: int) -> tuple[int, ...]:
    """The in-range ``[N]`` numbers in ``text``, de-duplicated, in order of first appearance."""
    refs: list[int] = []
    for match in CITATION_RE.finditer(text):
        ref = int(match.group(1))
        if 1 <= ref <= count and ref not in refs:
            refs.append(ref)
    return tuple(refs)
