"""``timeline._budget_doc_summaries`` still behaves exactly as it did (F2).

The function's packing loop was extracted into
:func:`brain.token_budget.pack_greedy` so ``brain recall`` and ``brain
timeline`` share one budgeter. An extraction like that is only safe if the
output is unchanged, and "the existing tests still pass" is weak evidence —
they cover five hand-picked cases.

So this module keeps a **verbatim copy of the pre-extraction implementation**
and asserts the live function agrees with it across a wide space of inputs,
including boundaries the hand-written tests do not reach (exact-fit budgets,
blank-vs-None summaries, zero-cost rows, oversized leading rows).

If someone later "improves" ``pack_greedy`` into a best-fit packer, this goes
red — which is the point, because best-fit would silently reorder timeline
evidence by size instead of recency.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest

from brain.timeline import _budget_doc_summaries


def _reference_implementation(
    rows: list[tuple[str, str | None]],
    *,
    count_tokens: Callable[[str], int],
    max_tokens: int,
) -> list[str]:
    """The pre-F2 body of ``_budget_doc_summaries``, copied verbatim.

    Do not refactor this to call the production code — it exists precisely to
    be an independent oracle.
    """
    out: list[str] = []
    used = 0
    for title, summary in rows:
        text = summary.strip() if summary and summary.strip() else title
        cost = count_tokens(text)
        if out and used + cost > max_tokens:
            break
        out.append(text)
        used += cost
    return out


def _words(text: str) -> int:
    return len(text.split())


def _chars(text: str) -> int:
    return len(text)


# A deliberately awkward corpus: blank summaries, None summaries,
# whitespace-only summaries, a very long row, a single-word row, and unicode.
_ROWS: list[tuple[str, str | None]] = [
    ("Quarterly Planning", "one two three four five"),
    ("Platform Retro", None),
    ("Budget Review", "   "),
    ("Hiring Sync", "a"),
    ("Offsite Notes", "w " * 40),
    ("Roadmap", "final summary here"),
    ("Unicode Doc 🙂", "emoji 🙂 summary"),
]


@pytest.mark.parametrize("max_tokens", [0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 1000])
@pytest.mark.parametrize("cost", [_words, _chars], ids=["words", "chars"])
def test_delegation_matches_the_reference_across_budgets(
    max_tokens: int, cost: Callable[[str], int]
) -> None:
    """Byte-identical output at every budget, under two different cost models."""
    assert _budget_doc_summaries(
        _ROWS, count_tokens=cost, max_tokens=max_tokens
    ) == _reference_implementation(_ROWS, count_tokens=cost, max_tokens=max_tokens)


@pytest.mark.parametrize("prefix_len", range(len(_ROWS) + 1))
def test_delegation_matches_the_reference_across_row_counts(prefix_len: int) -> None:
    """Every prefix of the corpus, including the empty one."""
    rows = _ROWS[:prefix_len]

    assert _budget_doc_summaries(
        rows, count_tokens=_words, max_tokens=10
    ) == _reference_implementation(rows, count_tokens=_words, max_tokens=10)


def test_exact_fit_boundary_is_unchanged() -> None:
    """The off-by-one most likely to shift in a rewrite: cost == remaining."""
    rows: list[tuple[str, str | None]] = [
        ("A", "one two"),
        ("B", "three four"),
        ("C", "five six"),
    ]

    for budget in (3, 4, 5, 6, 7):
        assert _budget_doc_summaries(
            rows, count_tokens=_words, max_tokens=budget
        ) == _reference_implementation(rows, count_tokens=_words, max_tokens=budget)


def test_first_row_still_included_when_it_alone_exceeds_the_budget() -> None:
    """The ``always_include_first=True`` contract, stated directly."""
    rows: list[tuple[str, str | None]] = [("Big", "w " * 50), ("Small", "x")]

    out = _budget_doc_summaries(rows, count_tokens=_words, max_tokens=2)

    assert len(out) == 1
    assert out == _reference_implementation(rows, count_tokens=_words, max_tokens=2)


def test_summary_wins_over_title_and_blank_summary_falls_back() -> None:
    """The timeline-specific half that stayed in ``timeline.py``."""
    rows: list[tuple[str, str | None]] = [
        ("Fallback Title", None),
        ("Whitespace Title", "   "),
        ("Ignored Title", "real summary"),
    ]

    out = _budget_doc_summaries(rows, count_tokens=_words, max_tokens=1000)

    assert out == ["Fallback Title", "Whitespace Title", "real summary"]


def test_zero_cost_rows_do_not_pack_unboundedly() -> None:
    """Behaviour DIVERGES from the reference here — deliberately, and safely.

    The old inline loop had no per-item floor, so a counter returning 0 would
    admit every row regardless of budget. ``pack_greedy`` floors each item at
    1 token. This is the one intentional difference, and it can only ever
    admit *fewer* rows than before, never more — so it cannot overflow a
    budget that previously held.
    """

    def zero(_text: str) -> int:
        return 0

    rows: list[tuple[str, str | None]] = [(f"T{i}", "x") for i in range(100)]

    live = _budget_doc_summaries(rows, count_tokens=zero, max_tokens=10)
    reference = _reference_implementation(rows, count_tokens=zero, max_tokens=10)

    assert len(reference) == 100, "the old loop packed everything — the defect"
    assert len(live) == 10, "the floor bounds it at the budget"
    assert len(live) <= len(reference), "the change can only ever admit fewer"
