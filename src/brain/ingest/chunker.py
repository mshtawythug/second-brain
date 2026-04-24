"""Paragraph-aware text chunker with token budget and overlap."""
import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    """A single chunk of text produced by :func:`chunk_text`."""

    index: int
    content: str


_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


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
    2. Greedily pack paragraphs into a chunk until the next would exceed target_tokens.
    3. If a single paragraph exceeds target_tokens, split it on sentence boundaries.
    4. Add overlap_tokens worth of trailing content from chunk N onto the start of chunk N+1.
    """
    text = text.strip()
    if not text:
        return []

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
        chunks_text = _add_overlap(chunks_text, overlap_tokens, count_tokens)

    return [Chunk(index=i, content=c) for i, c in enumerate(chunks_text)]


def _split_long_paragraph(
    para: str, target_tokens: int, count_tokens: Callable[[str], int]
) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(para) if s.strip()]
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for sent in sentences:
        sent_tokens = count_tokens(sent)
        if current and current_tokens + sent_tokens > target_tokens:
            pieces.append(" ".join(current))
            current = []
            current_tokens = 0
        current.append(sent)
        current_tokens += sent_tokens
    if current:
        pieces.append(" ".join(current))
    return pieces


def _add_overlap(
    chunks: list[str], overlap_tokens: int, count_tokens: Callable[[str], int]
) -> list[str]:
    out = [chunks[0]]
    for prev, cur in zip(chunks, chunks[1:], strict=False):
        tail = _take_tail_tokens(prev, overlap_tokens, count_tokens)
        out.append(tail + "\n\n" + cur if tail else cur)
    return out


def _take_tail_tokens(
    text: str, n_tokens: int, count_tokens: Callable[[str], int]
) -> str:
    """Return the last ~n_tokens worth of text, paragraph-aligned where possible."""
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]
    selected: list[str] = []
    total = 0
    for para in reversed(paragraphs):
        para_tokens = count_tokens(para)
        if total + para_tokens > n_tokens and selected:
            break
        selected.insert(0, para)
        total += para_tokens
    return "\n\n".join(selected)
