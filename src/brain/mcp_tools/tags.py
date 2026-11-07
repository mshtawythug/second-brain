"""MCP tag tools: list tags, suggest merges and summarize a tag, READ-ONLY (RM-21, RM-55).

The twins of ``brain tags list``, ``brain tags suggest-merges`` and ``brain tags summarize``, over
:mod:`brain.tag_mapping` and :mod:`brain.tag_summary`.

**No write tool, by decision (RM-21.D1).** Renaming or merging a tag rewrites every document,
chunk, vault file and saved search that carries it: a corpus-wide change that is an owner action
at the terminal (``brain tags rename`` / ``merge``, dry run by default), not something a hosted
model does. Both tools here run inside a ``READ ONLY`` transaction, so a write slipped into one
fails in Postgres rather than landing.

**The F6 lens is in the predicate.** A tag's name is the owner's text, and a tag found only on
confidential documents would tell a caller outside the trust boundary what those documents are
about; a count over both tiers would say withheld documents exist. So both tools read through
``confidential_lens`` (default: exclude confidential), and the counts, the overlap and the
suggestions are all computed over the visible tier only.

**``brain_tags_summarize`` lenses its INPUT** (RM-55). A document's summary is derived from its body
and the synthesis comes back as prose derived from the summaries, so redacting the output could
never be enough: the lens is in the fetch predicate, and a confidential summary never reaches the
prompt. The fetch's READ ONLY transaction is over before the chat model runs.

**Over HTTP it stops at the call deadline** (sec-124 LOW-1). The guard answers 504 at
``--call-timeout``, but the call keeps its worker, and the chat its Ollama slot, until the tool
returns. So the deadline starts when the tool does. After the fetch, the chat gets only the time
left, split across ``chat_json``'s :data:`~brain.chat.CHAT_ATTEMPTS` (each attempt's wait for the
model gets its share, at most ``enrich_timeout_seconds``). Once a share would fall below
:data:`MIN_CHAT_ATTEMPT_SECONDS`, the tool refuses without calling the chat, because an attempt
that short would time out and read as "Ollama is not available". The fetch itself is not cut
short; it runs on the database's own timeouts, and a slow one simply leaves the chat less time, or
none. On stdio there is no deadline.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from .. import chat
from ..ask_deadline import Deadline
from ..errors import BrainError
from ..mcp_compat import MCPServerProtocol
from ..tag_mapping import read_document_tags, suggest_merges, tag_counts
from ..tag_summary import (
    TAG_SUMMARY_DEFAULT_LIMIT,
    TAG_SUMMARY_FAILURES,
    TAG_SUMMARY_LIMIT_CEILING,
    TagSummaryInputError,
    describe_tag_summary_failure,
    fetch_tag_documents,
    resolve_tag,
    synthesize,
)
from ._context import ContextSlot, ToolContext

_CONTEXT = ContextSlot(__name__)

#: The clock the HTTP call deadline is read on. A module global so a test can pass a fake one.
_clock: Callable[[], float] = time.monotonic

#: The least time one chat attempt is given. Below it the call takes the deadline path instead.
MIN_CHAT_ATTEMPT_SECONDS = 1.0

#: ``brain_tags_summarize``'s answer once the server's call deadline has passed, as ``brain_ask``'s.
DEADLINE_PASSED = "brain_tags_summarize passed this server's call deadline"

#: ``brain_tags_list``'s default and ceiling for ``limit``.
LIST_LIMIT_DEFAULT = 200
LIST_LIMIT_CEILING = 1000
#: ``brain_tags_suggest_merges``'s default and ceiling for ``limit``.
SUGGEST_LIMIT_DEFAULT = 25
SUGGEST_LIMIT_CEILING = 100


def _check_limit(ctx: ToolContext, limit: int, ceiling: int) -> None:
    if not 1 <= limit <= ceiling:
        raise ctx.error(INVALID_PARAMS, f"limit must be between 1 and {ceiling}; got {limit}")


@contextmanager
def _read_only(ctx: ToolContext, state: Any) -> Iterator[psycopg.Connection[Any]]:
    """One connection and one READ ONLY transaction; a Postgres failure maps class-only."""
    try:
        with ctx.connection(state) as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            yield conn
    except psycopg.Error as exc:
        raise ctx.wrap_db_error(exc) from exc


def brain_tags_list(
    include_confidential: bool = False, limit: int = LIST_LIMIT_DEFAULT
) -> dict[str, Any]:
    """Every tag and how many documents carry it, most used first. Read-only.

    Returns ``{tags: [{tag, count}], truncated}``; ``truncated`` is true when more than ``limit``
    tags exist (``limit``: 1-1000, default 200). F6: confidential documents are left out of the
    counts, and a tag found only on them is not listed, unless ``include_confidential=true``.
    Renaming and merging tags is terminal-only (``brain tags rename`` / ``merge``).
    """
    ctx = _CONTEXT.get()
    _check_limit(ctx, limit, LIST_LIMIT_CEILING)
    state = ctx.get_state()
    with _read_only(ctx, state) as conn:
        counts = tag_counts(conn, sensitivity=ctx.confidential_lens(include_confidential))
    return {
        "tags": [{"tag": tag, "count": count} for tag, count in counts[:limit]],
        "truncated": len(counts) > limit,
    }


def brain_tags_suggest_merges(
    include_confidential: bool = False, limit: int = SUGGEST_LIMIT_DEFAULT
) -> dict[str, Any]:
    """Pairs of tags that are probably one tag, ranked, with evidence. Read-only.

    A pair qualifies by close spelling (ratio >= 0.85), singular/plural, or co-occurrence
    (document sets overlapping with Jaccard >= 0.8 on at least 2 shared documents). Returns
    ``{suggestions: [{keep, merge, keep_count, merge_count, reasons, ratio, overlap, shared}]}``,
    ``keep`` being the more used tag (``limit``: 1-100, default 25). F6 as ``brain_tags_list``.
    Nothing is merged: the owner runs ``brain tags merge <merge> --into <keep>`` at the terminal.
    """
    ctx = _CONTEXT.get()
    _check_limit(ctx, limit, SUGGEST_LIMIT_CEILING)
    state = ctx.get_state()
    with _read_only(ctx, state) as conn:
        doc_tags = read_document_tags(
            conn, sensitivity=ctx.confidential_lens(include_confidential)
        )
    return {"suggestions": [s.to_json() for s in suggest_merges(doc_tags, limit=limit)]}


class SummaryDeadlineError(BrainError):
    """Too little time is left on the call deadline to give the chat."""


def chat_timeout(enrich_timeout_s: float, deadline: Deadline | None) -> float | None:
    """The timeout for each ``chat_json`` attempt, or ``None`` for the chat's default (stdio).

    Raises:
        SummaryDeadlineError: an attempt's share of the time left is below
            :data:`MIN_CHAT_ATTEMPT_SECONDS` (the deadline passed, or nearly has).
    """
    if deadline is None:
        return None
    share = deadline.remaining() / chat.CHAT_ATTEMPTS
    if share < MIN_CHAT_ATTEMPT_SECONDS:
        raise SummaryDeadlineError("too little of the call deadline is left for the chat")
    return min(enrich_timeout_s, share)


def brain_tags_summarize(
    tag: str, include_confidential: bool = False, limit: int = TAG_SUMMARY_DEFAULT_LIMIT
) -> dict[str, Any]:
    """A short synthesis of what one tag's most recent documents say, with [N] citations.

    Reads the ``limit`` most recent non-draft documents carrying ``tag`` (``limit``: 1-50, default
    20) and sends their stored summaries (titles where there is none) to the local chat model.
    Returns ``{tag, summary, citations: [{ref, document_id, title, date}], considered, dropped}``:
    ``citations`` are the documents the summary cites, ``considered`` how many reached the model,
    ``dropped`` how many did not fit its input budget. A tag no visible document carries gives an
    empty ``summary``. F6: confidential documents are left out of the INPUT unless
    ``include_confidential=true``. Read-only: nothing is written or cached.
    """
    ctx = _CONTEXT.get()
    _check_limit(ctx, limit, TAG_SUMMARY_LIMIT_CEILING)
    try:
        canonical = resolve_tag(tag)
    except TagSummaryInputError as exc:
        raise ctx.error(INVALID_PARAMS, str(exc)) from exc
    state = ctx.get_state()
    cap: float | None = state.call_timeout_s
    deadline = None if cap is None else Deadline(_clock, cap)
    with _read_only(ctx, state) as conn:
        docs = fetch_tag_documents(
            conn, canonical, limit=limit, sensitivity=ctx.confidential_lens(include_confidential)
        )
    # The transaction is over (a per-call connection closed) before the model runs.
    try:
        timeout = chat_timeout(state.cfg.enrich_timeout_seconds, deadline)
    except SummaryDeadlineError:
        raise ctx.error(INTERNAL_ERROR, DEADLINE_PASSED) from None
    try:
        result = synthesize(
            canonical,
            docs,
            chat=chat.chat_json,
            cfg=state.cfg,
            cost=state.embedder.count_tokens,
            timeout=timeout,
        )
    except TAG_SUMMARY_FAILURES as exc:
        raise ctx.error(INTERNAL_ERROR, describe_tag_summary_failure(exc)) from exc
    return result.to_json()


def register(app: MCPServerProtocol, ctx: ToolContext) -> None:
    """Bind ``ctx`` (refusing a different one), then register the three read-only tools."""
    _CONTEXT.bind(ctx)
    app.tool()(brain_tags_list)
    app.tool()(brain_tags_suggest_merges)
    app.tool()(brain_tags_summarize)
