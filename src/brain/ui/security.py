"""The request guards. Everything here runs **before routing**, deliberately.

A local server that mutates data faces two different attacks, and conflating
them is the common mistake — they need two different defences:

**DNS rebinding.** An attacker's page resolves ``evil.example`` to ``127.0.0.1``
and issues requests the browser considers *same-origin*. The ``Origin`` header
is therefore attacker-controlled and legitimate-looking, so **an Origin check
does not stop this**. The defence is Host-header validation, which
``TrustedHostMiddleware`` performs and which :func:`build_middleware` installs
outermost.

**CSRF.** A page at a genuinely different origin rides the user's ambient local
access. Three layers, all in :class:`RequestGuardMiddleware`:

1. ``Origin`` must equal the bound origin exactly on every non-safe method. A
   *missing* Origin is also rejected — fail closed, because some legacy form
   posts omit it entirely.
2. ``Sec-Fetch-Site`` must be ``same-origin`` when the browser sends it.
3. ``Content-Type: application/json`` is required. This alone defeats HTML
   ``<form>`` CSRF (a form can only send urlencoded / multipart / text-plain)
   and forces a CORS preflight on any cross-origin ``fetch``, which layer 1
   then rejects.

**Why these are middleware and not decorators.** Read-only mode in particular
short-circuits every non-safe method here, before the router ever resolves a
handler. A per-handler check is one forgotten decorator away from a writable
endpoint on a server the user explicitly asked to be read-only; a middleware
cannot be forgotten, because a new route is covered the moment it is added.

Both classes are **pure ASGI** middleware rather than ``BaseHTTPMiddleware``
subclasses: BaseHTTPMiddleware wraps every request in an anyio task group and
buffers the response, which is unnecessary overhead here and — more importantly
— makes "runs before routing" harder to reason about.
"""
from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any

from .context import LOOPBACK_HOSTS
from .errors import UiError, UiForbidden, UiUnsupportedMedia

if TYPE_CHECKING:  # pragma: no cover — typing only
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from .context import UiContext

#: Methods that may not mutate. Everything else passes the full guard.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

#: The header the browser shell sends its shared secret in. A header, never a
#: query parameter: query strings land in access logs and in ``Referer``.
TOKEN_HEADER = "x-brain-ui-token"

#: ``default-src 'none'`` is only sustainable because the shell has no inline
#: ``<script>`` and no inline ``<style>``; that is what removes the need for a
#: nonce. ``img-src data:`` exists solely for the SVG grain overlay.
#: ``form-action 'none'`` and ``frame-ancestors 'none'`` add CSRF and
#: clickjacking hardening on top.
CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

_STATIC_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CSP.encode("latin-1")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
)


def is_loopback(host: str) -> bool:
    """True when ``host`` is a loopback address the server may bind unguarded.

    ``0.0.0.0`` is emphatically **not** loopback — it is every interface, and
    treating it as safe is the exact mistake that would expose an
    unauthenticated corpus to the local network.
    """
    return host.strip().lower() in LOOPBACK_HOSTS


async def _send_error(send: Send, exc: UiError) -> None:
    """Emit a JSON error envelope directly, bypassing the router.

    The security headers are attached here too. A rejected request is exactly
    the one an attacker sees, so it must not be the one response in the app
    that ships without a CSP.
    """
    body = json.dumps(exc.to_payload()).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("latin-1")),
        (b"cache-control", b"no-store"),
        *_STATIC_HEADERS,
    ]
    await send(
        {"type": "http.response.start", "status": exc.status, "headers": headers}
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Attach the CSP and hardening headers to **every** response.

    Implemented by wrapping ``send`` rather than by post-processing a response
    object, so it covers responses this middleware never constructed — static
    files, router 404s, and the 405s Starlette's own method dispatch emits.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_api = str(scope.get("path", "")).startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value)
                    for name, value in _STATIC_HEADERS
                    if name not in existing
                )
                if is_api and b"cache-control" not in existing:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestGuardMiddleware:
    """Token, read-only, Origin, ``Sec-Fetch-Site`` and Content-Type gates.

    Ordering inside :meth:`_check` is deliberate and is asserted by tests:
    read-only is refused before the request body's shape is considered, so a
    read-only server's answer never depends on how well-formed the write was.
    """

    def __init__(self, app: ASGIApp, *, context: UiContext) -> None:
        self.app = app
        self.context = context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            self._check(scope)
        except UiError as exc:
            await _send_error(send, exc)
            return
        await self.app(scope, receive, send)

    def _header(self, scope: Scope, name: str) -> str:
        target = name.lower().encode("latin-1")
        for key, value in scope.get("headers", []):
            if key.lower() == target:
                return str(value.decode("latin-1"))
        return ""

    def _check(self, scope: Scope) -> None:
        ctx = self.context
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))

        # The token guards the API only. The shell itself must load before any
        # JavaScript exists to send a header, and it carries no corpus data —
        # it is a constant HTML file. Every byte of the user's brain sits behind
        # /api/, which is what the token actually protects.
        if ctx.token and path.startswith("/api/"):
            supplied = self._header(scope, TOKEN_HEADER)
            if not secrets.compare_digest(supplied, ctx.token):
                raise UiForbidden(
                    "missing or invalid access token", code="invalid_token"
                )

        if method in SAFE_METHODS:
            return

        if ctx.read_only:
            raise UiForbidden(
                "this server was started with --read-only; every write is blocked",
                code="read_only",
            )

        origin = self._header(scope, "origin")
        if not origin:
            raise UiForbidden(
                "mutations require an Origin header", code="origin_missing"
            )
        if origin != ctx.allowed_origin:
            raise UiForbidden(
                "cross-origin mutations are refused", code="origin_mismatch"
            )

        fetch_site = self._header(scope, "sec-fetch-site")
        if fetch_site and fetch_site != "same-origin":
            raise UiForbidden("cross-site mutations are refused", code="cross_site")

        media_type = self._header(scope, "content-type").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise UiUnsupportedMedia(
                "mutations require Content-Type: application/json"
            )


def build_middleware(context: UiContext) -> list[Any]:
    """The middleware stack, outermost first.

    ``TrustedHostMiddleware`` is outermost on purpose: a rebinding attempt
    should be rejected on the Host header before any other code — including the
    Origin guard, which structurally cannot see that attack — has looked at the
    request.
    """
    from starlette.middleware import Middleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    return [
        Middleware(TrustedHostMiddleware, allowed_hosts=sorted(context.allowed_hosts)),
        Middleware(SecurityHeadersMiddleware),
        Middleware(RequestGuardMiddleware, context=context),
    ]
