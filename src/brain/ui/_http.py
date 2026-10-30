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

from .errors import UiBadRequest, UiPayloadTooLarge, wrap_db_error

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
    return parse_json_object(await request.body())


def parse_json_object(raw: bytes) -> dict[str, Any]:
    """Parse already-read bytes as a JSON object; empty bytes mean ``{}``."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UiBadRequest("body must be valid JSON", code="invalid_json") from exc
    if not isinstance(parsed, dict):
        raise UiBadRequest("body must be a JSON object", code="invalid_body")
    return parsed


async def read_body_capped(request: Request, limit: int) -> bytes:
    """Read the body, refusing (413) anything over ``limit`` bytes.

    The cap is enforced on the stream, never after buffering (Ingest spec §7.3,
    C7): an honest ``Content-Length`` over the cap is refused before a byte is
    read, and a lying or absent one (chunked transfer) is refused by the counter
    the moment the running total passes the cap.
    """
    declared = declared_length(request)
    if declared is not None and declared > limit:
        raise UiPayloadTooLarge(
            f"request body must be at most {limit} bytes", code="body_too_large"
        )
    received = bytearray()
    async for piece in request.stream():
        received.extend(piece)
        if len(received) > limit:
            raise UiPayloadTooLarge(
                f"request body must be at most {limit} bytes", code="body_too_large"
            )
    return bytes(received)


#: A declared length with more digits than this is larger than any cap here, and
#: is reported as this bound instead of being converted (CPython refuses int()
#: past 4300 digits, and a 500 there would be a free crash).
_MAX_LENGTH_DIGITS = 18


def declared_length(request: Request) -> int | None:
    """The request's ``Content-Length`` as an int, or ``None`` when absent or malformed.

    Only a precheck: a lying or absent length is caught by the caller's stream
    counter, so a malformed header is treated like a missing one.
    """
    declared = request.headers.get("content-length", "")
    # isascii(): "²".isdigit() is true and int() rejects it.
    if not (declared.isascii() and declared.isdigit()):
        return None
    digits = declared.lstrip("0") or "0"
    if len(digits) > _MAX_LENGTH_DIGITS:
        return int(10**_MAX_LENGTH_DIGITS)
    return int(digits)


def db_guard(exc: psycopg.Error) -> Exception:
    """Map a psycopg failure onto a leak-free 503."""
    return wrap_db_error(exc)
