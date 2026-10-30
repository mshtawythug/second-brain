"""``brain.ingest.text_safety.one_line``: titles printed to a terminal (RM-31, moved for RM-36)."""
from __future__ import annotations

from brain.ingest import github_docs, text_safety


def test_one_line_collapses_whitespace_and_drops_control_and_format_characters() -> None:
    assert text_safety.one_line(" a\n\tb \x1b[2J​‮ c\x07 ") == "a b [2J c"


def test_github_docs_re_exports_the_same_function() -> None:
    assert github_docs.one_line is text_safety.one_line
