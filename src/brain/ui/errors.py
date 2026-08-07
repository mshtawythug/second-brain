"""The HTTP error vocabulary for `brain ui`.

Every failure the UI reports is one of these, and each carries two things a
plain exception does not: an HTTP ``status`` and a stable machine ``code`` the
front end switches on. Inheriting :class:`~brain.errors.BrainError` keeps them
inside the project's one exception root, per the style rules.

**The message contract is a security boundary, not a formatting preference.**
``message`` must never contain SQL, a connection string, a filesystem path
outside the vault, or document content — it is rendered verbatim in a browser.
``brain.mcp_server._wrap_db_error`` already established this convention by
exposing only ``type(exc).__name__``; :func:`wrap_db_error` below is the same
idea for this surface.
"""
from __future__ import annotations

from ..errors import BrainError


class UiError(BrainError):
    """Base for every error the UI renders as a JSON envelope.

    Subclasses set :attr:`status`; instances set :attr:`code` so one class can
    cover several distinguishable failures (``invalid_limit`` and
    ``invalid_date`` are both :class:`UiBadRequest`) without a class explosion.
    """

    status: int = 500
    default_code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code

    def to_payload(self) -> dict[str, dict[str, str]]:
        """The single error envelope shape every route returns."""
        return {"error": {"code": self.code, "message": self.message}}


class UiBadRequest(UiError):
    """400 — the request itself is malformed or out of range."""

    status = 400
    default_code = "bad_request"


class UiForbidden(UiError):
    """403 — the request is well-formed but this server refuses it.

    Raised by the security middleware (cross-origin mutation, a bad token) and
    by read-only mode.
    """

    status = 403
    default_code = "forbidden"


class UiNotFound(UiError):
    """404 — no such document, or no such static asset."""

    status = 404
    default_code = "not_found"


class UiConflict(UiError):
    """409 — the write is refused because the server's state moved.

    Two very different causes share this status deliberately, because both mean
    "retry after re-reading": a stale ``body_hash`` (someone else wrote the file
    first) and a failed ``expected_title`` confirmation on delete.
    """

    status = 409
    default_code = "conflict"


class UiUnsupportedMedia(UiError):
    """415 — a mutation arrived without ``Content-Type: application/json``.

    This is load-bearing CSRF defence, not pedantry: an HTML ``<form>`` can only
    send ``urlencoded`` / ``multipart`` / ``text/plain``, so requiring JSON
    means a cross-origin form post cannot reach a write handler at all, and a
    cross-origin ``fetch`` is forced into a CORS preflight that the Origin guard
    then rejects.
    """

    status = 415
    default_code = "unsupported_media_type"


class UiUnavailable(UiError):
    """503 — a dependency the request needed is down (Postgres, Ollama)."""

    status = 503
    default_code = "unavailable"


def wrap_db_error(exc: Exception) -> UiUnavailable:
    """Turn a psycopg failure into a 503 that leaks nothing.

    Only the exception *class name* crosses the boundary. psycopg's
    ``str(exc)`` routinely embeds the failing SQL, parameter values, and the
    host/port/user of the connection — all of which would otherwise end up in a
    browser tab, and in any proxy log between here and it.
    """
    return UiUnavailable(
        f"database unavailable ({type(exc).__name__})",
        code="database_unavailable",
    )
