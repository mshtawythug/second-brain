"""Tests for brain.vault.slug — deterministic ASCII slug generation."""
from brain.vault.slug import slugify


def test_lowercases_basic_ascii() -> None:
    assert slugify("Hello World") == "hello-world"


def test_collapses_runs_of_non_alnum() -> None:
    assert slugify("foo!!  --  bar??") == "foo-bar"


def test_strips_leading_and_trailing_dashes() -> None:
    assert slugify("---foo bar---") == "foo-bar"
    assert slugify("!!!hello") == "hello"


def test_unicode_transliterated_via_nfkd() -> None:
    # NFKD-normalize: "é" → "e", "ñ" → "n"; bytes that don't decompose to
    # ASCII (like "中") are dropped entirely.
    assert slugify("person-x Q1 réview") == "person-a-q1-review"
    assert slugify("Señor año") == "senor-ano"


def test_non_ascii_only_falls_back_to_untitled() -> None:
    # No ASCII bytes survive NFKD encode → fallback.
    assert slugify("中文标题") == "untitled"
    assert slugify("🚀🚀🚀") == "untitled"


def test_empty_string_falls_back_to_untitled() -> None:
    assert slugify("") == "untitled"


def test_whitespace_only_falls_back_to_untitled() -> None:
    assert slugify("   \t\n  ") == "untitled"


def test_truncated_to_64_chars() -> None:
    # 80 a's; expect 64.
    assert slugify("a" * 80) == "a" * 64


def test_truncation_strips_trailing_dash() -> None:
    # 64 a's + 16 dashes — after truncation to 64 we still strip a dangling dash.
    long = "a" * 60 + " " * 30
    result = slugify(long)
    # First 60 chars are "a", the rest collapses to "-" then gets stripped.
    # Result: 60 a's, no trailing dash.
    assert result == "a" * 60
    assert not result.endswith("-")


def test_deterministic_on_repeat_calls() -> None:
    inputs = ["Hello World", "person-x Q1", "señor año", "" , "🚀"]
    first = [slugify(s) for s in inputs]
    second = [slugify(s) for s in inputs]
    assert first == second


def test_numbers_preserved() -> None:
    assert slugify("Q1 2026 review") == "q1-2026-review"


def test_underscores_treated_as_separators() -> None:
    # `_` is non-alnum per our regex (a-z0-9 only).
    assert slugify("hello_world") == "hello-world"
