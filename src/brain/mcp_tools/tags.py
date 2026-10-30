"""MCP tag tools: list tags and suggest merges, READ-ONLY (RM-21).

The twins of ``brain tags list`` and ``brain tags suggest-merges``, over :mod:`brain.tag_mapping`.

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
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from mcp.types import INVALID_PARAMS

from ..mcp_compat import MCPServerProtocol
from ..tag_mapping import read_document_tags, suggest_merges, tag_counts
from ._context import ContextSlot, ToolContext

_CONTEXT = ContextSlot(__name__)

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


def register(app: MCPServerProtocol, ctx: ToolContext) -> None:
    """Bind ``ctx`` (refusing a different one), then register the two read-only tools."""
    _CONTEXT.bind(ctx)
    app.tool()(brain_tags_list)
    app.tool()(brain_tags_suggest_merges)
