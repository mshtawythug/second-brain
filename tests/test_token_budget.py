"""``brain.token_budget`` — the shared budgeter (F2).

Pure logic, no DB and no embedder: every test injects its own ``cost``
callable, which is the whole point of the module taking one. Coverage target
is 95% per the plan.

The two properties worth stating plainly, because both are load-bearing
elsewhere:

- **Packing stops at the first overflow; it does not skip ahead.** Inputs are
  relevance-ordered, so skipping would reorder results by size instead of by
  rank, and it would change ``brain timeline``'s output (which delegates here
  and is gated on byte-identical results).
- **Per-item cost is floored at 1.** A counter returning ``0`` would otherwise
  admit unboundedly many items — the exact unbounded-payload failure this
  module exists to prevent.
"""
from __future__ import annotations

import pytest

from brain.token_budget import Packed, pack_greedy, truncate_to_token_budget


def words(text: str) -> int:
    """Cost = whitespace-delimited word count. Cheap, exact, obvious in asserts."""
    return len(text.split())


def chars(text: str) -> int:
    """Cost = character count. Used where per-character precision matters."""
    return len(text)


# ---------------------------------------------------------------------------
# pack_greedy
# ---------------------------------------------------------------------------


def test_packs_everything_when_the_budget_is_ample() -> None:
    items = ["one two", "three four", "five six"]

    packed = pack_greedy(items, cost=words, budget=100)

    assert packed == Packed(indices=[0, 1, 2], used_tokens=6, dropped=0)


def test_stops_at_the_first_item_that_does_not_fit() -> None:
    """Prefix-greedy, not best-fit."""
    items = ["a a a a", "b b b b", "c c c c"]

    packed = pack_greedy(items, cost=words, budget=9)

    assert packed.indices == [0, 1]
    assert packed.used_tokens == 8
    assert packed.dropped == 1


def test_does_not_skip_ahead_to_a_smaller_later_item() -> None:
    """The regression that would silently reorder results by size, not rank.

    A best-fit packer would drop the big item and take the two small ones.
    That is wrong here: callers pass relevance-ordered input, and ``timeline``
    depends on this exact behaviour for its byte-identical output gate.
    """
    items = ["x", "big big big big big", "y", "z"]

    packed = pack_greedy(items, cost=words, budget=3)

    assert packed.indices == [0], "must stop at the oversized item, not skip it"
    assert packed.dropped == 3


def test_empty_input_yields_an_empty_pack() -> None:
    packed = pack_greedy([], cost=words, budget=100)

    assert packed == Packed(indices=[], used_tokens=0, dropped=0)


def test_first_item_over_budget_is_dropped_by_default() -> None:
    packed = pack_greedy(["way too many words here"], cost=words, budget=2)

    assert packed.indices == []
    assert packed.used_tokens == 0
    assert packed.dropped == 1


def test_always_include_first_admits_an_oversized_leading_item() -> None:
    """The timeline contract: a bundle with no evidence is useless."""
    packed = pack_greedy(
        ["way too many words here", "second"],
        cost=words,
        budget=2,
        always_include_first=True,
    )

    assert packed.indices == [0]
    assert packed.used_tokens == 5
    assert packed.dropped == 1, "the oversized first item still blocks the rest"


@pytest.mark.parametrize("budget", [0, -1, -100])
def test_non_positive_budget_admits_nothing(budget: int) -> None:
    packed = pack_greedy(["anything"], cost=words, budget=budget)

    assert packed.indices == []
    assert packed.dropped == 1


def test_zero_cost_counter_cannot_admit_unbounded_items() -> None:
    """The floor. Without ``max(1, cost(...))`` this would pack all 1000.

    A degenerate counter is not hypothetical — an empty-string item, a stub in
    a test double, or a tokenizer that fails open all produce 0.
    """
    items = ["" for _ in range(1000)]

    packed = pack_greedy(items, cost=lambda _t: 0, budget=10)

    assert len(packed.indices) == 10, "each item must cost at least 1 token"
    assert packed.used_tokens == 10
    assert packed.dropped == 990


def test_indices_are_positions_into_the_input() -> None:
    """Callers map indices back to the rich objects they rendered from."""
    items = ["aa", "bb", "cc"]

    packed = pack_greedy(items, cost=lambda _t: 1, budget=2)

    assert [items[i] for i in packed.indices] == ["aa", "bb"]


def test_packed_is_frozen() -> None:
    packed = pack_greedy(["a"], cost=words, budget=10)

    with pytest.raises(AttributeError):
        packed.used_tokens = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# truncate_to_token_budget
# ---------------------------------------------------------------------------


def test_text_within_budget_is_returned_unchanged() -> None:
    assert truncate_to_token_budget("one two", cost=words, budget=10) == "one two"


def test_truncation_respects_the_budget() -> None:
    text = "abcdefghij"

    out = truncate_to_token_budget(text, cost=chars, budget=4)

    assert out == "abcd"
    assert chars(out) <= 4


def test_truncation_returns_the_longest_affordable_prefix() -> None:
    """Binary search must not stop short of the true boundary."""
    text = "x" * 500

    out = truncate_to_token_budget(text, cost=chars, budget=137)

    assert len(out) == 137


@pytest.mark.parametrize("budget", [0, -1])
def test_non_positive_budget_truncates_to_empty(budget: int) -> None:
    assert truncate_to_token_budget("anything", cost=chars, budget=budget) == ""


def test_empty_text_is_returned_empty() -> None:
    assert truncate_to_token_budget("", cost=chars, budget=10) == ""


def test_truncation_never_splits_a_multibyte_character() -> None:
    """Codepoint-safe: slicing on characters, never on bytes.

    Each emoji is 4 UTF-8 bytes. A byte-oriented truncator would cut one in
    half and produce mojibake (or raise on decode); slicing a ``str`` cannot.
    """
    text = "🙂🙃🙂🙃🙂"

    out = truncate_to_token_budget(text, cost=chars, budget=3)

    assert out == "🙂🙃🙂"
    assert out.encode("utf-8").decode("utf-8") == out, "must round-trip cleanly"


def test_truncation_calls_cost_logarithmically_not_per_character() -> None:
    """The binary search is the reason this is usable on a long passage."""
    calls = 0

    def counting(text: str) -> int:
        nonlocal calls
        calls += 1
        return len(text)

    truncate_to_token_budget("y" * 4096, cost=counting, budget=1000)

    assert calls < 40, f"expected O(log n) cost calls, got {calls}"
