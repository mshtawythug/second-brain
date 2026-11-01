"""MCP ``brain_related`` and ``brain_explain``: two read-only ranking diagnostics (RM-12).

``brain_related`` is ``brain ui``'s related-notes panel (``GET /api/notes/{id}/related``) for an
agent, and ``brain_explain`` is ``brain explain --json``. Each returns the same answer its twin
does, from the same function: :func:`brain.related.compute_related` and
:func:`brain.search_pipeline.run_search` with
:func:`brain.format_search.explanation_json`. One module for both (RM-12.D1): neither writes, and
both explain a ranking.

**Both apply the F6 lens, and both default to excluding confidential documents.** MCP serves a
model that may be hosted, outside the trust boundary (``docs/configuration.md``, "The CLI and MCP
treat confidential documents differently, on purpose"). ``brain_related`` gates its ROOT as well
as its candidates: ``compute_related`` filters candidates only, and a related list for a
withheld document is a précis of it, the leak ``brain.ui.routes_related`` describes. The root is
checked before ranking, and the refusal names no title and no candidate. ``brain_explain`` puts
the lens in the search predicate, so a confidential document is not in the match set at all,
rather than returned and redacted.

The root's tier is read with :func:`brain.queries.fetch_document`, the read ``brain_show``'s gate
uses, not with the UI route's one-column read: importing anything under ``brain.ui`` runs its
package ``__init__``, which loads Typer and the doctor modules into this server (RM-12.D2,
``docs/plans/2026-10-29-rm-12-mcp-related-explain.md``).
"""

from __future__ import annotations

from typing import Any

import psycopg
from mcp.types import INVALID_PARAMS

from ..errors import IdPrefixAmbiguous, IdPrefixNotFound, IdPrefixNotHex, IdPrefixTooShort
from ..format_search import explanation_json, search_results_json
from ..mcp_compat import MCPServerProtocol
from ..mcp_limits import PayloadCeilingExceeded, check_ceiling
from ..queries import fetch_document, resolve_document_prefix
from ..related import DEFAULT_RELATED_LIMIT, compute_related
from ..search import hybrid_search
from ..search_pipeline import pipeline_json, run_search
from ..sensitivity import is_confidential
from ..source_kinds import InvalidSourceKind, validate_source_kind
from ._context import ContextSlot, ToolContext

#: The most neighbours ``brain_related`` returns. Twice the panel's default: a related list is a
#: browsing aid, and past twenty the floor-passing tail is rarely worth the tokens. A module
#: constant, not a knob (the ticket's ruling; ``config.py`` is over the ceiling).
RELATED_LIMIT_CEILING = 20

#: ``brain_explain``'s default ``limit``: ``brain explain``'s ``--limit`` default.
DEFAULT_EXPLAIN_LIMIT = 10

_CONTEXT = ContextSlot(__name__)


def _check_limit(ctx: ToolContext, limit: int, *, ceiling: int) -> None:
    """Refuse a non-positive ``limit`` or one above ``ceiling``, as ``INVALID_PARAMS``."""
    if limit < 1:
        raise ctx.error(INVALID_PARAMS, "limit must be >= 1")
    try:
        check_ceiling(limit, ceiling=ceiling, param="limit")
    except PayloadCeilingExceeded as exc:
        raise ctx.error(INVALID_PARAMS, str(exc)) from exc


def _resolve_id(ctx: ToolContext, conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve an id prefix as ``brain_show`` does: every failure is the caller's to fix."""
    try:
        return resolve_document_prefix(conn, prefix)
    except (IdPrefixTooShort, IdPrefixNotHex, IdPrefixNotFound, IdPrefixAmbiguous) as exc:
        raise ctx.error(INVALID_PARAMS, str(exc)) from exc


def brain_related(
    id: str,
    limit: int = DEFAULT_RELATED_LIMIT,
    include_confidential: bool = False,
) -> dict[str, Any]:
    """Documents related to one document, best first: ``brain ui``'s related-notes panel.

    ``id`` is a document id or a prefix of 6+ hex characters, resolved as ``brain_show``
    resolves it. Ranking fuses a full-text leg (the document's own distinctive terms) and a
    vector leg (its average embedding), with the same cosine floor ``brain_search`` uses; the
    floor is echoed as ``vector_sim_floor``. No embed call is made.

    Returns ``{id, related: [{id, title, vault_path, source, score, snippet}],
    vector_sim_floor}``. ``snippet`` is a passage of the related document, at most 240
    characters. An empty ``related`` list is a normal answer: no candidate cleared the ranking
    floors. On a brain of under about 40 documents that often happens even when similar
    documents exist, because the full-text leg then treats every term as common.

    ``limit`` defaults to 10 and may not exceed 20 (``INVALID_PARAMS`` above that, or below 1).

    F6: confidential documents are excluded unless ``include_confidential=true``. That covers
    the document asked about too: a confidential one is refused with ``INVALID_PARAMS``
    (pass ``include_confidential=true``), because its related list would describe what is
    withheld. An unknown, ambiguous, too-short or non-hex ``id`` is ``INVALID_PARAMS``; a
    database failure is ``INTERNAL_ERROR`` naming only its class.
    """
    ctx = _CONTEXT.get()
    state = ctx.get_state()
    _check_limit(ctx, limit, ceiling=RELATED_LIMIT_CEILING)
    floor = state.cfg.vector_sim_floor
    try:
        with ctx.connection(state) as conn:
            document_id = _resolve_id(ctx, conn, id)
            if not include_confidential:
                root = fetch_document(conn, document_id)
                if root is not None and is_confidential(root.sensitivity):
                    # Over HTTP (RM-46 sec-118 #2b) the document answers as an
                    # unknown prefix does: no hint that it exists.
                    hidden = getattr(state, "hide_confidential", False)
                    raise ctx.error(
                        INVALID_PARAMS,
                        str(IdPrefixNotFound(id))
                        if hidden
                        else "document is confidential; pass include_confidential=true",
                    )
            related = compute_related(
                conn,
                document_id,
                limit=limit,
                vector_sim_floor=floor,
                exclude_confidential=not include_confidential,
            )
    except psycopg.Error as exc:
        raise ctx.wrap_db_error(exc) from exc
    return {
        "id": document_id,
        "related": [
            {
                "id": doc.id,
                "title": doc.title,
                "vault_path": doc.vault_path,
                "source": doc.source,
                "score": float(doc.score),
                "snippet": doc.snippet,
            }
            for doc in related
        ],
        "vector_sim_floor": floor,
    }


def brain_explain(
    query: str,
    limit: int = DEFAULT_EXPLAIN_LIMIT,
    source: str | None = None,
    tag: str | None = None,
    since_days: int | None = None,
    include_confidential: bool = False,
) -> dict[str, Any]:
    """Why a search ranked what it did: ``brain explain --json``, per result.

    Runs the same search as ``brain_search`` and returns, for each result, its ranking
    diagnostic: full-text rank and score, vector rank and cosine, each leg's RRF contribution,
    the recency boost, the final score, the best-matching chunk, and the filters applied.
    Snippets are left out; use ``brain_search`` or ``brain_show`` for text.

    Returns ``{query, results: [{id, title, source_kind, score, content_type, tags, explain}],
    fallback, fallback_reason, fallback_transient, fallback_note, stages}``. The four
    ``fallback*`` keys and ``stages`` mean what they mean on ``brain_search``: when the query
    embed fails the search runs on full-text matching only, ``fallback`` is ``"fts_only"``, and
    every ``vector_rank`` is ``null``.

    Filters: ``source`` (a source kind: ``manual``, ``krisp``, ``gmail``, ``slack``; anything
    else is ``INVALID_PARAMS``), ``tag``, ``since_days`` (at least 1, else ``INVALID_PARAMS``).
    ``limit`` defaults to 10 and may not exceed ``BRAIN_SEARCH_MAX_LIMIT`` (``INVALID_PARAMS``
    above that, or below 1).

    F6: confidential documents are excluded from the match set unless
    ``include_confidential=true``. A database failure is ``INTERNAL_ERROR`` naming only its
    class.
    """
    ctx = _CONTEXT.get()
    state = ctx.get_state()
    cfg = state.cfg
    _check_limit(ctx, limit, ceiling=cfg.search_max_limit)
    # 0 would search with no window while ``matched_filters`` echoed ``since_days: 0``, and a
    # negative value matches nothing; both are the caller's to fix, so refuse them up front.
    if since_days is not None and since_days < 1:
        raise ctx.error(INVALID_PARAMS, "since_days must be >= 1")
    if source is not None:
        try:
            validate_source_kind(source)
        except InvalidSourceKind as exc:
            raise ctx.error(INVALID_PARAMS, str(exc)) from exc
    try:
        with ctx.connection(state) as conn:
            # No ``except EmbedError``: the query embed is this tool's only embed, and
            # ``run_search``'s fallback absorbs it (reported in the ``fallback*`` keys).
            outcome = run_search(
                hybrid_search,
                conn,
                embedder=state.embedder,
                query=query,
                limit=limit,
                source_kind=source,
                tag=tag,
                since_days=since_days,
                vector_sim_floor=cfg.vector_sim_floor,
                recency_halflife_days=cfg.recency_halflife_days,
                snippet_context_tokens=cfg.snippet_context_tokens,
                snippet_max_chars=cfg.snippet_max_chars,
                explain=True,
                sensitivity=ctx.confidential_lens(include_confidential),
            )
    except psycopg.Error as exc:
        raise ctx.wrap_db_error(exc) from exc
    results = outcome.results
    return {
        "query": query,
        "results": [
            {
                **{key: value for key, value in base.items() if key != "snippet"},
                "explain": explanation_json(result.explain),
            }
            for result, base in zip(results, search_results_json(results), strict=True)
        ],
        **pipeline_json(outcome),
    }


def register(app: MCPServerProtocol, ctx: ToolContext) -> None:
    """Bind ``ctx`` (refusing a different one), then register both tools on ``app``."""
    _CONTEXT.bind(ctx)
    app.tool()(brain_related)
    app.tool()(brain_explain)
