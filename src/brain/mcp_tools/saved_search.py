"""MCP saved-search tools: list, add, peek-check and remove watches (RM-15).

The twins of ``brain watch list / add / check --peek / rm``, over the same core
(:mod:`brain.saved_search`, :mod:`brain.saved_search_check`).

**``brain_watch_check`` only peeks.** It reports what is new without recording it or moving the
watch's window: the hits are the owner's morning-brief notifications, and an agent must never
consume them (RM-15's ticket). It applies the F6 lens in the search predicate and defaults to
excluding confidential documents, as every retrieval tool here does.

**No tool returns a count that includes confidential documents.** ``brain_watch_add`` seeds a
watch with its current matches but does not say how many, and ``brain_watch_list`` and
``brain_watch_remove`` report no hit counts: a count taken over both tiers would tell a caller
outside the trust boundary that withheld matches exist (the membership oracle
``mcp_server._confidential_lens`` describes). A watch's name, query and filters are the owner's
own text, not document content, so they are returned as stored.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from ..errors import PersonAmbiguous, PersonNotFound
from ..mcp_compat import MCPServerProtocol
from ..saved_search import (
    MODE_LEXICAL,
    MODE_SEMANTIC,
    SavedSearch,
    SavedSearchError,
    WatchFilters,
    get_watch,
    list_watches,
    remove_watch,
    watch_tables_present,
)
from ..saved_search_check import (
    CheckResult,
    Matcher,
    add_watch,
    build_matcher,
    peek_enabled,
    peek_watch,
)
from ..source_kinds import InvalidSourceKind
from ._context import ContextSlot, ToolContext

_CONTEXT = ContextSlot(__name__)


@contextmanager
def _session(ctx: ToolContext, state: Any) -> Iterator[psycopg.Connection[Any]]:
    """One connection and ONE transaction for the call, every core failure mapped to MCP.

    The transaction makes a tool call commit whether the server's connection is autocommit
    (production) or not (a per-call test connection), and rolls back a refused call.
    """
    try:
        with ctx.connection(state) as conn, conn.transaction():
            if not watch_tables_present(conn):
                raise ctx.error(
                    INTERNAL_ERROR, "saved searches need migration 029: run `brain init`"
                )
            try:
                yield conn
            except (SavedSearchError, InvalidSourceKind, PersonNotFound, PersonAmbiguous) as exc:
                raise ctx.error(INVALID_PARAMS, str(exc)) from exc
    except psycopg.Error as exc:
        raise ctx.wrap_db_error(exc) from exc


def _matcher(state: Any) -> Matcher:
    def _embedder() -> Any:
        return state.embedder

    return build_matcher(state.cfg, embedder=_embedder)


def _watch_json(watch: SavedSearch) -> dict[str, Any]:
    return {
        "name": watch.name,
        "query": watch.query,
        "mode": watch.mode,
        "enabled": watch.enabled,
        "filters": watch.filters.to_json(),
        "created_at": watch.created_at.isoformat(),
        "last_checked_at": watch.last_checked_at.isoformat(),
    }


def _result_json(result: CheckResult) -> dict[str, Any]:
    return {
        "watch": result.watch,
        "mode": result.mode,
        "skipped": result.skipped,
        "unrecorded": result.unrecorded,
        "capped": result.capped,
        "new": [
            {"id": m.document_id, "title": m.title, "source": m.source_kind}
            for m in result.matches
        ],
    }


def brain_watch_list() -> dict[str, Any]:
    """List the owner's saved searches ("watches"), by name.

    Returns ``{watches: [{name, query, mode, enabled, filters, created_at, last_checked_at}]}``.
    ``mode`` is ``lexical`` (matches by words) or ``semantic`` (by meaning); ``enabled`` is false
    for a paused watch. Each morning's brief lists the documents that newly match each enabled
    watch. No document is named here. A database failure is ``INTERNAL_ERROR`` naming only its
    class.
    """
    ctx = _CONTEXT.get()
    state = ctx.get_state()
    with _session(ctx, state) as conn:
        watches = list_watches(conn)
    return {"watches": [_watch_json(watch) for watch in watches]}


def brain_watch_add(
    name: str,
    query: str,
    source: str | None = None,
    tag: str | None = None,
    person: str | None = None,
    kind: str | None = None,
    without_tag: str | None = None,
    semantic: bool = False,
) -> dict[str, Any]:
    """Save a search ("watch"); each morning's brief then lists documents that newly match it.

    ``name`` is a short label (unique, one line, at most 80 characters); ``query`` is the search.
    Filters, each optional and validated as ``brain_search`` validates them: ``source`` (a source
    kind: ``manual``, ``krisp``, ``gmail``, ``slack``), ``tag``, ``person`` (resolved through the
    people directory), ``kind`` (a content type such as ``transcript`` or ``note``) and
    ``without_tag``. ``semantic=true`` matches by meaning and needs the embedder; the default
    matches by words.

    The watch's current matches are recorded as already seen, so only later documents are
    reported. Returns ``{watch: {name, query, mode, enabled, filters, created_at,
    last_checked_at}}``. A taken name, a bad filter, or a semantic watch while the embedder is
    unavailable is ``INVALID_PARAMS``, and nothing is saved.
    """
    ctx = _CONTEXT.get()
    state = ctx.get_state()
    filters = WatchFilters(
        source=source, tag=tag, person=person, kind=kind, without_tag=without_tag
    )
    with _session(ctx, state) as conn:
        watch, _seeded = add_watch(
            conn,
            name=name,
            query=query,
            filters=filters,
            mode=MODE_SEMANTIC if semantic else MODE_LEXICAL,
            matcher=_matcher(state),
        )
    return {"watch": _watch_json(watch)}


def brain_watch_check(
    name: str | None = None, include_confidential: bool = False
) -> dict[str, Any]:
    """What is new for a watch (or every enabled watch) right now, WITHOUT recording it.

    A preview only: nothing is marked and the watch's window does not move, so the owner's
    morning brief still reports the same documents. Returns ``{results: [{watch, mode, skipped,
    unrecorded, capped, new: [{id, title, source}]}]}``. ``skipped`` is a reason (``skipped,
    embedder unavailable`` for a semantic watch while the embedder is down) and ``new`` is then
    empty. A check looks at 50 documents at most: ``unrecorded`` counts further lexical matches
    (under the same lens), and ``capped`` says a semantic check reached 50.

    F6: confidential documents are excluded unless ``include_confidential=true``. An unknown
    ``name`` is ``INVALID_PARAMS``.
    """
    ctx = _CONTEXT.get()
    state = ctx.get_state()
    sensitivity = ctx.confidential_lens(include_confidential)
    matcher = _matcher(state)
    with _session(ctx, state) as conn:
        results = (
            [peek_watch(conn, name, matcher, sensitivity=sensitivity)]
            if name is not None
            else peek_enabled(conn, matcher, sensitivity=sensitivity)
        )
    return {"results": [_result_json(result) for result in results]}


def brain_watch_remove(name: str, confirm: bool = False) -> dict[str, Any]:
    """Remove a watch and its recorded hits. **Irreversible.**

    ``confirm`` defaults to ``False``: the call is refused with ``INVALID_PARAMS`` naming what
    would be removed, and nothing changes. Pass ``confirm=true`` to remove. Returns
    ``{removed: name}``. An unknown ``name`` is ``INVALID_PARAMS``.
    """
    ctx = _CONTEXT.get()
    state = ctx.get_state()
    with _session(ctx, state) as conn:
        watch = get_watch(conn, name)
        if not confirm:
            raise ctx.error(
                INVALID_PARAMS,
                "removing a watch is destructive — re-call with confirm=true to proceed. "
                f"Nothing was changed. Would remove: watch {watch.name!r} and its recorded hits",
            )
        remove_watch(conn, watch.name)
    return {"removed": watch.name}


def register(app: MCPServerProtocol, ctx: ToolContext) -> None:
    """Bind ``ctx`` (refusing a different one), then register the four tools on ``app``."""
    _CONTEXT.bind(ctx)
    app.tool()(brain_watch_list)
    app.tool()(brain_watch_add)
    app.tool()(brain_watch_check)
    app.tool()(brain_watch_remove)
