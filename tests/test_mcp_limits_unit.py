"""Pure-logic tests for :mod:`brain.mcp_limits` — no DB, no MCP stack.

The module is deliberately framework-free so the *policy* (what is capped, and
whether the cut is visible) can be tested without a server. The theme running
through every test below is the module's own rule: **a ceiling that silently
truncates is worse than no ceiling.** Each function either raises with the
ceiling named or returns something the caller pairs with an explicit marker.

All fixture data is synthetic.
"""
from __future__ import annotations

import pytest

from brain.mcp_limits import (
    CONTENT_MARKERS,
    PayloadCeilingExceeded,
    apply_content_ceiling,
    cap_rows,
    check_ceiling,
    resolve_content_ceiling,
    saturation_notice,
    strip_content_markers,
    truncate_content,
)


def _cost(text: str) -> int:
    """Deterministic stand-in for ``Embedder.count_tokens`` (1 token / 4 chars)."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# check_ceiling
# ---------------------------------------------------------------------------


def test_check_ceiling_disabled_when_ceiling_is_zero() -> None:
    """``ceiling <= 0`` is the documented operator opt-out — the ONLY one."""
    assert check_ceiling(10**9, ceiling=0, param="limit") == 10**9
    assert check_ceiling(10**9, ceiling=-1, param="limit") == 10**9


def test_check_ceiling_accepts_the_exact_ceiling() -> None:
    """The boundary is inclusive: ``value == ceiling`` is a legal request."""
    assert check_ceiling(50, ceiling=50, param="limit") == 50


def test_check_ceiling_raises_naming_the_param_and_the_ceiling() -> None:
    """The error must be actionable — the agent has to know what to re-ask.

    An error that says only "too large" forces a guessing loop; naming both
    numbers makes the retry deterministic.
    """
    with pytest.raises(PayloadCeilingExceeded) as excinfo:
        check_ceiling(51, ceiling=50, param="limit")
    message = str(excinfo.value)
    assert "limit" in message
    assert "51" in message
    assert "50" in message


# ---------------------------------------------------------------------------
# cap_rows
# ---------------------------------------------------------------------------


def test_cap_rows_saturation_is_greater_than_not_equal() -> None:
    """Pins the ambiguous-at-exact-count decision.

    ``len(result) == limit`` cannot distinguish "there were exactly 3" from
    "there were 40 and we cut". Only ``len(rows) > limit`` can, which is why
    callers over-fetch one row.
    """
    exactly_full, saturated = cap_rows([1, 2, 3], limit=3)
    assert exactly_full == [1, 2, 3]
    assert saturated is False, "a full-but-complete page must NOT claim more"

    cut, saturated = cap_rows([1, 2, 3, 4], limit=3)
    assert cut == [1, 2, 3]
    assert saturated is True


@pytest.mark.parametrize("limit", [0, -1])
def test_cap_rows_rejects_a_non_positive_limit(limit: int) -> None:
    """A row cap of ``0`` is not an opt-out — it is a lie waiting to be told.

    Both graph tools fetch ``LIMIT effective_limit + 1``. Under a ``0`` ceiling
    that is ``LIMIT 1``, and the old ``limit <= 0`` branch reported that single
    row with ``saturated=False`` — a one-row answer presented as complete, the
    exact failure :mod:`brain.mcp_limits` forbids. No env var can produce it
    (all five row knobs are ``_parse_positive_int_env``), so failing loudly
    costs nothing reachable and removes a guard that lied.
    """
    with pytest.raises(ValueError) as excinfo:
        cap_rows([1, 2, 3], limit=limit)
    assert "limit >= 1" in str(excinfo.value)


# ---------------------------------------------------------------------------
# saturation_notice
# ---------------------------------------------------------------------------


def test_saturation_notice_marks_only_the_last_element() -> None:
    rows = [{"a": 1}, {"a": 2}, {"a": 3}]
    marked = saturation_notice(rows, saturated=True)
    assert marked[-1] == {"a": 3, "more_available": True}
    assert marked[:-1] == [{"a": 1}, {"a": 2}]


def test_saturation_notice_does_not_mutate_its_input() -> None:
    """Immutability rule: the caller's rows must survive untouched."""
    rows = [{"a": 1}, {"a": 2}]
    saturation_notice(rows, saturated=True)
    assert rows == [{"a": 1}, {"a": 2}]


def test_saturation_notice_adds_nothing_when_not_saturated() -> None:
    """A complete list must never carry a flag implying it was cut."""
    rows = [{"a": 1}]
    assert saturation_notice(rows, saturated=False) == [{"a": 1}]
    assert saturation_notice([], saturated=True) == []


# ---------------------------------------------------------------------------
# truncate_content
# ---------------------------------------------------------------------------


def test_truncate_content_returns_token_count() -> None:
    """``tokens`` describes the string RETURNED, not the string handed in."""
    text = "x" * 4000  # 1000 tokens under ``_cost``
    kept, was_truncated, tokens = truncate_content(text, max_tokens=100, cost=_cost)
    assert was_truncated is True
    assert tokens <= 100
    assert tokens == _cost(kept)
    assert len(kept) < len(text)


def test_truncate_content_leaves_a_short_string_untouched() -> None:
    kept, was_truncated, tokens = truncate_content(
        "short", max_tokens=1000, cost=_cost
    )
    assert (kept, was_truncated) == ("short", False)
    assert tokens == _cost("short")


def test_truncate_content_disabled_when_max_tokens_is_zero() -> None:
    text = "y" * 4000
    kept, was_truncated, _ = truncate_content(text, max_tokens=0, cost=_cost)
    assert kept == text
    assert was_truncated is False


# ---------------------------------------------------------------------------
# resolve_content_ceiling
# ---------------------------------------------------------------------------


def test_resolve_content_ceiling_defaults_to_the_configured_value() -> None:
    assert resolve_content_ceiling(None, configured=25000) == 25000


def test_resolve_content_ceiling_allows_lowering_but_not_raising() -> None:
    assert resolve_content_ceiling(500, configured=25000) == 500
    with pytest.raises(PayloadCeilingExceeded):
        resolve_content_ceiling(25001, configured=25000)


def test_resolve_content_ceiling_rejects_zero_as_a_caller_opt_out() -> None:
    """``0`` must NOT let a caller disable the ceiling it is subject to.

    ``check_ceiling`` treats ``ceiling <= 0`` as unbounded, so accepting a
    caller-supplied ``0`` here would hand every agent an escape hatch that is
    meant to belong to the operator's env var alone.
    """
    with pytest.raises(PayloadCeilingExceeded):
        resolve_content_ceiling(0, configured=25000)


# ---------------------------------------------------------------------------
# apply_content_ceiling / strip_content_markers
# ---------------------------------------------------------------------------


def test_apply_content_ceiling_leaves_a_small_payload_byte_identical() -> None:
    """The backward-compat guarantee, at the policy layer."""
    payload = {"id": "abc", "content": "a short body", "title": "t"}
    out = apply_content_ceiling(
        payload, summary_only=False, has_summary=True, max_tokens=1000, cost=_cost
    )
    assert out == payload
    assert not (set(out) & set(CONTENT_MARKERS))


def test_apply_content_ceiling_summary_only_drops_the_body_with_a_marker() -> None:
    payload = {"content": "a body", "summary": "s"}
    out = apply_content_ceiling(
        payload, summary_only=True, has_summary=True, max_tokens=1000, cost=_cost
    )
    assert out["content"] is None
    assert "summary_only=false" in out["content_omitted"]


def test_apply_content_ceiling_falls_back_when_there_is_no_summary() -> None:
    """The NULL-summary tail: an empty answer is the worse failure."""
    payload = {"content": "a body"}
    out = apply_content_ceiling(
        payload, summary_only=True, has_summary=False, max_tokens=1000, cost=_cost
    )
    assert out["content"] == "a body"
    assert out["summary_unavailable"] is True
    assert "content_omitted" not in out


def test_apply_content_ceiling_does_not_mutate_its_input() -> None:
    payload = {"content": "z" * 4000}
    apply_content_ceiling(
        payload, summary_only=False, has_summary=False, max_tokens=10, cost=_cost
    )
    assert payload == {"content": "z" * 4000}


def test_every_key_apply_content_ceiling_adds_is_declared_in_content_markers() -> None:
    """THE REVERSE GATE: a new marker key cannot escape ``CONTENT_MARKERS``.

    The existing coverage runs one way — deleting an entry from the tuple goes
    red. Nothing forced the other direction, so a NEW key added to
    :func:`apply_content_ceiling` and forgotten here would survive
    :func:`strip_content_markers` and ride out on a *confidential* ``brain_show``
    payload, implying a body was produced, with the whole suite green.

    Exhaustive over the branch space rather than over hand-picked cases: all
    four ``(summary_only, has_summary)`` combinations crossed with a body over
    and under the cap — eight calls, which is every path through the function.
    ``==`` (not ``<=``) makes this bidirectional, so it also fails a marker that
    is declared but no branch can ever emit.
    """
    base = {"id": "x", "content": "z" * 4000, "summary": "s" * 4000}
    small = {**base, "content": "z" * 8, "summary": "s" * 8}

    added: set[str] = set()
    truncating_arms = 0
    summary_truncating_arms = 0
    for summary_only in (False, True):
        for has_summary in (False, True):
            for payload, max_tokens in ((base, 10), (small, 10_000)):
                out = apply_content_ceiling(
                    payload,
                    summary_only=summary_only,
                    has_summary=has_summary,
                    max_tokens=max_tokens,
                    cost=_cost,
                )
                new_keys = set(out) - set(payload)
                added |= new_keys
                truncating_arms += "content_truncated" in new_keys
                summary_truncating_arms += "summary_truncated" in new_keys

    # Guard the guard, three ways. Without these, a function that returned its
    # input unchanged — or one whose truncation branch had become unreachable —
    # would satisfy the assertion below by emitting nothing at all. The summary
    # arm is counted separately because a long ``summary`` is what makes the
    # summary-ceiling markers reachable at all; if that fixture were ever
    # shortened back to ``"s"``, the ``==`` below would start failing for the
    # confusing reason ("declared but never emitted") rather than this clear one.
    assert added, "no branch added any key; the arms below assert nothing"
    assert truncating_arms, "no arm truncated; the truncation markers are untested"
    assert summary_truncating_arms, (
        "no arm truncated a summary; the summary-ceiling markers are untested"
    )

    assert added == set(CONTENT_MARKERS), (
        "apply_content_ceiling emits keys CONTENT_MARKERS does not declare "
        f"(undeclared: {sorted(added - set(CONTENT_MARKERS))}); a confidential "
        "brain_show would leak them past strip_content_markers. Declared but "
        f"never emitted: {sorted(set(CONTENT_MARKERS) - added)}"
    )


def test_apply_content_ceiling_bounds_the_summary_under_summary_only() -> None:
    """N7: the escape hatch FROM the body ceiling must have a ceiling itself.

    ``summary_only=true`` is the cheap mode. Before this, it returned
    ``documents.summary`` verbatim — a ``TEXT`` column with no length
    constraint in migration 011 — so the cheap mode was the one with the
    unbounded payload.
    """
    payload = {"content": "b" * 4000, "summary": "s" * 4000}

    out = apply_content_ceiling(
        payload, summary_only=True, has_summary=True, max_tokens=10, cost=_cost
    )

    assert out["content"] is None
    assert out["summary_truncated"] is True
    assert len(out["summary"]) < len(payload["summary"])
    # The count describes the string RETURNED, matching ``truncate_content``'s
    # contract — a caller reporting it is reporting what it actually got.
    assert out["summary_tokens"] == _cost(out["summary"])
    assert "brain show" in out["summary_truncated_recovery"]


def test_apply_content_ceiling_bounds_the_summary_on_the_default_path_too() -> None:
    """Not only under ``summary_only``: ``brain_show`` returns both fields.

    Capping the summary only under the escape hatch would leave the identical
    hole on the path every ordinary open takes.
    """
    payload = {"content": "b" * 8, "summary": "s" * 4000}

    out = apply_content_ceiling(
        payload, summary_only=False, has_summary=True, max_tokens=10, cost=_cost
    )

    assert out["content"] == "b" * 8, "the body was under the ceiling; leave it alone"
    assert out["summary_truncated"] is True
    assert out["summary_tokens"] == _cost(out["summary"])


def test_a_short_summary_is_left_byte_identical() -> None:
    """The additive guarantee: no marker rides out when nothing was cut."""
    payload = {"id": "x", "content": "short", "summary": "also short"}

    out = apply_content_ceiling(
        payload, summary_only=False, has_summary=True, max_tokens=10_000, cost=_cost
    )

    assert out == payload
    assert not (set(out) & set(CONTENT_MARKERS))


def test_strip_content_markers_removes_every_marker_and_nothing_else() -> None:
    payload = {"id": "x", "content": None, **dict.fromkeys(CONTENT_MARKERS, True)}
    assert strip_content_markers(payload) == {"id": "x", "content": None}
