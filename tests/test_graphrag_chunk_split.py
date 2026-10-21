"""Tests for :mod:`brain.graph_rag.chunk_split` — re-splitting a concept-extraction
chunk whose model reply was cut off at ``num_predict``.

Pure DI: the model call, the splitter and the token counter are plain fakes
(``count_tokens`` = word count), so the recursion bounds are exact. All values
are synthetic (no PII).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

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
    assert result.pieces_split == 0
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

    with pytest.raises(OllamaUnavailable):
        _run(head + "tail " * 300, extract, max_depth=1)


def test_the_chunks_own_malformed_reply_propagates() -> None:
    """Below the chunk a malformed piece is skipped; the chunk's OWN malformed
    reply keeps the caller's skip-the-chunk policy."""

    def extract(text: str) -> list[Any]:
        raise EnrichmentError("chat failed after 2 attempts: Expecting value")

    with pytest.raises(EnrichmentError):
        _run("w " * 1600, extract)


def test_split_in_half_partitions_the_text_at_paragraph_boundaries() -> None:
    first = " ".join(["alpha"] * 50)
    second = " ".join(["beta"] * 50)
    pieces = split_in_half(f"{first}\n\n{second}", count_tokens=_words)
    assert pieces == [first, second]
