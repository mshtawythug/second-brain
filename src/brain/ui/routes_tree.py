"""`GET /api/tree` — the left rail, from one indexed query."""
from __future__ import annotations

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import queries as ui_queries
from ._http import context_of, db_guard, ok
from .tree import build_tree


async def tree(request: Request) -> JSONResponse:
    """The full vault as a nested tree.

    One query, one pure fold. The empty case names ``brain vault sync`` rather
    than rendering a blank panel, because an empty rail on a populated brain
    almost always means "exported nothing yet", not "you have no notes".

    CONFIDENTIAL TITLES ARE GATED HERE, on ``serve_confidential_titles`` and
    not on ``serve_confidential_bodies``. This rail paints on load, beside the
    recent rail and the search box that both already filter — so until this
    gate existed it was the one unprompted surface naming every confidential
    document in the vault. The flag is computed at the route, like every other
    context-derived predicate in the package, and the query owns the SQL.
    """
    ctx = context_of(request)
    strict = not ctx.serve_confidential_titles
    try:
        with ctx.connect() as conn:
            rows = ui_queries.iter_tree_rows(conn, exclude_confidential=strict)
    except psycopg.Error as exc:
        raise db_guard(exc) from exc

    root = build_tree(rows)
    payload = root.to_payload()
    payload["count"] = len(rows)
    payload["empty_hint"] = (
        None
        if rows
        else "No notes are exported to the vault yet. Run `brain vault sync`."
    )
    return ok(payload)
