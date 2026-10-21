"""Re-split a concept-extraction chunk whose model reply was cut off at ``num_predict``.

On an entity-dense chunk ``llama3.1:8b`` lists one entry per MENTION rather than
per entity, so the JSON reply runs out of ``num_predict`` on both attempts and
:func:`brain.chat.chat_json_with_client` raises
:class:`~brain.errors.ChatOutputTruncated`. Salvaging the complete prefix of that
reply alone would keep the head of the chunk and lose every entity after the cut,
which is the chunk's tail. So a truncated piece is first split roughly in half at
paragraph / sentence boundaries (the ingest chunker's fallback chain), and each
half is extracted on its own. A piece is salvaged instead only at the bound: it
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
    """Split ``text`` into about two pieces at paragraph / sentence boundaries.

    Uses the ingest chunker's packing and fallback chain with a target of half
    the text and no overlap, so the pieces partition the text. Greedy packing can
    yield a third, small piece, which is harmless here.
    """
    # Late import, as in ``extract.py``: keeps :mod:`brain.graph_rag` import-cheap.
    from ..ingest.chunker import chunk_text

    half = max(1, (count_tokens(text) + 1) // 2)
    chunks = chunk_text(text, target_tokens=half, overlap_tokens=0, count_tokens=count_tokens)
    return [chunk.content for chunk in chunks]
