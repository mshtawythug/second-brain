"""`create_app` — the route table, middleware stack, and static mount.

Route methods are declared **explicitly** on every entry. That is what makes the
"a GET can never mutate" guarantee structural: Starlette answers 405 for any
method a route did not list, so the property holds for routes added later
without anyone having to remember it.
"""
from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..errors import VaultPathEscape
from . import routes_meta, routes_notes, routes_search, routes_tree
from .context import UiContext
from .errors import UiError, UiNotFound
from .security import build_middleware

logger = logging.getLogger(__name__)


def static_dir() -> Path:
    """Locate the packaged static assets.

    Resolved through ``importlib.resources`` rather than ``__file__`` so the
    assets are found identically in a source checkout and inside an installed
    wheel — mirroring ``db.migrations_dir()`` and ``brain.demo``. This
    repository has already shipped one broken wheel for exactly this class of
    mistake (commit ``ed8195f``, migrations missing from the wheel); the failure
    mode here would be a blank page with no error, which is why it also gets a
    dedicated test.
    """
    return Path(str(files("brain.ui") / "static"))


async def _ui_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a :class:`UiError` as the one JSON envelope every route uses."""
    assert isinstance(exc, UiError)
    return JSONResponse(exc.to_payload(), status_code=exc.status)


async def _http_error_handler(request: Request, exc: Exception) -> Response:
    """Render Starlette's own 404/405 in the same envelope shape.

    Without this, a 405 from the router would come back as Starlette's HTML
    default — a different shape from every other error the client sees, and one
    the front end would have to special-case.
    """
    assert isinstance(exc, HTTPException)
    code = {404: "not_found", 405: "method_not_allowed"}.get(
        exc.status_code, "http_error"
    )
    return JSONResponse(
        {"error": {"code": code, "message": exc.detail}}, status_code=exc.status_code
    )


async def _traversal_handler(request: Request, exc: Exception) -> JSONResponse:
    """A path-traversal refusal is a 400, wherever in the stack it was raised.

    Registered centrally rather than caught at each call site on purpose.
    ``assert_within_vault`` is invoked from several places in the service layer —
    on ``folder`` at create, on ``new_folder`` at move, and defensively on the
    stored ``vault_path`` when opening a file — and one un-wrapped call site
    would turn a *blocked attack* into a 500 with a traceback. Handling the
    exception type once means a new call site is covered the moment it is added.

    The message deliberately does not echo the resolved absolute path, which
    would disclose the vault's location on disk.
    """
    return JSONResponse(
        {
            "error": {
                "code": "folder_escapes_vault",
                "message": "that path resolves outside the vault",
            }
        },
        status_code=400,
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the detail, return none of it.

    The traceback goes to the server log where the user can read it; the
    response carries only a class name, so an unexpected failure cannot become
    an information leak in a browser tab.
    """
    logger.exception("brain ui: unhandled error serving %s", request.url.path)
    return JSONResponse(
        {
            "error": {
                "code": "internal_error",
                "message": f"internal error ({type(exc).__name__})",
            }
        },
        status_code=500,
    )


async def index(request: Request) -> FileResponse:
    """Serve the single HTML shell.

    The shell is a constant file with no interpolation — there is exactly one
    template in this app and it is not a template. That is what removes the
    server-side-injection surface a Jinja page would have, and it is why the CSP
    can forbid inline script and style outright.
    """
    shell = static_dir() / "index.html"
    if not shell.is_file():  # pragma: no cover — packaging failure
        raise UiNotFound(
            "the UI assets are missing from this installation",
            code="assets_missing",
        )
    return FileResponse(shell)


def create_app(context: UiContext) -> Starlette:
    """Build the application for ``context``."""
    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/health", routes_meta.health, methods=["GET"]),
        Route("/api/status", routes_meta.status, methods=["GET"]),
        Route("/api/facets", routes_meta.facets, methods=["GET"]),
        Route("/api/tree", routes_tree.tree, methods=["GET"]),
        Route("/api/search", routes_search.search, methods=["GET"]),
        Route("/api/notes", routes_notes.create_note, methods=["POST"]),
        Route(
            "/api/notes/{id_prefix}/draft", routes_notes.set_draft, methods=["POST"]
        ),
        Route("/api/notes/{id_prefix}/move", routes_notes.move_note, methods=["POST"]),
        Route(
            "/api/notes/{id_prefix}",
            routes_notes.get_note,
            methods=["GET"],
        ),
        Route(
            "/api/notes/{id_prefix}",
            routes_notes.update_note,
            methods=["PUT"],
            name="update_note",
        ),
        Route(
            "/api/notes/{id_prefix}",
            routes_notes.delete_note,
            methods=["DELETE"],
            name="delete_note",
        ),
        Mount(
            "/static",
            app=StaticFiles(directory=static_dir(), check_dir=False),
            name="static",
        ),
    ]

    app = Starlette(
        routes=routes,
        middleware=build_middleware(context),
        exception_handlers={
            UiError: _ui_error_handler,
            VaultPathEscape: _traversal_handler,
            HTTPException: _http_error_handler,
            Exception: _unhandled_error_handler,
        },
    )
    app.state.ui = context
    return app
