"""Pure-ASGI guards for the MCP HTTP transport (RM-46).

``brain mcp serve`` stacks these around the SDK's streamable-HTTP app (outermost
first; see :func:`brain.mcp_http.build_http_app`):

- :class:`RequestLogMiddleware`: one line per request, naming a fixed route
  label (never the raw, attacker-chosen path), the socket's client address, the
  status and the time. It never reads a header value or a body.
- :class:`HealthzRoute` / :class:`ReadyzRoute`: liveness (static, no auth) and
  readiness (behind the token, no corpus statistics).
- :class:`ResponseGuardMiddleware`: the per-call deadline (504) and the result
  size cap (502). The endpoint answers JSON, not a stream, so the response is
  held until it is complete; that is what lets an over-cap result be replaced.
- :class:`ConfidentialGateMiddleware`: refuses a ``tools/call`` that asks for
  ``include_confidential`` unless the server was started with
  ``--include-confidential`` (RM-46.D4; F6's egress rule). It reads the body
  STRICTLY, because it must judge the same request the SDK will run: UTF-8
  only, no duplicate keys (Python keeps the last one, another parser might keep
  the first), no ``NaN``, no batches, bounded nesting, no content encoding. A
  body it cannot read that way is refused, never passed through. The bytes it
  read are replayed to the app unchanged.

Every guard passes ``lifespan`` (and any non-HTTP scope) straight through,
because the SDK app's lifespan runs its session manager. None of them catches an
exception. No SDK import: the guards are testable around a sentinel app.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import anyio

logger = logging.getLogger("brain.mcp.http")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

HEALTHZ_PATH = "/healthz"
READYZ_PATH = "/readyz"
#: Deeper JSON than this is refused before it is parsed. An MCP request nests
#: a handful of levels; the bound keeps the parse linear and recursion-free.
MAX_JSON_DEPTH = 64
#: JSON-RPC "invalid params", as ``mcp.types.INVALID_PARAMS``.
INVALID_PARAMS = -32602
CONFIDENTIAL_REFUSAL = (
    "include_confidential is refused over HTTP: this server was started without "
    "--include-confidential, so confidential documents stay on the machine that owns them"
)

_KNOWN_METHODS = frozenset({"GET", "POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"})
_STRUCTURAL = re.compile(rb'[\[\]{}"\\]')
_PASS, _MALFORMED, _REFUSE = "pass", "malformed", "refuse"
#: A byte-order mark: valid UTF-8, but no JSON parser should have to agree on it.
_BOM = "\ufeff"


async def send_json(send: Send, status: int, payload: Any) -> None:
    """Send a complete JSON response that the app never saw."""
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _is_post(scope: Scope) -> bool:
    """Every HTTP POST, WHATEVER its path (sec-118 #1).

    Keying a guard on ``scope["path"] == endpoint`` let ``POST /mcp%0A`` past
    it: uvicorn decodes the path to ``"/mcp\n"``, and starlette's ``^/mcp$``
    route still matches it, because ``$`` also matches before a final newline.
    A guard that checks every POST cannot be routed around.
    """
    return bool(scope["type"] == "http" and scope["method"] == "POST")


class PathAllowlistMiddleware:
    """Answer 404 for any path that is not EXACTLY one of ``paths``, before anything runs.

    Plain set membership, never a pattern: the SDK's route regex is exactly what
    ``/mcp\n`` slipped past (sec-118 #1). Outermost but for the request log, so
    no auth, Host check, guard or route ever sees an unlisted path.
    """

    def __init__(self, app: ASGIApp, *, paths: frozenset[str]) -> None:
        self._app = app
        self._paths = paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._paths:
            await self._app(scope, receive, send)
            return
        await send_json(send, 404, {"error": "not found"})


class RequestLogMiddleware:
    """Log method, route label, status and duration; never a path, header or body."""

    def __init__(self, app: ASGIApp, *, endpoint: str) -> None:
        self._app = app
        self._labels = {endpoint: "endpoint", HEALTHZ_PATH: "healthz", READYZ_PATH: "readyz"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        status = 500
        started = time.monotonic()

        async def record(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self._app(scope, receive, record)
        finally:
            client = scope.get("client")
            method = scope["method"] if scope["method"] in _KNOWN_METHODS else "OTHER"
            logger.log(
                logging.INFO if status < 400 else logging.WARNING,
                "%s %s %s %d %dms",
                client[0] if client else "-",
                method,
                self._labels.get(scope["path"], "other"),
                status,
                round((time.monotonic() - started) * 1000),
            )


class HealthzRoute:
    """``GET /healthz`` answers ``{"status": "ok"}``: the process is alive, nothing more."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != HEALTHZ_PATH:
            await self._app(scope, receive, send)
            return
        if scope["method"] != "GET":
            await send_json(send, 405, {"error": "method not allowed"})
            return
        await send_json(send, 200, {"status": "ok"})


class ReadyzRoute:
    """``GET /readyz`` answers what ``probe`` reports. It sits behind the token."""

    def __init__(self, app: ASGIApp, *, probe: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        self._app = app
        self._probe = probe

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != READYZ_PATH:
            await self._app(scope, receive, send)
            return
        if scope["method"] != "GET":
            await send_json(send, 405, {"error": "method not allowed"})
            return
        await send_json(send, 200, await self._probe())


class ResponseGuardMiddleware:
    """Bound a tool call in time (504) and its result in size (502).

    The deadline stops the WAIT, not the work: a synchronous tool already
    running on a worker thread finishes in the background, and its result is
    dropped. The concurrency cap bounds how many such threads can exist.
    """

    def __init__(self, app: ASGIApp, *, max_response_bytes: int, timeout_s: float) -> None:
        self._app = app
        self._cap = max_response_bytes
        self._timeout = timeout_s

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_post(scope):
            await self._app(scope, receive, send)
            return
        start: Message | None = None
        chunks: list[bytes] = []
        size = 0
        over = False

        async def hold(message: Message) -> None:
            nonlocal start, size, over
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body" and not over:
                body = message.get("body", b"")
                size += len(body)
                if size > self._cap:
                    over = True
                    chunks.clear()
                else:
                    chunks.append(body)

        with anyio.move_on_after(self._timeout) as deadline:
            await self._app(scope, receive, hold)
        if deadline.cancelled_caught:
            logger.warning("tool call exceeded its %ss deadline", self._timeout)
            await send_json(send, 504, {"error": "timed out"})
        elif over:
            logger.warning("tool result exceeded %d bytes", self._cap)
            await send_json(send, 502, {"error": "result too large"})
        elif start is None:
            await send_json(send, 500, {"error": "no response"})
        else:
            await send(start)
            await send({"type": "http.response.body", "body": b"".join(chunks)})


def json_depth_exceeds(body: bytes, limit: int) -> bool:
    """True when ``body`` nests arrays/objects deeper than ``limit``, ignoring strings.

    A linear scan over the structural bytes only, so it is cheap and cannot
    recurse. Malformed input is judged by the parse that follows, not here.
    """
    depth = 0
    in_string = False
    skip = -1
    for match in _STRUCTURAL.finditer(body):
        position = match.start()
        if position == skip:
            continue
        char = match.group()
        if in_string:
            if char == b"\\":
                skip = position + 1
            elif char == b'"':
                in_string = False
        elif char == b'"':
            in_string = True
        elif char in (b"[", b"{"):
            depth += 1
            if depth > limit:
                return True
        elif char in (b"]", b"}"):
            depth -= 1
    return False


class _UnreadableJsonError(ValueError):
    """A body the gate refuses to interpret (duplicate key, ``NaN``)."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _UnreadableJsonError("duplicate key")
        result[key] = value
    return result


def _no_constants(name: str) -> Any:
    raise _UnreadableJsonError("non-finite number")


def inspect_request(body: bytes) -> tuple[str, Any]:
    """``("pass", None)``, ``("malformed", None)`` or ``("refuse", <request id>)``."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return _MALFORMED, None
    if text.startswith(_BOM) or json_depth_exceeds(body, MAX_JSON_DEPTH):
        return _MALFORMED, None
    try:
        message = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants
        )
    except (ValueError, RecursionError):
        return _MALFORMED, None
    if not isinstance(message, dict):
        return _MALFORMED, None
    params = message.get("params")
    arguments = params.get("arguments") if isinstance(params, dict) else None
    if (
        message.get("method") != "tools/call"
        or not isinstance(arguments, dict)
        or "include_confidential" not in arguments
    ):
        return _PASS, None
    value = arguments["include_confidential"]
    if value is False or value is None:
        return _PASS, None
    return _REFUSE, message.get("id")


async def _read_capped(receive: Receive, cap: int) -> tuple[bytes, str]:
    """``(body, "ok")``; ``(b"", "over")`` past ``cap`` bytes; ``(b"", "gone")`` on disconnect."""
    received = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b"", "gone"
        received.extend(message.get("body", b""))
        if len(received) > cap:
            return b"", "over"
        if not message.get("more_body", False):
            return bytes(received), "ok"


class ConfidentialGateMiddleware:
    """Refuse ``include_confidential`` over HTTP; replay every other request unchanged."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._cap = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_post(scope):
            await self._app(scope, receive, send)
            return
        encodings = [
            value.strip().lower()
            for name, value in scope["headers"]
            if name.lower() == b"content-encoding"
        ]
        if any(value not in (b"", b"identity") for value in encodings):
            await send_json(send, 415, {"error": "unsupported content encoding"})
            return
        body, outcome = await _read_capped(receive, self._cap)
        if outcome == "gone":
            return
        if outcome == "over":
            await send_json(send, 413, {"error": "request too large"})
            return
        verdict, request_id = inspect_request(body)
        if verdict == _MALFORMED:
            await send_json(send, 400, {"error": "malformed request"})
            return
        if verdict == _REFUSE:
            logger.warning("refused a tools/call asking for include_confidential")
            await send_json(
                send,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": INVALID_PARAMS, "message": CONFIDENTIAL_REFUSAL},
                },
            )
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self._app(scope, replay, send)
