"""Payload ceilings for the MCP tool surface — one place, not eight.

Every ceiling here answers the same question: an agent asked for something
whose size it cannot see in advance, and a single MCP response must never be
able to consume a large fraction of its context window. The bounds are
*policy*, so they live together and are configured together; the tools stay
thin.

A ceiling that silently truncates is worse than no ceiling, because the caller
reads a partial answer as a complete one. So every function here either (a)
raises :class:`PayloadCeilingExceeded` with the ceiling named, so the agent can
re-ask smaller, or (b) returns a value the caller pairs with an explicit
truncation marker. Never a bare silent cut.

That rule binds this module's own configuration too, which is why (c) exists:
a ceiling that could not do its job — a row cap of ``0``, which would trim to
nothing while reporting completeness — raises ``ValueError`` rather than
degrading. See :func:`cap_rows`. ``ValueError`` and not
:class:`PayloadCeilingExceeded` deliberately: the latter is mapped to
``INVALID_PARAMS``, and the agent did nothing wrong.

The module is deliberately framework-free: it does NOT import
``brain.mcp_server`` (that would be circular) and knows nothing about MCP error
codes. ``mcp_server`` catches :class:`PayloadCeilingExceeded` and maps it to
``INVALID_PARAMS`` at the call site, which keeps this module testable without
the MCP stack.
"""
from __future__ import annotations

from typing import Any, TypeVar

from .errors import BrainError
from .token_budget import TokenCost, truncate_to_token_budget

T = TypeVar("T")

__all__ = [
    "CONTENT_MARKERS",
    "PayloadCeilingExceeded",
    "apply_content_ceiling",
    "resolve_content_ceiling",
    "cap_rows",
    "check_ceiling",
    "saturation_notice",
    "strip_content_markers",
    "truncate_content",
]


class PayloadCeilingExceeded(BrainError):
    """A caller asked for more than a configured MCP payload ceiling allows.

    Caller-fixable by construction — the message names the parameter, the value
    asked for, and the ceiling — so ``mcp_server`` maps it to ``INVALID_PARAMS``
    rather than an internal error.
    """


def check_ceiling(value: int, *, ceiling: int, param: str) -> int:
    """Return ``value``, or raise if it exceeds ``ceiling``.

    ``ceiling <= 0`` disables the check. Exactly ONE knob can reach here with
    ``0``: ``BRAIN_SHOW_MAX_CONTENT_TOKENS``, which is parsed by
    :func:`brain.config._parse_non_negative_int_env` and is the operator opt-out
    for ``brain_show``'s body ceiling. The other five Wave-3 knobs
    (``BRAIN_SEARCH_MAX_LIMIT``, ``BRAIN_RECALL_MAX_BUDGET_TOKENS``,
    ``BRAIN_GRAPH_ENTITIES_MAX_LIMIT``, ``BRAIN_MCP_ROWS_MAX_LIMIT``,
    ``BRAIN_GRAPH_COMMUNITIES_LIST_LIMIT``) go through
    :func:`brain.config._parse_positive_int_env`, which rejects ``0`` with a
    ``ConfigError`` at load — so for those surfaces there is **no** opt-out and
    this branch is unreachable. Do not document one into existence.

    Raises:
        PayloadCeilingExceeded: ``value > ceiling``, with both numbers named so
            the agent can re-ask smaller instead of guessing.
    """
    if ceiling <= 0:
        return value
    if value > ceiling:
        raise PayloadCeilingExceeded(
            f"{param}={value} exceeds the configured ceiling of {ceiling}; "
            f"re-call with {param} <= {ceiling}"
        )
    return value


def cap_rows(rows: list[T], *, limit: int) -> tuple[list[T], bool]:
    """Return ``(rows[:limit], saturated)``.

    ``saturated`` is ``len(rows) > limit`` — NOT ``len(result) == limit``, which
    is ambiguous when the true count equals the limit exactly. Callers therefore
    have to over-fetch by one row for the flag to mean anything; that is
    cheaper than lying.

    ``limit <= 0`` is NOT an opt-out here and never was. Every row cap that
    reaches this function comes from a ``_parse_positive_int_env`` knob, so no
    environment can produce it; the only way in is a hand-built ``Config``.
    It used to return every row with ``saturated=False``, which was a lie in
    the one place it could be reached: both graph tools fetch
    ``LIMIT effective_limit + 1``, so a ``0`` ceiling fetched ONE row and this
    reported it complete. A one-row answer presented as the whole answer is
    precisely the silent cut this module exists to forbid, so a non-positive
    limit now fails loudly instead.

    Callers whose return type is a bare list cannot carry the flag in an
    envelope; see :func:`saturation_notice`.

    Raises:
        ValueError: ``limit < 1``. Not :class:`PayloadCeilingExceeded` — that
            maps to ``INVALID_PARAMS`` and would blame the agent for what is an
            operator/programming error.
    """
    if limit < 1:
        raise ValueError(
            f"cap_rows requires limit >= 1 (got {limit}); a row cap of 0 would "
            "report a truncated list as complete"
        )
    return list(rows[:limit]), len(rows) > limit


def saturation_notice(
    rows: list[dict[str, Any]], *, saturated: bool
) -> list[dict[str, Any]]:
    """Flag a truncated **bare list** by marking its last element.

    Three MCP tools (``brain_backlinks`` / ``brain_links`` / ``brain_orphans``)
    return ``list[dict]``. Wrapping them in an envelope would break every
    existing caller, and appending a sentinel element would put a non-data row
    in a data list. So the flag rides as an additive
    ``"more_available": True`` key on the FINAL returned element, and only when
    the list was actually cut.

    Returns a new list (the input rows are never mutated). ``saturated=False``
    or an empty list returns the rows unchanged.
    """
    if not saturated or not rows:
        return list(rows)
    return [*rows[:-1], {**rows[-1], "more_available": True}]


def resolve_content_ceiling(requested: int | None, *, configured: int) -> int:
    """Resolve ``brain_show``'s body ceiling: ``None`` → ``configured``.

    An explicit value may only LOWER the ceiling. ``0`` is deliberately NOT a
    caller-side opt-out — accepting it would let any caller disable the ceiling
    it is subject to. The opt-out is the operator's env var, which reaches here
    as ``configured=0`` and disables :func:`check_ceiling` for everyone.

    Raises:
        PayloadCeilingExceeded: ``requested`` is below 1 or above ``configured``.
    """
    if requested is None:
        return configured
    if requested < 1:
        raise PayloadCeilingExceeded("max_content_tokens must be >= 1")
    return check_ceiling(requested, ceiling=configured, param="max_content_tokens")


CONTENT_MARKERS = (
    "content_omitted",
    "content_truncated",
    "content_truncated_recovery",
    "content_tokens",
    "summary_unavailable",
)
"""Keys :func:`apply_content_ceiling` may add. Emitted only on the paths that
produce them, so an unbounded normal payload stays byte-identical.

**This tuple is now guarded in BOTH directions**, which it was not when the
markers shipped.

*Deleting* a marker here goes red in ``tests/test_mcp_show_ceiling.py``
(verified 2026-08-13 by removing ``content_truncated_recovery``: both
confidential-withhold tests fail) — so a key can never quietly stop being
stripped. Note *which* assertion catches it on each path: the ``summary_only``
test iterates this tuple, but that loop cannot see a deleted marker the path
never emits, so there it is the ``len(CONTENT_MARKERS) >= 5`` guard-the-guard
that fires; the truncation twin catches it independently by name.

*Adding* a marker to :func:`apply_content_ceiling` and forgetting it here — the
direction that would leak a key past the confidential branch with the whole
suite green — is caught by
``test_every_key_apply_content_ceiling_adds_is_declared_in_content_markers``
(``tests/test_mcp_limits_unit.py``). It walks all eight paths through the
function (``summary_only`` × ``has_summary`` × over/under the cap) and asserts
the union of keys they add ``==`` this tuple. Both mutations were verified
2026-08-14: an undeclared emitted key and a declared-but-unemitted entry each
redden it, each naming the offending key. This tuple is therefore now the same
bidirectional shape as the recall payload's guard (``measure_recall`` vs
``brain_recall``, compared as KEY SETS with both ``missing`` and ``extra``
asserted).

**What the gate still cannot catch.** It knows only what
:func:`apply_content_ceiling` adds. A marker-like key added to a ``brain_show``
payload *anywhere else* — in ``mcp_server`` around the call, or in a future
sibling helper — is invisible to it and would still ride past
:func:`strip_content_markers`. The gate binds this one function; keep the
markers in it."""


def apply_content_ceiling(
    payload: dict[str, Any],
    *,
    summary_only: bool,
    has_summary: bool,
    max_tokens: int,
    cost: TokenCost,
) -> dict[str, Any]:
    """Bound a ``brain_show`` payload's ``content``, marking every cut.

    Returns a NEW payload. Two independent reductions, in order:

    1. ``summary_only`` drops the body in favour of the ingest-time summary
       (``content=None`` + ``content_omitted``). When the document has **no**
       summary it degrades to the body under the normal ceiling and sets
       ``summary_unavailable`` — returning nothing useful to a well-formed
       request is the worse failure.
    2. Whatever body survives is capped at ``max_tokens``
       (``content_truncated`` + ``content_tokens`` +
       ``content_truncated_recovery`` when cut — the last names what to do
       next, which Task 3.3 requires of every marker).

    A payload whose body is already under the ceiling comes back unchanged, so
    the caller's byte-identical guarantee holds.
    """
    out = dict(payload)
    if summary_only:
        if has_summary:
            out["content"] = None
            out["content_omitted"] = (
                "summary_only=true; re-call with summary_only=false for the body"
            )
        else:
            out["summary_unavailable"] = True
    body = out.get("content")
    if isinstance(body, str):
        kept, was_truncated, tokens = truncate_content(
            body, max_tokens=max_tokens, cost=cost
        )
        if was_truncated:
            out["content"] = kept
            out["content_truncated"] = True
            out["content_tokens"] = tokens
            # Task 3.3: the marker must NAME the recovery path, not merely
            # announce the cut. `content_omitted` (the summary_only sibling)
            # already does; this is the token-cut half, which previously left
            # the agent to infer what to do next. Kept as a separate key so
            # `content_truncated` stays the documented `True` boolean.
            out["content_truncated_recovery"] = (
                "body truncated to the configured ceiling; re-call "
                "brain_show with summary_only=true for the gist, or read the "
                "full document via the CLI (`brain show <id>`)"
            )
    return out


def strip_content_markers(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` without any :data:`CONTENT_MARKERS`.

    Used where a LATER rule withholds the body outright (F6 confidentiality):
    the withheld payload must be exactly the withheld shape, never a mix
    carrying a marker that implies a body was returned.
    """
    return {k: v for k, v in payload.items() if k not in CONTENT_MARKERS}


def truncate_content(
    text: str, *, max_tokens: int, cost: TokenCost
) -> tuple[str, bool, int]:
    """Return ``(text_or_prefix, truncated, tokens)``.

    ``tokens`` is always the measured cost of the string RETURNED, so a caller
    that reports it is reporting what it actually shipped.

    ``max_tokens <= 0`` returns ``text`` unchanged with ``truncated=False``.
    That is the ``BRAIN_SHOW_MAX_CONTENT_TOKENS=0`` operator opt-out arriving
    here — the one knob in the family that accepts ``0`` (see
    :func:`check_ceiling`). It is NOT a silent cut: nothing is dropped.

    Delegates the cut to :func:`brain.token_budget.truncate_to_token_budget`
    (binary search on codepoint length, so a prefix can never split a
    multi-byte character) rather than re-implementing it.
    """
    if max_tokens <= 0:
        return text, False, cost(text)
    total = cost(text)
    if total <= max_tokens:
        return text, False, total
    prefix = truncate_to_token_budget(text, cost=cost, budget=max_tokens)
    return prefix, True, cost(prefix)
