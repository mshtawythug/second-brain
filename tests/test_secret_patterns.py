"""Tests for the shared secret-pattern registry (:mod:`brain.secret_patterns`)."""
from __future__ import annotations

import re

import pytest

from brain.secret_patterns import (
    SECRET_PATTERNS,
    SecretPattern,
    compiled_patterns,
    egrep_alternation,
)
from tests.secret_fixtures import SYNTHETIC_NEGATIVES, SYNTHETIC_POSITIVES

# Characters that end a run of literal, value-fixed regex text.
_METACHARS = "(){}*+?|.\\^$"

# A bracket class this small is an enumeration (e.g. the token-TYPE letter in
# ``xox[baprs]-``), not an entropy source. Anything larger is treated as secret
# material for the purposes of the preview-safety check below.
_MAX_ENUMERATION_MEMBERS = 8


def _low_entropy_prefix_len(regex: str) -> int:
    """Leading characters of ``regex`` whose value is fixed or drawn from a tiny set.

    This is the upper bound on a safe ``preview_head``. A literal character
    contributes 1. An unquantified bracket class of at most
    :data:`_MAX_ENUMERATION_MEMBERS` enumerated members (no ``-`` ranges) also
    contributes 1, because its value identifies a token *variant* rather than
    carrying secret bytes. Anything else — a range class, a quantifier, a
    group — stops the scan.
    """
    index = 0
    length = 0
    while index < len(regex):
        char = regex[index]
        if char == "[":
            close = regex.index("]", index)
            members = regex[index + 1 : close]
            is_range = "-" in members[1:-1]
            quantified = close + 1 < len(regex) and regex[close + 1] in "*+?{"
            if is_range or quantified or len(members) > _MAX_ENUMERATION_MEMBERS:
                break
            length += 1
            index = close + 1
            continue
        if char in _METACHARS:
            break
        length += 1
        index += 1
    return length


def test_registry_and_fixtures_cover_the_same_kinds() -> None:
    """A pattern with no fixture is an untested pattern — fail loudly."""
    kinds = {p.kind for p in SECRET_PATTERNS}
    assert kinds == set(SYNTHETIC_POSITIVES)
    assert kinds == set(SYNTHETIC_NEGATIVES)


def test_every_kind_is_unique() -> None:
    """``kind`` is the redaction marker and a JSON key — duplicates would alias."""
    kinds = [p.kind for p in SECRET_PATTERNS]
    assert len(kinds) == len(set(kinds))


def test_every_pattern_compiles() -> None:
    compiled = compiled_patterns()
    assert len(compiled) == len(SECRET_PATTERNS)
    for pattern, regex in compiled:
        assert isinstance(pattern, SecretPattern)
        assert isinstance(regex, re.Pattern)


def test_compiled_patterns_is_cached_not_rebuilt() -> None:
    """Recompiling per call would dominate a full-corpus scan."""
    assert compiled_patterns() is compiled_patterns()


@pytest.mark.parametrize("pattern", SECRET_PATTERNS, ids=lambda p: p.kind)
def test_pattern_matches_its_canonical_positive(pattern: SecretPattern) -> None:
    # --- setup
    positive = SYNTHETIC_POSITIVES[pattern.kind]

    # --- exercise / verify
    assert re.search(pattern.regex, positive), (
        f"{pattern.kind} failed to match its own synthetic fixture"
    )


@pytest.mark.parametrize("pattern", SECRET_PATTERNS, ids=lambda p: p.kind)
def test_pattern_rejects_its_near_miss(pattern: SecretPattern) -> None:
    # --- setup
    negative = SYNTHETIC_NEGATIVES[pattern.kind]

    # --- exercise / verify
    assert not re.search(pattern.regex, negative)


def test_no_negative_matches_any_pattern() -> None:
    """A negative that trips a SIBLING regex would silently weaken the suite."""
    for kind, negative in SYNTHETIC_NEGATIVES.items():
        for pattern, regex in compiled_patterns():
            assert not regex.search(negative), (
                f"negative fixture for {kind} unexpectedly matched {pattern.kind}"
            )


@pytest.mark.parametrize("pattern", SECRET_PATTERNS, ids=lambda p: p.kind)
def test_preview_head_stays_inside_the_low_entropy_prefix(
    pattern: SecretPattern,
) -> None:
    """``preview_head`` must never reach a character of the credential itself.

    This is the structural half of the preview-safety contract (the behavioural
    half lives in ``test_ingest_guard.py``). It is what stops someone raising
    ``openai_key``'s head from 3 to 4 for consistency with its neighbours —
    ``sk-`` is only three characters, so a fourth would echo a secret byte.
    """
    assert pattern.preview_head <= _low_entropy_prefix_len(pattern.regex), (
        f"{pattern.kind}: preview_head={pattern.preview_head} reaches past the "
        f"pattern's fixed prefix and would leak credential bytes"
    )


def test_egrep_alternation_is_deterministic() -> None:
    assert egrep_alternation() == egrep_alternation()


def test_egrep_alternation_contains_each_regex_exactly_once() -> None:
    # --- exercise
    alternation = egrep_alternation()

    # --- verify
    for pattern in SECRET_PATTERNS:
        assert alternation.count(pattern.regex) == 1, (
            f"{pattern.kind} appears {alternation.count(pattern.regex)} times"
        )


def test_egrep_alternation_is_a_single_parenthesized_group() -> None:
    # --- exercise
    alternation = egrep_alternation()

    # --- verify
    assert alternation.startswith("(")
    assert alternation.endswith(")")
    # One pipe fewer than the number of alternatives. Any pattern that itself
    # contained a top-level "|" would break the hook's grouping, so this doubles
    # as a guard against adding one.
    assert alternation.count("|") == len(SECRET_PATTERNS) - 1


def test_egrep_alternation_is_valid_python_regex() -> None:
    """POSIX-ERE / Python-``re`` compatibility is the premise of sharing one string."""
    assert re.compile(egrep_alternation()) is not None
