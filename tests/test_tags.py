"""Unit tests for brain.tags — pure-Python normalization rules.

No DB required; these are tight contract tests for the canonical
normalization rule used at every tag-write boundary in the codebase.
"""
from __future__ import annotations

import pytest

from brain.tags import normalize_tag, normalize_tags

# ---------------------------------------------------------------------------
# normalize_tag — single-string canonicalization
# ---------------------------------------------------------------------------


def test_normalize_tag_lowercases_brand_casing() -> None:
    # (a) Brand/proper-noun casing is collapsed to lowercase storage form.
    assert normalize_tag("COMPANY_REDACTED") == "company-ko"


def test_normalize_tag_replaces_spaces_with_hyphen() -> None:
    # (b) Internal whitespace becomes a single hyphen.
    assert normalize_tag("Interview Prep") == "interview-prep"


def test_normalize_tag_replaces_underscores_with_hyphen() -> None:
    # (c) Underscore separator collapses to hyphen — same canonical bucket
    # as the space-separated form above.
    assert normalize_tag("interview_prep") == "interview-prep"


def test_normalize_tag_strips_outer_whitespace_and_collapses_hyphens() -> None:
    # (d) Leading / trailing whitespace and runs of hyphens both collapse.
    assert normalize_tag("  --foo--  ") == "foo"


def test_normalize_tag_handles_unicode_casefold() -> None:
    # (e) ``str.casefold`` (vs ``str.lower``) is what makes the German
    # eszett collapse to ``ss`` — the Unicode-correct lowercase form.
    assert normalize_tag("ß") == "ss"


def test_normalize_tag_returns_empty_for_empty_input() -> None:
    # (f) Empty input → empty string (gets dropped by ``normalize_tags``).
    assert normalize_tag("") == ""


def test_normalize_tag_returns_empty_for_whitespace_input() -> None:
    # (f, cont.) All-whitespace input → empty string.
    assert normalize_tag("   ") == ""


def test_normalize_tag_collapses_mixed_separators() -> None:
    # Defensive: a mix of underscores + spaces + hyphens collapses to one.
    assert normalize_tag("a _ b - c") == "a-b-c"


def test_normalize_tag_preserves_internal_unicode() -> None:
    # Unicode characters that already lowercase cleanly are passed through.
    assert normalize_tag("Réview") == "réview"


# ---------------------------------------------------------------------------
# normalize_tags — list-level canonicalization
# ---------------------------------------------------------------------------


def test_normalize_tags_dedupes_case_variants() -> None:
    # (g) Mixed-case duplicates collapse to a single canonical entry.
    assert normalize_tags(["A", "a", "A"]) == ["a"]


def test_normalize_tags_drops_empties() -> None:
    # (h) Empty strings and whitespace-only entries vanish.
    assert normalize_tags(["foo", "", "  ", "bar"]) == ["foo", "bar"]


def test_normalize_tags_preserves_first_seen_order() -> None:
    # (i) ``dict.fromkeys`` keeps the first occurrence; later duplicates
    # don't reorder the list.
    assert normalize_tags(["b", "a", "b"]) == ["b", "a"]


def test_normalize_tags_dedupes_separator_variants() -> None:
    # ``Interview Prep`` / ``interview_prep`` / ``interview-prep`` all
    # canonicalize to the same bucket.
    result = normalize_tags(["Interview Prep", "interview_prep", "interview-prep"])
    assert result == ["interview-prep"]


def test_normalize_tags_drops_entries_that_canonicalize_to_empty() -> None:
    # ``"---"`` becomes "" after the strip("-") step — must not survive.
    assert normalize_tags(["foo", "---", "bar"]) == ["foo", "bar"]


@pytest.mark.parametrize(
    "tags",
    [
        ["a"],
        ["COMPANY_REDACTED", "company-ko"],
        ["foo", "bar", "BAZ"],
        ["Interview Prep", "interview_prep", "AI"],
        ["", "   ", "---"],
        [],
        ["a-b-c", "A B C", "a_b_c"],
    ],
)
def test_normalize_tags_is_idempotent(tags: list[str]) -> None:
    # (j) ``normalize_tags(normalize_tags(x)) == normalize_tags(x)`` — the
    # canonical form is a fixed point of the function.
    once = normalize_tags(tags)
    twice = normalize_tags(once)
    assert once == twice


def test_normalize_tags_returns_fresh_list() -> None:
    # Callers can mutate the return value without affecting future calls.
    a = normalize_tags(["foo"])
    b = normalize_tags(["foo"])
    assert a == b
    a.append("bar")
    assert b == ["foo"]


def test_normalize_tags_accepts_arbitrary_iterable() -> None:
    # The signature uses ``Iterable[str]`` — generators must work too.
    def _gen() -> object:
        yield "Foo"
        yield "FOO"
        yield "foo"

    assert normalize_tags(_gen()) == ["foo"]
