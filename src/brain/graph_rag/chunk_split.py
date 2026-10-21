"""Re-split a concept-extraction chunk whose model reply was cut off at ``num_predict``.

On an entity-dense chunk ``llama3.1:8b`` lists one entry per MENTION rather than
per entity, so the JSON reply runs out of ``num_predict`` on both attempts and
:func:`brain.chat.chat_json_with_client` raises
:class:`~brain.errors.ChatOutputTruncated`. Salvaging the complete prefix of that
reply alone would keep the head of the chunk and lose every entity after the cut,
which is the chunk's tail. So a truncated piece is first split in two at the
paragraph / sentence / line / word boundary nearest its token midpoint
(:func:`split_in_half`, never more than two pieces), and each half is extracted
on its own. A piece is salvaged instead only at the bound: it
has already been split :data:`MAX_SPLIT_DEPTH` times, it is under
:data:`MIN_SPLIT_TOKENS`, or it will not split into two.

Cost of the bound: a truncated call has already generated up to
``EXTRACT_NUM_PREDICT * (1 + NUM_PREDICT_RETRY_FACTOR)`` tokens, and each level
of splitting can repeat that per piece. Depth 2 caps one pathological chunk at
1 + 2 + 4 = 7 such calls. Chunks whose reply parses never reach this module's
split path, so their cost and their output are unchanged.

Model output is document-derived, so nothing here logs; the caller logs the
returned counts.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import ChatOutputTruncated, EnrichmentError, OllamaUnavailable
from ..json_salvage import salvage_array_items

#: How many times a truncated piece may be halved before its prefix is salvaged:
#: a 1,600-token chunk bottoms out at four pieces of roughly 400 tokens.
MAX_SPLIT_DEPTH = 2

#: A truncated piece under this many ``cl100k_base`` tokens is salvaged rather
#: than split: halves this small barely help a model that already overran on them.
MIN_SPLIT_TOKENS = 200

#: Split boundaries, coarsest first: paragraph, sentence end, line, word.
_BOUNDARIES = (
    re.compile(r"\n[ \t]*\n\s*"),
    re.compile(r"(?<=[.!?])\s+"),
    re.compile(r"\n\s*"),
    re.compile(r"\s+"),
)

#: A boundary is used only when each side keeps at least this share of the
#: tokens; a lopsided paragraph cut falls through to a finer boundary.
_MIN_SIDE_SHARE = 0.25

#: The reply key holding the entity list (``_EXTRACT_SYSTEM_PROMPT``'s schema).
_ENTITIES_KEY = "entities"


@dataclass(frozen=True)
class ChunkExtraction:
    """The raw entity entries for one chunk, plus what it took to get them."""

    entries: tuple[Any, ...]
    #: Truncated pieces that were split (the chunk itself counts as one).
    pieces_split: int = 0
    #: Truncated pieces at the bound whose complete prefix was salvaged.
    pieces_salvaged: int = 0
    #: Entries recovered by those salvages.
    entries_salvaged: int = 0
    #: Sub-pieces skipped because their reply was malformed (not truncated).
    pieces_failed: int = 0

    @property
    def truncated(self) -> bool:
        """True when any piece of the chunk hit ``num_predict``."""
        return self.pieces_split > 0 or self.pieces_salvaged > 0


@dataclass
class _Tally:
    pieces_split: int = 0
    pieces_salvaged: int = 0
    entries_salvaged: int = 0
    pieces_failed: int = 0
    entries: list[Any] = field(default_factory=list)


def extract_splitting_on_truncation(
    text: str,
    *,
    extract_entities: Callable[[str], list[Any]],
    split: Callable[[str], list[str]],
    count_tokens: Callable[[str], int],
    max_depth: int = MAX_SPLIT_DEPTH,
    min_tokens: int = MIN_SPLIT_TOKENS,
) -> ChunkExtraction:
    """Extract one chunk's raw entries, splitting it while its reply truncates.

    The chunk's own failures keep the caller's policy: :class:`OllamaUnavailable`
    and a malformed (non-truncated) reply propagate unchanged. Below the chunk,
    a malformed sub-piece is skipped and counted so its siblings still
    contribute; :class:`OllamaUnavailable` from any piece still propagates.
    """
    tally = _Tally()

    def on_truncated(piece: str, exc: ChatOutputTruncated, depth: int) -> None:
        pieces = (
            split(piece)
            if depth < max_depth and count_tokens(piece) >= min_tokens
            else []
        )
        if len(pieces) < 2:
            salvaged = salvage_array_items(exc.partial, _ENTITIES_KEY)
            tally.pieces_salvaged += 1
            tally.entries_salvaged += len(salvaged)
            tally.entries.extend(salvaged)
            return
        tally.pieces_split += 1
        for sub_piece in pieces:
            try:
                extract(sub_piece, depth + 1)
            except OllamaUnavailable:
                raise
            except EnrichmentError:
                tally.pieces_failed += 1

    def extract(piece: str, depth: int) -> None:
        try:
            tally.entries.extend(extract_entities(piece))
        except ChatOutputTruncated as exc:
            on_truncated(piece, exc, depth)

    extract(text, 0)
    return ChunkExtraction(
        entries=tuple(tally.entries),
        pieces_split=tally.pieces_split,
        pieces_salvaged=tally.pieces_salvaged,
        entries_salvaged=tally.entries_salvaged,
        pieces_failed=tally.pieces_failed,
    )


def split_in_half(text: str, *, count_tokens: Callable[[str], int]) -> list[str]:
    """Split ``text`` into at most two pieces whose concatenation is exactly ``text``.

    The cut is the boundary nearest the token midpoint at the coarsest level
    that leaves each side at least :data:`_MIN_SIDE_SHARE` of the tokens --
    paragraph, then sentence, then line, then word -- and the token midpoint
    itself when no boundary qualifies. Never more than two pieces, so the
    recursion's call bound (1 + 2 + 4 at depth 2) holds; nothing is dropped or
    repeated at the cut. Returns ``[text]`` when it cannot be split.
    """
    total = count_tokens(text)
    if total < 2:
        return [text]
    half = total / 2
    for pattern in _BOUNDARIES:
        cuts = [
            m.end()
            for m in pattern.finditer(text)
            if text[: m.start()].strip() and text[m.end() :].strip()
        ]
        if not cuts:
            continue
        cut = _nearest_cut(text, cuts, half, count_tokens)
        head_tokens = count_tokens(text[:cut])
        if min(head_tokens, total - head_tokens) >= total * _MIN_SIDE_SHARE:
            return [text[:cut], text[cut:]]
    cut = _first_index_reaching(text, half, count_tokens)
    return [text[:cut], text[cut:]] if 0 < cut < len(text) else [text]


def _nearest_cut(
    text: str, cuts: list[int], half: float, count_tokens: Callable[[str], int]
) -> int:
    """The cut whose prefix token count is nearest ``half`` (cuts ascending).

    Binary search on the prefix count, which grows with the cut, then the
    better of the two neighbours -- O(log n) tokenizations, not one per cut.
    """
    lo, hi = 0, len(cuts) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if count_tokens(text[: cuts[mid]]) < half:
            lo = mid + 1
        else:
            hi = mid
    neighbours = cuts[max(lo - 1, 0) : lo + 1]
    return min(neighbours, key=lambda c: abs(count_tokens(text[:c]) - half))


def _first_index_reaching(text: str, half: float, count_tokens: Callable[[str], int]) -> int:
    """The smallest character index whose prefix holds at least ``half`` tokens."""
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if count_tokens(text[:mid]) < half:
            lo = mid + 1
        else:
            hi = mid
    return lo
