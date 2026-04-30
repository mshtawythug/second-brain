"""Paragraph-aware text chunker with token budget and overlap."""
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Chunk:
    """A single chunk of text produced by :func:`chunk_text`."""

    index: int
    content: str


_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_LINE_SPLIT = re.compile(r"\n")
_WHITESPACE_SPLIT = re.compile(r"\s+")


def chunk_text(
    text: str,
    *,
    target_tokens: int = 600,
    overlap_tokens: int = 100,
    count_tokens: Callable[[str], int],
) -> list[Chunk]:
    """Split text into paragraph-aware chunks under a token budget.

    Strategy:
    1. Split on blank lines (paragraphs).
    2. Greedily pack paragraphs into a chunk until the next would exceed
       ``target_tokens``.
    3. If a single paragraph exceeds ``target_tokens``, split it via a
       fallback chain: sentence terminators → single newlines → whitespace
       → characters.
    4. Add ``overlap_tokens`` worth of trailing content from chunk N onto
       chunk N+1, capped so no chunk exceeds ``target_tokens + overlap_tokens``.

    Every emitted chunk is guaranteed to satisfy
    ``count_tokens(content) <= target_tokens + overlap_tokens``.
    """
    text = text.strip()
    if not text:
        return []

    ceiling = target_tokens + overlap_tokens
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]
    units: list[str] = []
    for para in paragraphs:
        if count_tokens(para) <= target_tokens:
            units.append(para)
        else:
            units.extend(_split_long_paragraph(para, target_tokens, count_tokens))

    chunks_text: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = count_tokens(unit)
        if current and current_tokens + unit_tokens > target_tokens:
            chunks_text.append("\n\n".join(current))
            current = []
            current_tokens = 0
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        chunks_text.append("\n\n".join(current))

    if overlap_tokens > 0 and len(chunks_text) > 1:
        chunks_text = _add_overlap(
            chunks_text, overlap_tokens, count_tokens, ceiling=ceiling
        )

    # Defensive backstop: any chunk somehow over the ceiling gets hard-split.
    # The fallback chain above should make this branch unreachable.
    final: list[str] = []
    for c in chunks_text:
        if count_tokens(c) <= ceiling:
            final.append(c)
        else:  # pragma: no cover - defensive
            logger.warning(
                "chunker backstop fired: chunk had %d tokens, ceiling=%d",
                count_tokens(c),
                ceiling,
            )
            final.extend(_split_long_paragraph(c, target_tokens, count_tokens))

    return [Chunk(index=i, content=c) for i, c in enumerate(final)]


def _split_long_paragraph(
    para: str, target_tokens: int, count_tokens: Callable[[str], int]
) -> list[str]:
    """Split an oversized paragraph into pieces each ``<= target_tokens``.

    Cascades through progressively finer separators; each step only fires when
    the previous step left a piece over budget, so well-formed prose flows
    through the sentence-only fast path unchanged.
    """
    pieces = _pack_split(para, _SENTENCE_SPLIT, target_tokens, count_tokens, joiner=" ")
    pieces = _refine(pieces, _LINE_SPLIT, target_tokens, count_tokens, joiner="\n")
    pieces = _refine(pieces, _WHITESPACE_SPLIT, target_tokens, count_tokens, joiner=" ")
    out: list[str] = []
    for piece in pieces:
        if count_tokens(piece) <= target_tokens:
            out.append(piece)
        else:
            out.extend(_split_by_chars(piece, target_tokens, count_tokens))
    return out


def _pack_split(
    text: str,
    pattern: re.Pattern[str],
    target_tokens: int,
    count_tokens: Callable[[str], int],
    *,
    joiner: str,
) -> list[str]:
    """Split ``text`` by ``pattern``, then greedily pack parts into pieces.

    Each emitted piece tries to stay ``<= target_tokens``. A part that is
    individually larger than ``target_tokens`` is emitted alone; the caller is
    expected to refine it with a finer split.
    """
    parts = [p.strip() for p in pattern.split(text) if p.strip()]
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for part in parts:
        part_tokens = count_tokens(part)
        if current and current_tokens + part_tokens > target_tokens:
            pieces.append(joiner.join(current))
            current = []
            current_tokens = 0
        current.append(part)
        current_tokens += part_tokens
    if current:
        pieces.append(joiner.join(current))
    return pieces


def _refine(
    pieces: list[str],
    pattern: re.Pattern[str],
    target_tokens: int,
    count_tokens: Callable[[str], int],
    *,
    joiner: str,
) -> list[str]:
    """Re-split any piece that is still over budget using a finer pattern."""
    out: list[str] = []
    for piece in pieces:
        if count_tokens(piece) <= target_tokens:
            out.append(piece)
        else:
            out.extend(
                _pack_split(piece, pattern, target_tokens, count_tokens, joiner=joiner)
            )
    return out


def _split_by_chars(
    text: str, target_tokens: int, count_tokens: Callable[[str], int]
) -> list[str]:
    """Last-resort: split a single whitespace-free blob by character count.

    Used when none of sentence/newline/whitespace splitting reduced a piece
    below ``target_tokens`` — typical for base64 payloads or minified JSON.
    Callers only invoke this when the input is already over budget.
    """
    total_tokens = count_tokens(text)
    avg_chars = max(1, len(text) // total_tokens)
    char_budget = max(1, int(target_tokens * avg_chars * 0.9))
    pieces: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        end = min(n, i + char_budget)
        piece = text[i:end]
        # Token estimate may overshoot; iteratively shrink until under budget.
        while count_tokens(piece) > target_tokens and end - i > 1:
            shrink = max(1, (end - i) // 8)
            end -= shrink
            piece = text[i:end]
        pieces.append(piece)
        i = end
    return pieces


def _add_overlap(
    chunks: list[str],
    overlap_tokens: int,
    count_tokens: Callable[[str], int],
    *,
    ceiling: int,
) -> list[str]:
    """Prepend a tail of the previous chunk onto each subsequent chunk.

    The prepended tail is bounded so that
    ``count_tokens(combined) <= ceiling``. If the chunk is already at the
    ceiling, no overlap is added.
    """
    out = [chunks[0]]
    for prev, cur in zip(chunks, chunks[1:], strict=False):
        cur_tokens = count_tokens(cur)
        room = ceiling - cur_tokens
        if room <= 0:  # pragma: no cover - defensive
            out.append(cur)
            continue
        budget = min(overlap_tokens, room)
        tail = _take_tail_tokens(prev, budget, count_tokens)
        if not tail:  # pragma: no cover - defensive
            out.append(cur)
            continue
        combined = tail + "\n\n" + cur
        if count_tokens(combined) > ceiling:  # pragma: no cover - defensive
            # Tail estimate overshot; drop overlap rather than violate ceiling.
            out.append(cur)
        else:
            out.append(combined)
    return out


def _take_tail_tokens(
    text: str, n_tokens: int, count_tokens: Callable[[str], int]
) -> str:
    """Return at most ~``n_tokens`` worth of trailing content.

    Paragraph-aligned where possible; falls back to hard-splitting the last
    paragraph if it alone is larger than ``n_tokens``.
    """
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]
    selected: list[str] = []
    total = 0
    for para in reversed(paragraphs):
        para_tokens = count_tokens(para)
        if selected and total + para_tokens > n_tokens:
            break
        if not selected and para_tokens > n_tokens:
            atoms = _split_long_paragraph(para, n_tokens, count_tokens)
            return atoms[-1] if atoms else ""
        selected.insert(0, para)
        total += para_tokens
    return "\n\n".join(selected)
