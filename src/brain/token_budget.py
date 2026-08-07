"""Token-budgeted packing and truncation — the shared budgeter (F2).

Three surfaces need "fit as much of this as you can into N tokens": ``brain
recall`` (an agent's context window), ``brain timeline``'s synthesis prompt,
and ``brain audio``'s script prompt. The first two go through here; each had
its own inline loop before.

The module depends on **nothing** — not the DB, not an embedder, not Rich. It
takes a ``cost`` callable, which is the ``count_tokens`` half of the
:class:`~brain.ingest.Embedder` Protocol. That is deliberately the half every
backend implements offline via tiktoken, which is why budgeting keeps working
under ``BRAIN_EMBEDDER=none`` where ``embed()`` is a no-op.

**Packing is prefix-greedy: it STOPS at the first item that does not fit, it
does not skip ahead to a smaller later item.** That is load-bearing, not an
oversight. Callers hand these functions *relevance-ordered* sequences, so
admitting item 9 after rejecting item 4 would silently reorder the result by
size instead of by rank — and it is the exact semantics
:func:`brain.timeline._budget_doc_summaries` had before it delegated here, so
changing it would alter timeline output.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

TokenCost = Callable[[str], int]
"""Measures a string's token cost. In production, ``Embedder.count_tokens``."""


@dataclass(frozen=True)
class Packed:
    """Outcome of :func:`pack_greedy`.

    ``indices`` are positions into the input sequence, in input order, so the
    caller can map back to whatever rich object it rendered from.
    """

    indices: list[int]
    used_tokens: int
    dropped: int


def _floored_cost(text: str, cost: TokenCost) -> int:
    """Token cost of ``text``, never below 1.

    A counter that returns ``0`` — a stub, a degenerate tokenizer, an empty
    string — would otherwise let the packer admit unboundedly many items
    while the running total stayed at zero, which is precisely the
    unbounded-payload failure this module exists to prevent.
    """
    return max(1, cost(text))


def pack_greedy(
    rendered: Sequence[str],
    *,
    cost: TokenCost,
    budget: int,
    always_include_first: bool = False,
) -> Packed:
    """Pack a prefix of ``rendered`` into ``budget`` tokens.

    Walks in order and stops at the first item that would overflow (see the
    module docstring for why it stops rather than skips). Returns the indices
    kept, the tokens they consume, and how many items were left behind.

    ``always_include_first=True`` admits ``rendered[0]`` even when it alone
    exceeds the budget, so the result is never empty for a non-empty input.
    That is the timeline synthesis contract — a bundle with no evidence in it
    is useless, and one oversized summary beats nothing. ``brain recall``
    passes ``False`` because it has a better answer for that case: truncate
    the single passage to fit (see :func:`truncate_to_token_budget`) rather
    than blow the budget it promised the caller.

    A non-positive ``budget`` admits nothing (or exactly the first item under
    ``always_include_first``).
    """
    kept: list[int] = []
    used = 0
    for index, text in enumerate(rendered):
        item_cost = _floored_cost(text, cost)
        if kept and used + item_cost > budget:
            break
        if not kept and item_cost > budget and not always_include_first:
            break
        kept.append(index)
        used += item_cost
    return Packed(
        indices=kept, used_tokens=used, dropped=len(rendered) - len(kept)
    )


def truncate_to_token_budget(text: str, *, cost: TokenCost, budget: int) -> str:
    """Return the longest prefix of ``text`` costing at most ``budget`` tokens.

    Binary-searches on **character** length, not bytes: Python ``str`` slicing
    is codepoint-safe, so a prefix can never split a multi-byte character and
    produce mojibake. ``cost`` is called O(log n) times rather than once per
    character.

    Assumes cost is monotonic in prefix length — true of every real tokenizer,
    and the search degrades to "a slightly shorter prefix" rather than
    anything unsafe if it is not.

    A non-positive ``budget``, or a budget too small for even one character,
    yields ``""``.
    """
    if budget <= 0 or not text:
        return ""
    if cost(text) <= budget:
        return text

    # Invariant: ``low`` is always a known-affordable length, ``high`` a
    # bound on the answer. The empty prefix always fits, so low=0 is valid.
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if cost(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]
