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
    """
    ctx = context_of(request)
    try:
        with ctx.connect() as conn:
            rows = ui_queries.iter_tree_rows(conn)
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
