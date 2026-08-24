"""``brain.token_report`` — the one definition of "payload tokens".

Pure logic, no DB and no embedder object: every test injects a ``cost``
callable. What is pinned here is that the count is taken over the SERIALIZED
payload, because the failure mode this module exists to prevent is two
surfaces each counting something slightly different and then being compared.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
from typing import Any

from brain.token_report import count_payload_tokens, count_results_tokens

#: A one-token-per-character cost, so every assertion below is an exact
#: character count rather than a tokenizer approximation.
_CHARS: Any = len


def _result(snippet: str) -> dict[str, Any]:
    """One entry in the frozen seven-key projection shape."""
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "title": "Quarterly note",
        "source_kind": "manual",
        "snippet": snippet,
        "score": 0.5,
        "content_type": "note",
        "tags": ["planning"],
    }


def test_counts_serialized_json_not_the_sum_of_snippets() -> None:
    """Structural overhead — keys, quotes, braces, commas — is real cost.

    Summing the snippet strings would under-report by everything the caller
    also has to read. Asserted as a strict inequality against that wrong
    answer, not just as "some number", so an implementation that quietly
    switched to summing values would fail here.
    """
    results = [_result("alpha snippet"), _result("beta snippet")]

    counted = count_results_tokens(results, cost=_CHARS)

    snippets_only = sum(len(r["snippet"]) for r in results)
    assert counted > snippets_only
    assert counted == len(json.dumps(results, ensure_ascii=False))


def test_empty_results_costs_the_empty_list() -> None:
    """An empty payload still costs its two brackets, and never crashes."""
    assert count_results_tokens([], cost=_CHARS) == len("[]")


def test_non_ascii_is_not_escaped() -> None:
    """``ensure_ascii=False`` is load-bearing, not a style choice.

    The Wave-0 harness (``scripts/token_payload_report.py``) serializes with
    ``ensure_ascii=False``; the default ``True`` would expand every non-ASCII
    character to a ``\\uXXXX`` escape and inflate the count against a payload
    the caller never receives. The two measurements are cross-checked, so a
    divergence here would silently corrupt the comparison.
    """
    results = [_result("café — naïve")]

    counted = count_results_tokens(results, cost=_CHARS)

    assert counted == len(json.dumps(results, ensure_ascii=False))
    assert counted < len(json.dumps(results, ensure_ascii=True))


def test_count_payload_tokens_handles_a_dict_payload() -> None:
    """``brain recall``'s shape: a dict, not a list of results."""
    payload = {"query": "quarterly", "passages": [], "used_tokens": 12}

    counted = count_payload_tokens(payload, cost=_CHARS)

    assert counted == len(json.dumps(payload, ensure_ascii=False))


def test_the_cost_callable_is_actually_used() -> None:
    """Non-vacuous: the injected counter, not a hardcoded len, decides.

    A ``cost`` that doubles must double the answer. Without this a version
    that ignored ``cost`` and called ``len`` directly would pass every other
    test in this file.
    """
    results = [_result("alpha")]

    plain = count_results_tokens(results, cost=_CHARS)
    doubled = count_results_tokens(results, cost=lambda s: len(s) * 2)

    assert doubled == plain * 2
