"""Unit tests for the helpers in ``brain.vault.rename``.

These exercise pure transforms (no DB) so we cover defensive branches that
the higher-level integration tests don't naturally hit.
"""
from __future__ import annotations

import pytest

from brain.vault.rename import (
    RenameError,
    _rewrite_link_text,
    _rewrite_source_frontmatter,
    apply_matches_to_text,
)


def test_apply_matches_empty_returns_text() -> None:
    """No matches → text returned unchanged."""
    text = "no links here"
    assert apply_matches_to_text(text, []) == text


def test_rewrite_link_text_simple() -> None:
    assert _rewrite_link_text("[[Old]]", new_title="New", embed=False) == "[[New]]"


def test_rewrite_link_text_with_alias() -> None:
    assert (
        _rewrite_link_text("[[Old|alias]]", new_title="New", embed=False)
        == "[[New|alias]]"
    )


def test_rewrite_link_text_with_heading() -> None:
    assert (
        _rewrite_link_text("[[Old#section]]", new_title="New", embed=False)
        == "[[New#section]]"
    )


def test_rewrite_link_text_embed() -> None:
    assert _rewrite_link_text("![[Old]]", new_title="New", embed=True) == "![[New]]"


def test_rewrite_link_text_combo_heading_and_alias() -> None:
    """Heading goes on the new title; alias preserved."""
    assert (
        _rewrite_link_text("[[Old#h|disp]]", new_title="New", embed=False)
        == "[[New#h|disp]]"
    )


def test_rewrite_source_frontmatter_malformed_raises() -> None:
    """If somehow the source's frontmatter is malformed, raise RenameError."""
    bad_text = "---\nfoo: [unclosed\n---\nbody\n"
    with pytest.raises(RenameError, match="malformed"):
        _rewrite_source_frontmatter(bad_text, new_title="X")


def test_rewrite_source_frontmatter_no_frontmatter_creates_one() -> None:
    """A file without frontmatter at all gets a fresh header (defensive)."""
    text = "just a body, no frontmatter"
    out = _rewrite_source_frontmatter(text, new_title="X")
    assert out.startswith("---")
    assert "title: X" in out
    assert "updated:" in out
    assert text in out
