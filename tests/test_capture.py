"""Unit tests for the pure quick-capture helpers in ``brain.capture``.

All fixtures are synthetic — no production data, no DB, no wall-clock dates
(``make_capture_title`` takes ``today`` as a frozen parameter).
"""
from datetime import date

import pytest

from brain.capture import make_capture_title, slugify_title

# ---------------------------------------------------------------------------
# slugify_title
# ---------------------------------------------------------------------------


def test_slugify_title_canonical_example() -> None:
    """The plan's canonical example slugifies hyphenated words intact."""
    out = slugify_title("follow up with person-a re project-ko", max_words=6)
    assert out == "follow-up-with-person-a-re-project-ko"


def test_slugify_title_strips_punctuation() -> None:
    """Trailing/embedded punctuation collapses to hyphens and trims."""
    assert slugify_title("Hello, World!!! Foo.", max_words=6) == "hello-world-foo"


def test_slugify_title_preserves_unicode_word_chars() -> None:
    """Accented letters survive (no ASCII-only stripping) and are casefolded."""
    assert slugify_title("Café Münchën", max_words=6) == "café-münchën"


def test_slugify_title_collapses_whitespace_and_trims() -> None:
    """Leading/trailing and repeated interior whitespace produce no empties."""
    assert slugify_title("  leading   and  trailing  ", max_words=6) == (
        "leading-and-trailing"
    )


def test_slugify_title_honors_max_words() -> None:
    """Only the first ``max_words`` whitespace words are kept."""
    assert slugify_title("one two three four five", max_words=3) == "one-two-three"


def test_slugify_title_empty_text_returns_empty() -> None:
    """Whitespace-only input yields an empty slug (not an error)."""
    assert slugify_title("   ", max_words=6) == ""


def test_slugify_title_rejects_non_positive_max_words() -> None:
    """``max_words`` below 1 is a programming error, surfaced as ValueError."""
    with pytest.raises(ValueError, match="max_words"):
        slugify_title("anything", max_words=0)


# ---------------------------------------------------------------------------
# make_capture_title
# ---------------------------------------------------------------------------


def test_make_capture_title_format_with_frozen_date() -> None:
    """Auto-title is ``<ISO date>-capture-<slug>`` using the injected date."""
    out = make_capture_title(
        "follow up with person-a re project-ko",
        today=date(2026, 6, 4),
        max_words=6,
    )
    assert out == "2026-06-04-capture-follow-up-with-person-a-re-project-ko"
