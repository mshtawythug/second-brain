"""Tests for :mod:`brain.graph_rag.chunk_split` — re-splitting a concept-extraction
chunk whose model reply was cut off at ``num_predict``.

Pure DI: the model call, the splitter and the token counter are plain fakes
(``count_tokens`` = word count), so the recursion bounds are exact. All values
are synthetic (no PII).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from brain.errors import ChatOutputTruncated, EnrichmentError, OllamaUnavailable
from brain.graph_rag.chunk_split import (
    MAX_SPLIT_DEPTH,
    MIN_SPLIT_TOKENS,
    extract_splitting_on_truncation,
    split_in_half,
)


def _words(text: str) -> int:
    return len(text.split())


def _halve(text: str) -> list[str]:
    words = text.split()
    mid = len(words) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


def _truncation(partial: str = '{"entities": [{"name": "W"}, {"na') -> ChatOutputTruncated:
    return ChatOutputTruncated("chat failed after 2 attempts: x", partial=partial, num_predict=2048)


def _always_truncates(calls: list[str]) -> Callable[[str], list[Any]]:
    def extract(text: str) -> list[Any]:
        calls.append(text)
        raise _truncation()

    return extract


def _raised(call: Callable[[], object]) -> Exception | None:
    """The exception ``call`` raised, or None -- so a missing raise is an
    AssertionError at the asserting line rather than pytest's DID NOT RAISE."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - returned for the caller to assert on
        return exc
    return None


def _run(text: str, extract: Callable[[str], list[Any]], **kwargs: Any) -> Any:
    return extract_splitting_on_truncation(
        text, extract_entities=extract, split=_halve, count_tokens=_words, **kwargs
    )


def test_a_reply_that_parses_is_returned_untouched_with_no_split() -> None:
    calls: list[str] = []

    def extract(text: str) -> list[Any]:
        calls.append(text)
        return [{"name": "A"}]

    result = _run("w " * 1600, extract)
    assert result.entries == ({"name": "A"},)
    assert not result.truncated
    assert len(calls) == 1


def test_split_depth_is_bounded_then_each_leaf_is_salvaged() -> None:
    """Default depth 2 on a piece far over the floor: 1 + 2 + 4 calls, 3 splits,
    the 4 leaves salvaged — never a 15-call third level."""
    assert MAX_SPLIT_DEPTH == 2
    calls: list[str] = []
    result = _run("w " * 1600, _always_truncates(calls))
    assert len(calls) == 7
    assert result.pieces_split == 3
    assert result.pieces_salvaged == 4
    assert result.entries == ({"name": "W"},) * 4
    assert result.entries_salvaged == 4


def test_a_piece_under_the_floor_is_salvaged_without_splitting() -> None:
    calls: list[str] = []
    result = _run("w " * (MIN_SPLIT_TOKENS - 1), _always_truncates(calls))
    assert len(calls) == 1
    assert result.pieces_salvaged == 1


def test_a_piece_at_the_floor_is_split() -> None:
    calls: list[str] = []
    result = _run("w " * MIN_SPLIT_TOKENS, _always_truncates(calls), max_depth=1)
    assert len(calls) == 3
    assert result.pieces_split == 1


def test_a_piece_that_will_not_split_is_salvaged() -> None:
    calls: list[str] = []
    result = extract_splitting_on_truncation(
        "w " * 1600,
        extract_entities=_always_truncates(calls),
        split=lambda text: [text],
        count_tokens=_words,
    )
    assert len(calls) == 1
    assert result.pieces_salvaged == 1


def test_split_then_salvage_keeps_the_parsed_half_and_the_salvaged_prefix() -> None:
    head, tail = "head " * 300, "tail " * 300

    def extract(text: str) -> list[Any]:
        if text.strip() == head.strip():
            return [{"name": "A"}]
        if text.strip() == tail.strip():
            raise _truncation('{"entities": [{"name": "B"}, {"name": "C')
        raise _truncation()

    result = _run(head + tail, extract, max_depth=1)
    assert result.entries == ({"name": "A"}, {"name": "B"})
    assert (result.pieces_split, result.pieces_salvaged, result.entries_salvaged) == (1, 1, 1)


def test_a_malformed_sub_piece_is_skipped_and_counted_its_sibling_kept() -> None:
    head, tail = "head " * 300, "tail " * 300

    def extract(text: str) -> list[Any]:
        if text.strip() == head.strip():
            raise EnrichmentError("chat failed after 2 attempts: Expecting value")
        if text.strip() == tail.strip():
            return [{"name": "B"}]
        raise _truncation()

    result = _run(head + tail, extract, max_depth=1)
    assert result.entries == ({"name": "B"},)
    assert result.pieces_failed == 1


def test_ollama_unavailable_in_a_sub_piece_propagates() -> None:
    head = "head " * 300

    def extract(text: str) -> list[Any]:
        if text.strip() == head.strip():
            raise OllamaUnavailable("connection refused")
        if "head" in text:
            raise _truncation()
        return []

    raised = _raised(lambda: _run(head + "tail " * 300, extract, max_depth=1))
    assert isinstance(raised, OllamaUnavailable)


def test_the_chunks_own_malformed_reply_propagates() -> None:
    """Below the chunk a malformed piece is skipped; the chunk's OWN malformed
    reply keeps the caller's skip-the-chunk policy."""

    def extract(text: str) -> list[Any]:
        raise EnrichmentError("chat failed after 2 attempts: Expecting value")

    raised = _raised(lambda: _run("w " * 1600, extract))
    assert isinstance(raised, EnrichmentError)


def _para(word: str, n: int) -> str:
    return " ".join([word] * n)


def test_split_in_half_cuts_at_the_paragraph_boundary_nearest_the_midpoint() -> None:
    # 10 / 50 / 20 words: the paragraph cuts sit at 10 and 60 of 80; 60 is nearer
    # the midpoint (40) and leaves each side >= 25%, so it wins over a word cut.
    first, second, third = _para("a", 10), _para("b", 50), _para("c", 20)
    text = f"{first}\n\n{second}\n\n{third}"
    assert split_in_half(text, count_tokens=_words) == [f"{first}\n\n{second}\n\n", third]


def test_split_in_half_picks_the_nearer_of_the_two_neighbouring_cuts() -> None:
    # Cuts at 38 and 48 of 80: the one BELOW the midpoint is nearer.
    first, second, third = _para("a", 38), _para("b", 10), _para("c", 32)
    text = f"{first}\n\n{second}\n\n{third}"
    assert split_in_half(text, count_tokens=_words)[0] == f"{first}\n\n"


def test_split_in_half_a_lopsided_paragraph_cut_falls_through_to_a_word_cut() -> None:
    # The only paragraph cut (5 of 80) leaves one side under 25%: cut mid-words.
    text = f"{_para('a', 5)}\n\n{_para('b', 75)}"
    head, _tail = split_in_half(text, count_tokens=_words)
    assert _words(head) == 40


def test_split_in_half_falls_back_to_the_token_midpoint_without_whitespace() -> None:
    assert split_in_half("abcdefgh", count_tokens=len) == ["abcd", "efgh"]


def test_split_in_half_leaves_a_single_token_whole() -> None:
    assert split_in_half("word", count_tokens=_words) == ["word"]


def test_split_in_half_never_fans_out_past_two_and_loses_nothing() -> None:
    """The REAL tokenizer on texts whose greedy packing made 3+ pieces: never more
    than two pieces, and they concatenate back to the exact input (no loss, no
    duplication), so the recursion's 1 + 2 + 4 call bound holds."""
    from brain.enrichment import OllamaEnricher

    count = OllamaEnricher(host="http://x", model="m").count_tokens
    texts = [
        _greedy_hostile_text(),
        " ".join(f"Sentence {i} closes here." for i in range(300)),
        "x" * 4000,
    ]
    for text in texts:
        pieces = split_in_half(text, count_tokens=count)
        assert len(pieces) <= 2
        assert "".join(pieces) == text


def _greedy_hostile_text() -> str:
    """Uneven paragraphs (synthetic) that a greedy half-target packer splits 3+ ways."""
    sizes = [300, 40, 300, 40, 300, 40, 300]
    return "\n\n".join(" ".join(["routine"] * n) + "." for n in sizes)
