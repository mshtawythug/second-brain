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
