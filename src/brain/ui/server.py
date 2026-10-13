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
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import psycopg

from ..config import Config
from ..db import connect
from ..errors import BrainError
from .context import UiContext, bind_address, bind_origin, host_allowlist
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


SocketFactory = Callable[[int, int], socket.socket]


def _probe_socket(
    address: str, socket_factory: SocketFactory = socket.socket
) -> socket.socket:
    """A socket configured the way uvicorn's real bind will be.

    Same address family as the bind, and for IPv6 the same ``IPV6_V6ONLY=1``
    asyncio sets on every IPv6 server socket. Without it the probe was
    dual-stack: on Linux an IPv4 socket holding the port made it report "busy"
    for a ``::`` server that would have bound fine (phase-7 review round 3).
    """
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    # Creation may raise OSError; the caller reports that as "cannot open".
    sock = socket_factory(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    except OSError as exc:
        # The socket EXISTS here, so close it, and say what actually failed:
        # "cannot open" would send the user looking for a missing address
        # family that is present (review round 6).
        sock.close()
        name = "IPv6" if family == socket.AF_INET6 else "IPv4"
        raise BrainError(
            f"cannot configure an {name} probe socket for --host {address} "
            f"({exc.strerror or exc})"
        ) from exc
    return sock


def _port_is_free(
    host: str, port: int, *, socket_factory: SocketFactory = socket.socket
) -> bool:
    """Probe a port with a real bind. Portable; no ``lsof``.

    The probe uses the address family the server will bind. It used to be
    IPv4-only (with ``::1`` mapped to ``127.0.0.1``), so binding ``::`` raised
    on every port, every port looked busy, and ``brain ui --host ::`` died with
    "no free port found" before it could print a thing (phase-7 audit L-8).
    """
    address = bind_address(host)
    try:
        probe = _probe_socket(address, socket_factory)
    except OSError as exc:
        # Creating the socket, not binding it: the kernel has no such address
        # family. It was outside the try below and escaped as a raw traceback
        # (review round 5). A bind failure means "busy"; this means "never".
        family = "IPv6" if ":" in address else "IPv4"
        raise BrainError(
            f"cannot open an {family} socket for --host {host} "
            f"({exc.strerror or exc})\n"
            f"  This machine may not support {family}; pass a different --host."
        ) from exc
    with closing(probe) as sock:
        try:
            sock.bind((address, port))
        except socket.gaierror as exc:
            raise BrainError(
                f"--host {host} does not resolve to an address on this machine "
                f"({exc.strerror or exc})."
            ) from exc
        except OSError as exc:
            # ONLY "in use" means busy (final round). This was a catch-all, so
            # an address the machine does not own, or a refused privileged
            # port, was reported as "port already in use" / "no free port".
            if exc.errno == errno.EADDRINUSE:
                return False
            raise _bind_failure(host, port, exc) from exc
    return True


def _bind_failure(host: str, port: int, exc: OSError) -> BrainError:
    """The error for a probe bind that failed for any reason but "in use".

    EACCES is not treated as busy: the auto-port bump moves by one, which stays
    inside the privileged range below 1024 and would hide the real cause.
    """
    if exc.errno == errno.EADDRNOTAVAIL:
        return BrainError(f"--host {host} is not an address this machine owns.")
    if exc.errno == errno.EACCES:
        return BrainError(
            f"permission denied binding port {port} — ports below 1024 need "
            "privileges; pass --port 1024 or higher."
        )
    return BrainError(
        f"--host {host} cannot be bound on this machine ({exc.strerror or exc})."
    )


def resolve_port(
    start: int,
    *,
    host: str = DEFAULT_HOST,
    attempts: int = 20,
    auto: bool = True,
    socket_factory: SocketFactory = socket.socket,
) -> int:
    """Return a free port at or after ``start``.

    A TOCTOU window remains between this probe and uvicorn's bind, so
    :func:`serve` also catches ``EADDRINUSE`` and prints the same remediation
    rather than a traceback.
    """
    if _port_is_free(host, start, socket_factory=socket_factory):
        return start
    if not auto:
        raise BrainError(
            f"port {start} is already in use\n"
            f"  Stop the process using port {start}, pass a different --port,\n"
            f"  or drop --no-auto-port to auto-select the next free one."
        )
    for candidate in range(start + 1, start + attempts):
        if _port_is_free(host, candidate, socket_factory=socket_factory):
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

    @contextmanager
    def conn_factory() -> Iterator[Any]:
        """Per-request connection, in AUTOCOMMIT — the contract, now implemented.

        ``UiContext`` documents this factory as "yielding an autocommit-capable
        psycopg connection" and ``gaps.record_search_query`` states "Callers run
        with ``autocommit=True``". Both were true as prose and false as code:
        this returned ``connect(...)`` unmodified, so psycopg3's default
        ``autocommit=False`` left an implicit transaction open, telemetry's
        ``conn.transaction()`` degraded to a SAVEPOINT inside it, and
        ``conn.close()`` rolled the lot back.

        Measured before the fix: **zero rows with source='ui' after ~215 real
        UI searches**, while ``/api/status`` reported ``logging_enabled: true``.
        Failing closed would have been bad; failing closed while health asserts
        success is what made it invisible for the whole of phase 0.

        ``connect_raw`` deliberately rolls back its pgvector probe so this flag
        can be set here (``db.py:43-44``); ``cli_recall.py:114`` is the same
        line, done right, and was the template.
        """
        with connect(cfg.database_url) as conn:
            conn.autocommit = True
            yield conn

    return UiContext(
        cfg=cfg,
        conn_factory=conn_factory,
        embedder=embedder,
        search_fn=search_fn or hybrid_search,
        read_only=read_only,
        token=token,
        allowed_origin=bind_origin(host, port),
        allowed_hosts=host_allowlist(host),
        logging_enabled=logging_enabled,
        graph_syncer=graph_syncer,
        notices=notices,
        # Confidential bodies leave this process only when the server is
        # loopback-bound (CLI-equivalent trust) or the operator said so.
        serve_confidential_bodies=loopback or include_confidential,
        # Confidential TITLES on the unprompted listing surfaces are a
        # separate, config-only decision — deliberately NOT ``or``-ed with
        # ``loopback`` or ``include_confidential``. Both of those say "this
        # session may READ confidential material it opens"; neither says "paint
        # confidential titles in a rail nobody asked for". Default False.
        serve_confidential_titles=cfg.ui_serve_confidential_titles,
    )


def warm_embedder(embedder: Any) -> bool:
    """Pay the embedder's cold-start cost at boot, not on the first search.

    Measured cold vs warm: **5,358 ms vs ~250 ms** — a 12-21x cliff. Ollama's
    ``keep_alive=-1`` holds the model resident, so the residual exposure is
    exactly *process restart*, which is what a freshly-started ``brain ui``
    is. One throwaway embed converts a user-visible 5.4 s stall on the first
    search into startup time nobody is watching.

    **Never fatal, never noisy.** An FTS-only install (``BRAIN_EMBEDDER=none``)
    has no vectors to warm and must boot unchanged, so the duck-typed
    ``produces_embeddings`` flag short-circuits before any network call — the
    same ``getattr(..., True)`` shape ``search``/``ingest``/``cli_search`` use.
    A real backend with Ollama *down* must also boot: the failure is logged at
    debug and swallowed, because the user's next action is a search that will
    surface the problem properly with its own remediation text. A server that
    refused to start because a warm-up failed would be a worse product than one
    whose first search is slow.

    Returns True when a vector was actually fetched — for tests and for the
    caller's log line, not for control flow.
    """
    if embedder is None or not getattr(embedder, "produces_embeddings", True):
        return False
    try:
        embedder.embed(["warm"], input_type="query")
    except (BrainError, OSError) as exc:
        # BrainError covers EmbedError/OllamaEmbedError; OSError covers a socket
        # that dies below the HTTP client. Anything else is a real bug and
        # should not be hidden by a performance optimization.
        logger.debug("embedder warm-up skipped: %s", type(exc).__name__)
        return False
    return True


def browser_url(host: str, port: int, token: str) -> str:
    """The URL to open: the bind's origin, plus the token in the fragment.

    The fragment is how the client receives the token (``static/js/api.js``):
    it is never sent to the server, logged, or leaked in a Referer. Without it a
    tokened server opens a page whose every API call is refused. One function
    for the opened tab and the printed panel, so the two cannot disagree, and
    built on :func:`bind_origin`, so neither can disagree with the write guard.

    The token is percent-encoded because ``api.js`` ``decodeURIComponent``-s
    it: a raw ``%`` threw ``URIError`` before the page could boot, and a raw
    ``#`` truncated the secret (phase-7 review L-3).
    """
    url = f"{bind_origin(host, port)}/"
    return f"{url}#t={quote(token, safe='')}" if token else url


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

    # Before the bind, so the cost lands in startup rather than in the first
    # request — and before the browser opens, so a warm model is ready by the
    # time anyone can type.
    warm_embedder(context.embedder)

    url = browser_url(host, port, context.token)
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
