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


def test_strip_content_markers_removes_every_marker_and_nothing_else() -> None:
    payload = {"id": "x", "content": None, **dict.fromkeys(CONTENT_MARKERS, True)}
    assert strip_content_markers(payload) == {"id": "x", "content": None}
