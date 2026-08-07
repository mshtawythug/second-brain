"""Small shared helpers for the route modules.

Exists so the four ``routes_*`` modules stay genuinely thin instead of each
re-deriving how to reach the context, parse a JSON body, or shape a response.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from .errors import UiBadRequest, wrap_db_error

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .context import UiContext


def context_of(request: Request) -> UiContext:
    """The :class:`~brain.ui.context.UiContext` this app was built with."""
    ctx: UiContext = request.app.state.ui
    return ctx


def ok(payload: Any, *, status: int = 200) -> JSONResponse:
    """A JSON response. ``no-store`` is added by the security middleware."""
    return JSONResponse(payload, status_code=status)


async def json_body(request: Request) -> dict[str, Any]:
    """Parse a request body as a JSON object.

    The Content-Type gate already ran in middleware, so reaching here with
    unparseable bytes means a malformed body rather than a wrong media type —
    hence 400 rather than 415.
    """
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UiBadRequest("body must be valid JSON", code="invalid_json") from exc
    if not isinstance(parsed, dict):
        raise UiBadRequest("body must be a JSON object", code="invalid_body")
    return parsed


def db_guard(exc: psycopg.Error) -> Exception:
    """Map a psycopg failure onto a leak-free 503."""
    return wrap_db_error(exc)
