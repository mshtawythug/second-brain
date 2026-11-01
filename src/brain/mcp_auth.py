"""The bearer token for ``brain mcp serve``: its file, its checks, and the ASGI gate (RM-46).

The token is 256 random bits in a file only its owner can read
(``$BRAIN_HOME/mcp_token`` by default). The file is written so it is never
briefly world-readable (``O_EXCL`` on a temp sibling at 0600, then an atomic
``os.replace``) and read so that a planted symlink, a FIFO, another user's
file or an oversized file is refused rather than trusted (``O_NOFOLLOW``, then
``fstat`` on the open descriptor, then a bounded read).

:class:`BearerAuthMiddleware` compares BYTES with :func:`hmac.compare_digest`:
on ``str`` it raises ``TypeError`` for a non-ASCII header, which is how
``brain ui``'s review round 5 found a 500 behind its token. Every refusal is
one byte-identical 401, so a client learns nothing about why it failed. The
reason is logged as a fixed word; the token and the header never are.

Imports: stdlib, :mod:`brain.errors` and the settings leaf, which pulls in
:mod:`brain.config` (for ``ConfigError`` and the env parsers). No MCP SDK,
starlette, typer or :mod:`brain.mcp_server`, so ``brain mcp token`` stays cheap;
``tests/test_import_hygiene.py`` pins that.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import McpTokenError
from .settings.mcp_http import TOKEN_PATTERN, McpHttpSettings

logger = logging.getLogger("brain.mcp.http")

#: The file name under ``$BRAIN_HOME``.
TOKEN_FILE_NAME = "mcp_token"
#: A token file is at most this big; a larger one is refused unread.
MAX_TOKEN_FILE_BYTES = 4096
#: ``secrets.token_urlsafe(TOKEN_BYTES)``: 256 bits.
TOKEN_BYTES = 32

_FINGERPRINT_HEX = 8
_SCHEME = b"bearer "
_UNAUTHORIZED_BODY = json.dumps({"error": "unauthorized"}).encode("ascii")
_UNAUTHORIZED_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode("ascii")),
    (b"www-authenticate", b"Bearer"),
    (b"cache-control", b"no-store"),
]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


@dataclass(frozen=True)
class TokenInfo:
    """A resolved bearer token and where it came from.

    ``path`` is ``None`` for a token from ``BRAIN_MCP_TOKEN``. ``secret`` is
    left out of ``repr``; it reaches only :func:`hmac.compare_digest` and, on an
    explicit ``brain mcp token --show``, stdout.
    """

    path: Path | None
    secret: str = field(repr=False)
    mode_octal: str
    mode_ok: bool
    fingerprint: str


def default_token_path(brain_home: Path) -> Path:
    """``<brain_home>/mcp_token``."""
    return brain_home / TOKEN_FILE_NAME


def generate_token() -> str:
    """A new 256-bit token in the URL-safe base64 alphabet."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def fingerprint(secret: str) -> str:
    """The first 8 hex digits of the token's SHA-256: names it without revealing it."""
    return hashlib.sha256(secret.encode("ascii")).hexdigest()[:_FINGERPRINT_HEX]


def write_token(path: Path, token: str) -> None:
    """Write ``token`` to ``path`` at mode 0600, replacing any previous token atomically."""
    if not TOKEN_PATTERN.fullmatch(token):
        raise McpTokenError("refusing to write a token outside the token alphabet")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(temp, flags, 0o600)
        try:
            os.write(fd, f"{token}\n".encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise McpTokenError(
            f"cannot write the token file {path} ({exc.strerror or type(exc).__name__})"
        ) from None


def read_token(path: Path, *, expected_uid: int | None = None) -> TokenInfo:
    """Read and check the token file at ``path``; refuse anything unsafe.

    ``expected_uid`` defaults to this process's uid; tests pass another one to
    stand for a file someone else owns.
    """
    owner = os.getuid() if expected_uid is None else expected_uid
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise McpTokenError(
            f"no token at {path}; create one with: brain mcp token --rotate"
        ) from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise McpTokenError(
                f"token file {path} is a symbolic link; point --token-file at the "
                "file itself"
            ) from None
        raise McpTokenError(
            f"cannot open the token file {path} ({exc.strerror or type(exc).__name__})"
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise McpTokenError(f"token file {path} is not a regular file")
        if info.st_uid != owner:
            raise McpTokenError(f"token file {path} is owned by another user")
        raw = os.read(fd, MAX_TOKEN_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_TOKEN_FILE_BYTES:
        raise McpTokenError(f"token file {path} is too large to be a token")
    secret = _parse_token(raw, path)
    mode = stat.S_IMODE(info.st_mode)
    return TokenInfo(
        path=path,
        secret=secret,
        mode_octal=f"{mode:04o}",
        mode_ok=(mode & 0o077) == 0,
        fingerprint=fingerprint(secret),
    )


def _parse_token(raw: bytes, path: Path) -> str:
    """The token in ``raw``: one optional trailing newline, nothing else around it."""
    malformed = McpTokenError(
        f"token file {path} does not hold a token; rotate it with: brain mcp token --rotate"
    )
    body = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        raise malformed from None
    if not TOKEN_PATTERN.fullmatch(text):
        raise malformed
    return text


def resolve_token(
    settings: McpHttpSettings, *, explicit: Path | None, brain_home: Path
) -> TokenInfo | None:
    """The token ``brain mcp serve`` uses, or ``None`` when there is none.

    In order: an explicit ``--token-file`` (which must exist), then
    ``BRAIN_MCP_TOKEN``, then ``BRAIN_MCP_TOKEN_PATH``, then
    ``<brain_home>/mcp_token``. The last two may be absent.
    """
    if explicit is not None:
        return read_token(explicit)
    if settings.token is not None:
        return TokenInfo(
            path=None,
            secret=settings.token,
            mode_octal="",
            mode_ok=True,
            fingerprint=fingerprint(settings.token),
        )
    path = settings.token_path or default_token_path(brain_home)
    if not os.path.lexists(path):
        return None
    return read_token(path)


class BearerAuthMiddleware:
    """Pure ASGI: refuse every HTTP request without the bearer token, except ``exempt_paths``.

    It reads only the ``authorization`` header of ``scope``. It never touches
    ``receive``, so an unauthenticated body is never read. A websocket is closed
    (this server speaks none); lifespan passes straight through.
    """

    def __init__(self, app: ASGIApp, *, token: TokenInfo, exempt_paths: frozenset[str]) -> None:
        self._app = app
        self._expected = token.secret.encode("ascii")
        self._exempt = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http" or scope["path"] in self._exempt:
            await self._app(scope, receive, send)
            return
        reason = self._refusal(scope)
        if reason is None:
            await self._app(scope, receive, send)
            return
        logger.warning("auth rejected: %s", reason)
        await send(
            {"type": "http.response.start", "status": 401, "headers": _UNAUTHORIZED_HEADERS}
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})

    def _refusal(self, scope: Scope) -> str | None:
        """A fixed reason word when the request is refused, ``None`` when it is let in."""
        values = [value for name, value in scope["headers"] if name.lower() == b"authorization"]
        if not values:
            return "missing-header"
        if len(values) > 1:
            return "duplicate-header"
        value: bytes = values[0]
        if value[: len(_SCHEME)].lower() != _SCHEME:
            return "bad-scheme"
        presented = value[len(_SCHEME) :]
        if not hmac.compare_digest(presented, self._expected):
            return "mismatch"
        return None
