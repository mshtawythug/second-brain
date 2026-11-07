"""The ``[N]`` citation helpers shared by ``brain ask`` and ``brain tags summarize`` (RM-55)."""
from __future__ import annotations

import pytest

from brain.citations import cited_refs, strip_dangling_citations

pytestmark = pytest.mark.nodb


def test_strip_keeps_in_range_markers_verbatim() -> None:
    assert strip_dangling_citations("one [1] and two [2].", 2) == "one [1] and two [2]."


def test_strip_removes_out_of_range_and_zero_with_lead_space() -> None:
    assert strip_dangling_citations("a [0] b [3] c [2]", 2) == "a b c [2]"


def test_strip_with_no_sources_removes_every_marker() -> None:
    assert strip_dangling_citations("nothing [1] here", 0) == "nothing here"


def test_strip_leaves_bracketed_words_alone() -> None:
    assert strip_dangling_citations("see [note] and [1]", 1) == "see [note] and [1]"


def test_cited_refs_dedupes_in_first_appearance_order() -> None:
    assert cited_refs("[2] x [1] [2] [9]", 2) == (2, 1)


def test_cited_refs_ignores_zero_and_out_of_range() -> None:
    assert cited_refs("[0] [3] [1]", 2) == (1,)


def test_cited_refs_of_text_without_markers_is_empty() -> None:
    assert cited_refs("no markers at all", 5) == ()


def test_a_seven_digit_marker_is_stripped_and_not_cited() -> None:
    assert strip_dangling_citations("a [1234567] b [1]", 2) == "a b [1]"
    assert cited_refs("a [1234567] b", 2) == ()


def test_a_5000_digit_marker_does_not_crash_and_is_stripped() -> None:
    text = f"claim [{'9' * 5000}] and [2]"
    assert strip_dangling_citations(text, 2) == "claim and [2]"
    assert cited_refs(text, 2) == (2,)


def test_a_six_digit_marker_is_still_read_as_a_number() -> None:
    assert strip_dangling_citations("x [000002] y", 2) == "x [000002] y"
    assert cited_refs("x [000002] y", 2) == (2,)


def test_a_marker_in_non_ascii_digits_is_stripped_and_not_cited() -> None:
    arabic_indic_two = "٢"
    text = f"x [{arabic_indic_two}] y [1]"
    assert strip_dangling_citations(text, 2) == "x y [1]"
    assert cited_refs(text, 2) == (1,)
