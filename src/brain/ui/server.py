"""Startup: preflight, port resolution, context construction, and uvicorn.

The **only** module that imports uvicorn, so importing anything else in this
package — or importing ``brain.cli`` — never pays for it.

Failures happen here, before the socket binds, and with the same remediation
wording ``brain doctor`` uses. A dead Postgres discovered by the first request
is a much worse experience than one discovered at startup.
"""
from __future__ import annotations

import errno
import ipaddress
import logging
import socket
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import psycopg

from ..config import Config
from ..db import connect
from ..errors import BrainError
from .context import (
    Resolved,
    UiContext,
    bind_origin,
    host_allowlist,
    literal_addresses,
)
from .ingest_limits import MAX_QUEUED_JOBS, RETAIN_FOR, RETAIN_JOBS
from .ingest_service import IngestRuntime
from .jobs import JobRegistry
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

#: The listen backlog — uvicorn's own default.
_BACKLOG = 2048

#: The highest TCP port. ``bind`` raises ``OverflowError`` past it, which is not
#: an ``OSError`` (review round 12).
_MAX_PORT = 65535


def _listen_socket(
    address: str, socket_factory: SocketFactory = socket.socket
) -> socket.socket:
    """An unbound server socket for ``address``.

    The address's own family; for IPv6 ``IPV6_V6ONLY=1``, as asyncio sets it.
    This was the PROBE until round 11; it is now the socket the server runs
    on. Without V6ONLY the old probe was dual-stack: on Linux an IPv4 socket
    holding the port made it report "busy" for a ``::`` server that would have
    bound fine (phase-7 review round 3).

    ``SO_REUSEADDR`` is set for a SPECIFIC address only, never the wildcard
    (review round 13). On macOS it lets ``0.0.0.0:P`` bind while another
    process already listens on ``127.0.0.1:P``; the loopback URL we print and
    open, token in the fragment, then reaches THAT process. Without it the
    bind fails with EADDRINUSE and auto-port moves on. Linux refuses that
    overlap even with the option, so there it changes one thing only: a
    wildcard restart while old connections sit in TIME_WAIT steps to the next
    port (or, with ``--no-auto-port``, reports the port in use). The other
    order — a process binding loopback AFTER our wildcard — is closed by
    :func:`_loopback_companion` (round 14).
    """
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    # Creation may raise OSError; the caller reports that as "cannot open".
    sock = socket_factory(family, socket.SOCK_STREAM)
    try:
        if not ipaddress.ip_address(address).is_unspecified:
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
            f"cannot configure an {name} socket for --host {address} "
            f"({exc.strerror or exc})"
        ) from exc
    return sock


Resolver = Callable[..., Any]


def resolve_bind_addresses(host: str, resolver: Resolver | None = None) -> Resolved:
    """Every address ``host`` binds — resolved ONCE, then classified and bound.

    A literal needs no lookup. A hostname is resolved with ``getaddrinfo`` and
    ``AI_PASSIVE``, as asyncio's ``create_server`` would (review round 10):
    classifying the NAME let a name that resolves to 0.0.0.0 bind every
    interface with no notice, and trusted ``localhost`` as loopback wherever it
    pointed. This answer is the only one used (round 11): :func:`claim_port`
    binds exactly these addresses and uvicorn is handed the sockets, so a
    second lookup that answers differently cannot move the bind away from what
    was classified. ``resolver`` is injectable so tests never touch real DNS.
    """
    literal = literal_addresses(host)
    if literal:
        return literal
    lookup = resolver or socket.getaddrinfo
    try:
        infos = lookup(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_PASSIVE)
    except socket.gaierror as exc:
        raise BrainError(
            f"--host {host} does not resolve to an address on this machine "
            f"({exc.strerror or exc})."
        ) from exc
    except UnicodeError as exc:
        raise BrainError(f"--host {host} is not a valid hostname.") from exc
    addresses = tuple(dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos))
    if not addresses:
        raise BrainError(f"--host {host} does not resolve to an address on this machine.")
    return addresses


class _PortBusy(Exception):
    """Internal: the port is in use on one of the addresses (EADDRINUSE)."""


def _open_listener(
    host: str, address: str, port: int, socket_factory: SocketFactory
) -> socket.socket:
    """One listening socket on exactly ``address``:``port``, or an error.

    This IS the bind, not a probe of it (review round 11): the socket stays
    open and is handed to uvicorn, so nothing resolves ``host`` again between
    classification and bind, and no other process can take the port between a
    probe and the real bind either.
    """
    try:
        sock = _listen_socket(address, socket_factory)
    except OSError as exc:
        # Creating the socket, not binding it: the kernel has no such address
        # family (review round 5).
        family = "IPv6" if ":" in address else "IPv4"
        raise BrainError(
            f"cannot open an {family} socket for --host {host} "
            f"({exc.strerror or exc})\n"
            f"  This machine may not support {family}; pass a different --host."
        ) from exc
    try:
        sock.bind((address, port))
        sock.listen(_BACKLOG)
    except OSError as exc:
        sock.close()
        # ONLY "in use" means busy (final round). A catch-all here reported an
        # address the machine does not own, or a refused privileged port, as
        # "port already in use" / "no free port".
        if exc.errno == errno.EADDRINUSE:
            raise _PortBusy from exc
        raise _bind_failure(host, port, exc) from exc
    except BaseException:
        # Not an OSError, but the socket exists all the same: ``bind`` raises
        # OverflowError for a port outside 0–65535 (review round 12), and an
        # interrupt can land here too. Closed, then re-raised unchanged.
        sock.close()
        raise
    return sock


def bind_listeners(
    host: str,
    port: int,
    *,
    addresses: Resolved,
    socket_factory: SocketFactory = socket.socket,
) -> list[socket.socket]:
    """Listening sockets on EXACTLY ``addresses`` — what was classified.

    One per resolved address, each in its own family (round 10: a name
    resolving only to IPv6 was probed as IPv4). If any one fails, every socket
    already opened is closed before the error propagates.

    Two refinements, both review round 14. A specific address under a wildcard
    of its own family is already covered by it, so it is not bound again —
    binding it first would make the wildcard's bind, which carries no
    ``SO_REUSEADDR``, fail on every port. And each wildcard gets a loopback
    companion: see :func:`_loopback_companion`.
    """
    wildcards = {a.version for a in addresses if a.is_unspecified}
    sockets: list[socket.socket] = []
    try:
        for address in addresses:
            if address.version in wildcards and not address.is_unspecified:
                continue
            sockets.append(_open_listener(host, str(address), port, socket_factory))
            if address.is_unspecified:
                companion = _loopback_companion(host, address, port, socket_factory)
                if companion is not None:
                    sockets.append(companion)
    except BaseException:
        for sock in sockets:
            sock.close()
        raise
    return sockets


def _loopback_companion(
    host: str,
    wildcard: ipaddress.IPv4Address | ipaddress.IPv6Address,
    port: int,
    socket_factory: SocketFactory,
) -> socket.socket | None:
    """Also hold the wildcard's loopback address, or ``None`` where it cannot be.

    Review round 14, the other bind order of round 13's leak: once our
    wildcard listens on ``0.0.0.0:P``, macOS lets another process bind
    ``127.0.0.1:P`` with ``SO_REUSEADDR`` and receive every loopback
    connection — the tokened URL we open included (measured, both families).
    Holding ``127.0.0.1:P`` (``::1:P``) ourselves makes that bind fail, even
    with ``SO_REUSEPORT``. It exposes nothing new: loopback is inside the
    wildcard, and it is served like any other socket.

    Two failures are tolerated, and only two. EADDRINUSE: Linux refuses this
    bind against our own wildcard and needs no companion, since it refuses the
    foreign bind too. EADDRNOTAVAIL: a host with IPv6 disabled on ``lo``
    (``net.ipv6.conf.lo.disable_ipv6=1``) binds ``::`` but has no ``::1`` —
    and then no other process can hold ``::1:P`` either (review round 15:
    refusing it made ``brain ui --host ::`` fail there with "not an address
    this machine owns"). Any other failure propagates. One gap remains on
    macOS: a process that binds loopback in the instant between our two binds
    turns this companion into EADDRINUSE as well.
    """
    loopback = "::1" if wildcard.version == 6 else "127.0.0.1"
    try:
        return _open_listener(host, loopback, port, socket_factory)
    except _PortBusy:
        return None
    except BrainError as exc:
        cause = exc.__cause__
        if isinstance(cause, OSError) and cause.errno == errno.EADDRNOTAVAIL:
            return None
        raise


def _bind_failure(host: str, port: int, exc: OSError) -> BrainError:
    """The error for a bind that failed for any reason but "in use".

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


def claim_port(
    start: int,
    *,
    host: str = DEFAULT_HOST,
    addresses: Resolved | None = None,
    attempts: int = 20,
    auto: bool = True,
    socket_factory: SocketFactory = socket.socket,
) -> tuple[int, list[socket.socket]]:
    """Bind ``addresses`` at ``start`` or the next free port; return it and the sockets.

    The probe is the bind (review round 11). This used to probe, close, and let
    uvicorn bind again from ``host`` — a TOCTOU twice over: the port could be
    taken in between, and a hostname was RESOLVED again by asyncio, so a name
    classified as loopback could bind somewhere else. The returned sockets
    are passed to :func:`serve` and bound to exactly what was classified.
    ``addresses`` defaults to ``host`` itself when it is an IP literal.
    """
    if not 1 <= start <= _MAX_PORT:
        # Port 0 asks the kernel for an ephemeral port, which it would pick
        # PER ADDRESS — a different one on each socket (review round 12).
        raise BrainError(f"--port {start} is not a usable port; pass 1 to {_MAX_PORT}.")
    targets = addresses if addresses is not None else literal_addresses(host)
    if not targets:
        raise BrainError(f"--host {host} has no address to bind; resolve it first.")
    # Auto-port stops at the last port rather than walking into OverflowError.
    stop = min(start + attempts, _MAX_PORT + 1) if auto else start + 1
    for candidate in range(start, stop):
        try:
            return candidate, bind_listeners(
                host, candidate, addresses=targets, socket_factory=socket_factory
            )
        except _PortBusy:
            continue
    if not auto:
        raise BrainError(
            f"port {start} is already in use\n"
            f"  Stop the process using port {start}, pass a different --port,\n"
            f"  or drop --no-auto-port to auto-select the next free one."
        )
    raise BrainError(
        f"no free port found in {start}..{stop - 1}; "
        "pass --port with a free one"
    )


def preflight(cfg: Config) -> tuple[bool, tuple[str, ...]]:
    """Probe the database once. Return ``(logging_enabled, notices)``.

    Raises :class:`BrainError` when Postgres is unreachable, so a dead database
    fails **before** uvicorn starts serving, leaving no server the user would
    then have to go and kill. Since round 11 the port is already bound by
    :func:`claim_port` when this runs (the context needs the port); the CLI
    closes those sockets on the way out.
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
    resolved: Resolved | None = None,
) -> UiContext:
    """Assemble the :class:`UiContext` the app will run on.

    ``resolved`` is what the bind resolves ``host`` to
    (:func:`resolve_bind_addresses`); the loopback test, the Origin and the Host
    allowlist all follow it, so a hostname is judged by where it points.
    """
    from ..search import hybrid_search

    logging_enabled, notices = preflight(cfg)
    loopback = is_loopback(host, resolved)

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
        allowed_origin=bind_origin(host, port, resolved),
        allowed_hosts=host_allowlist(host, resolved),
        logging_enabled=logging_enabled,
        graph_syncer=graph_syncer,
        notices=notices,
        # Confidential bodies leave this process only when the server is
        # loopback-bound (CLI-equivalent trust) or the operator said so.
        # `loopback` is a loopback ADDRESS, or `localhost` resolving only to
        # loopback: a loopback alias is not, so it withholds like a network
        # bind (round 16), and neither is a `localhost` with a mixed answer.
        serve_confidential_bodies=loopback or include_confidential,
        # Confidential TITLES on the unprompted listing surfaces are a
        # separate, config-only decision — deliberately NOT ``or``-ed with
        # ``loopback`` or ``include_confidential``. Both of those say "this
        # session may READ confidential material it opens"; neither says "paint
        # confidential titles in a rail nobody asked for". Default False.
        serve_confidential_titles=cfg.ui_serve_confidential_titles,
        # The Ingest tab (Ingest spec §4.1, D8). The registry always exists; it
        # is the BIND that decides whether ingest is offered: loopback and
        # writable only, because ingest is the largest cost surface the server
        # has (disk, memory, minutes of Ollama time) and a LAN token is the
        # only guard off loopback.
        jobs=JobRegistry(
            clock=lambda: datetime.now(UTC),
            max_queued=MAX_QUEUED_JOBS,
            retain=RETAIN_JOBS,
            retain_for=RETAIN_FOR,
        ),
        ingest_runtime=IngestRuntime.from_config(cfg),
        ingest_enabled=loopback and not read_only,
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


def browser_url(
    host: str, port: int, token: str, resolved: Resolved | None = None
) -> str:
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
    return _with_token(f"{bind_origin(host, port, resolved)}/", token)


def _with_token(url: str, token: str) -> str:
    """``url`` with the token in its fragment, percent-encoded (L-3)."""
    return f"{url}#t={quote(token, safe='')}" if token else url


def serve(
    context: UiContext,
    *,
    host: str,
    port: int,
    sockets: list[socket.socket],
    open_browser: bool,
    opener: Callable[[str], Any] | None = None,
) -> None:
    """Run the app under uvicorn until Ctrl-C, on the already-bound ``sockets``.

    ``sockets`` come from :func:`claim_port` (review round 11): uvicorn serves
    exactly those and never resolves or binds ``host`` itself. ``host`` and
    ``port`` stay in uvicorn's config only for its own messages. A bind error
    is therefore raised by OUR code, with our messages; the handler that used
    to catch EADDRINUSE here was unreachable, because uvicorn catches a bind
    OSError itself and exits (3 from uvicorn 0.50, 1 before).

    Foreground-only by design: no PID file, no launchd plist. Daemonizing a
    *write-capable* HTTP server that could outlive the user's attention is a bad
    idea. The daemons are the vault-sync watcher and the daily brief, which
    serve nothing (the read-only wiki that once had one was deleted at R3).
    """
    try:
        _run(
            context,
            host=host,
            port=port,
            sockets=sockets,
            open_browser=open_browser,
            opener=opener,
        )
    finally:
        # Closed on every exit, including a failure before uvicorn starts
        # (an embedder that will not warm): the sockets were bound by
        # claim_port, so nothing else would release them.
        for sock in sockets:
            sock.close()


def _run(
    context: UiContext,
    *,
    host: str,
    port: int,
    sockets: list[socket.socket],
    open_browser: bool,
    opener: Callable[[str], Any] | None,
) -> None:
    """The body of :func:`serve`; the caller closes ``sockets``."""
    import uvicorn

    from .app import create_app

    # Before serving, so the cost lands in startup rather than in the first
    # request — and before the browser opens, so a warm model is ready by the
    # time anyone can type. The sockets are already bound: a connection made
    # meanwhile waits in the listen backlog.
    warm_embedder(context.embedder)

    # The guard's own origin: the opened tab cannot diverge from the Origin the
    # write guard expects, whatever the host resolved to (round 10).
    url = _with_token(f"{context.allowed_origin}/", context.token)
    app = create_app(context)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)

    if open_browser:
        # Opened after uvicorn starts serving rather than before, so the
        # browser cannot land on a page nobody is answering yet.
        original_startup = server.startup

        async def startup_then_open(*args: Any, **kwargs: Any) -> None:
            # Forward EVERYTHING: uvicorn passes ``sockets=`` here, and a
            # startup without it binds config.host:port itself — the
            # resolve-again bind round 11 removed, back on the default --open
            # path (review round 12, measured against uvicorn 0.54).
            await original_startup(*args, **kwargs)
            (opener or webbrowser.open)(url)

        server.startup = startup_then_open  # type: ignore[method-assign]

    server.run(sockets=sockets)


def with_notice(context: UiContext, notice: str) -> UiContext:
    """Return ``context`` with one more startup notice attached."""
    return replace(context, notices=(*context.notices, notice))
