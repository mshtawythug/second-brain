"""Startup: preflight, port resolution, context construction, and uvicorn.

The **only** module that imports uvicorn, so importing anything else in this
package — or importing ``brain.cli`` — never pays for it.

Failures happen here, before the socket binds, and with the same remediation
wording ``brain doctor`` uses. A dead Postgres discovered by the first request
is a much worse experience than one discovered at startup.
"""
from __future__ import annotations

import errno
import logging
import socket
import webbrowser
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from typing import Any

import psycopg

from ..config import Config
from ..db import connect
from ..errors import BrainError
from .context import UiContext, host_allowlist
from .security import is_loopback
from .telemetry import ui_source_supported

logger = logging.getLogger(__name__)

#: 8765 avoids every port this repository already uses: production Postgres
#: 55432, test Postgres 5434, demo Postgres 55433, wiki/Caddy 8080, Ollama
#: 11434.
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"

TELEMETRY_DISABLED_NOTICE = (
    "search + open logging is disabled — the telemetry CHECK constraint does "
    "not accept 'ui'. Run `brain init` to apply migration 024. The UI works "
    "normally otherwise."
)


def _port_is_free(host: str, port: int) -> bool:
    """Probe a port with a real bind. Portable; no ``lsof``."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host if host != "::1" else "127.0.0.1", port))
        except OSError:
            return False
    return True


def resolve_port(
    start: int, *, host: str = DEFAULT_HOST, attempts: int = 20, auto: bool = True
) -> int:
    """Return a free port at or after ``start``.

    A TOCTOU window remains between this probe and uvicorn's bind, so
    :func:`serve` also catches ``EADDRINUSE`` and prints the same remediation
    rather than a traceback.
    """
    if _port_is_free(host, start):
        return start
    if not auto:
        raise BrainError(
            f"port {start} is already in use\n"
            f"  Stop the process using port {start}, pass a different --port,\n"
            f"  or drop --no-auto-port to auto-select the next free one."
        )
    for candidate in range(start + 1, start + attempts):
        if _port_is_free(host, candidate):
            return candidate
    raise BrainError(
        f"no free port found in {start}..{start + attempts - 1}; "
        "pass --port with a free one"
    )


def preflight(cfg: Config) -> tuple[bool, tuple[str, ...]]:
    """Probe the database once. Return ``(logging_enabled, notices)``.

    Raises :class:`BrainError` when Postgres is unreachable, so a dead database
    fails **before** uvicorn binds a port the user would then have to go and
    kill.
    """
    notices: list[str] = []
    try:
        with connect(cfg.database_url) as conn:
            conn.execute("SELECT 1")
            logging_enabled = ui_source_supported(conn)
    except psycopg.Error as exc:
        raise BrainError(
            f"cannot reach Postgres ({type(exc).__name__}).\n"
            "  Is it running?  brain doctor"
        ) from exc

    if not logging_enabled:
        notices.append(TELEMETRY_DISABLED_NOTICE)
    return logging_enabled, tuple(notices)


def build_context(
    cfg: Config,
    *,
    host: str,
    port: int,
    read_only: bool,
    token: str,
    include_confidential: bool,
    embedder: Any,
    search_fn: Callable[..., Any] | None = None,
    graph_syncer: Any | None = None,
) -> UiContext:
    """Assemble the :class:`UiContext` the app will run on."""
    from ..search import hybrid_search

    logging_enabled, notices = preflight(cfg)
    loopback = is_loopback(host)

    def conn_factory() -> Any:
        return connect(cfg.database_url)

    return UiContext(
        cfg=cfg,
        conn_factory=conn_factory,
        embedder=embedder,
        search_fn=search_fn or hybrid_search,
        read_only=read_only,
        token=token,
        allowed_origin=f"http://{host}:{port}",
        allowed_hosts=host_allowlist(host),
        logging_enabled=logging_enabled,
        graph_syncer=graph_syncer,
        notices=notices,
        # Confidential bodies leave this process only when the server is
        # loopback-bound (CLI-equivalent trust) or the operator said so.
        serve_confidential_bodies=loopback or include_confidential,
    )


def serve(
    context: UiContext,
    *,
    host: str,
    port: int,
    open_browser: bool,
    opener: Callable[[str], Any] | None = None,
) -> None:
    """Run the app under uvicorn until Ctrl-C.

    Foreground-only by design: no PID file, no launchd plist. Daemonizing a
    *write-capable* HTTP server that could outlive the user's attention is a bad
    idea — the read-only wiki is the surface that gets a daemon.
    """
    import uvicorn

    from .app import create_app

    url = f"http://{host}:{port}/"
    app = create_app(context)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)

    if open_browser:
        # Opened after the bind rather than before it, so the browser cannot
        # race the socket and land on a connection-refused page.
        original_startup = server.startup

        async def startup_then_open(*args: Any, **kwargs: Any) -> None:
            await original_startup(*args, **kwargs)
            (opener or webbrowser.open)(url)

        server.startup = startup_then_open  # type: ignore[method-assign]

    try:
        server.run()
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise BrainError(
                f"port {port} is already in use\n"
                f"  Stop the process using port {port}, or pass a different --port."
            ) from exc
        raise


def with_notice(context: UiContext, notice: str) -> UiContext:
    """Return ``context`` with one more startup notice attached."""
    return replace(context, notices=(*context.notices, notice))
