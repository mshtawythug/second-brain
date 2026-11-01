"""``brain mcp serve``: the MCP tools over streamable HTTP (RM-46).

Opt-in, loopback by default, bearer-authenticated, read-only and
confidential-excluded by default. Design: R1
(``docs/specs/2026-07-25-sections/R1-DEFERRED-mcp-http-transport.md``) as
updated by the roadmap's RM-46 and plan ``docs/plans/2026-10-31-rm-46-mcp-http.md``.

- **Bind policy** (:func:`validate_options`) reuses ``brain ui``'s: the host is
  validated and resolved ONCE, judged by the addresses it resolves to, and
  those exact addresses are bound before uvicorn starts and handed to it as
  sockets (``brain.ui.server.claim_port``), so nothing re-resolves the name.
  Off loopback, ``--allow-remote`` AND a token are both required.
- **Host header**: starlette's ``TrustedHostMiddleware`` checks every path,
  health routes included; the SDK also gets an explicit
  ``TransportSecuritySettings`` (the same hosts, no allowed browser origins),
  which keeps its loopback auto-branch from ever running.
- **State**: :func:`brain.mcp_server.start_state` builds what stdio builds, but
  with a connection per call (RM-46.D5): sync tools run concurrently on worker
  threads, and one psycopg connection must not be shared by them.
- **Read-only**: unless ``--allow-writes``, every tool off
  :data:`brain.mcp_tool_classes.READ_ONLY_TOOLS` is removed before listening.
- **Sessions**: stateless, JSON responses (RM-46.D6); nothing accumulates per
  client, and the response guard can bound each result.

Needs the mcp 2.x server API (``pip install 'secondbrain-py[http]'``); stdio
does not. Only ``brain.cli_mcp.serve``'s body imports this module.
"""
from __future__ import annotations

import importlib.metadata
import logging
import re
import socket
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import anyio
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import mcp_http_conn, mcp_server
from .config import Config
from .errors import BrainError, McpTransportError
from .mcp_auth import BearerAuthMiddleware, TokenInfo
from .mcp_compat import MCP_MAJOR
from .mcp_http_conn import (
    IDLE_TIMEOUT_S,
    MAX_CALLS,
    MAX_CONNECTIONS,
    WorkLimitMiddleware,
    idle_deadline_protocol,
)
from .mcp_http_guard import (
    HEALTHZ_PATH,
    READYZ_PATH,
    ASGIApp,
    CloseOnErrorMiddleware,
    ConfidentialGateMiddleware,
    EndpointMethodMiddleware,
    HealthzRoute,
    PathAllowlistMiddleware,
    ReadyzRoute,
    RequestLogMiddleware,
    ResponseGuardMiddleware,
)
from .mcp_http_tools import sanitize_tool_errors
from .mcp_tool_classes import (
    ToolRegistry,
    apply_hide_mode,
    apply_read_only,
    registered_tool_names,
)
from .ui.context import (
    HostRefused,
    Resolved,
    address_class,
    bind_origin,
    host_allowlist,
    literal_addresses,
    validate_host,
)
from .ui.server import claim_port, resolve_bind_addresses

logger = logging.getLogger("brain.mcp.http")

DEFAULT_HOST = "127.0.0.1"
#: Away from uvicorn's 8000, `brain ui`'s 8765 and the 55432/55433/5434 block.
DEFAULT_PORT = 8787
DEFAULT_PATH = "/mcp"
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
MIN_BODY_BYTES = 1024
MAX_BODY_BYTES = 16 * 1024 * 1024
#: The largest tool result returned; the tools' own payload ceilings sit far below it.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
#: ``brain_ask`` plans, retrieves and synthesizes through a local model; give it room.
DEFAULT_CALL_TIMEOUT_S = 300.0
MIN_CALL_TIMEOUT_S = 1.0
MAX_CALL_TIMEOUT_S = 3600.0
TIMEOUT_KEEP_ALIVE_S = 5
TIMEOUT_GRACEFUL_SHUTDOWN_S = 10
#: How long ``/readyz`` waits for the database before reporting it unavailable.
READY_CONNECT_TIMEOUT_S = 3
READY_STATEMENT_TIMEOUT_MS = 2000

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_PATH = re.compile(r"/[A-Za-z0-9/_-]{1,64}")
_RESERVED_PATHS = frozenset({HEALTHZ_PATH, READYZ_PATH})

HTTP_EXTRA_HINT = "pip install 'secondbrain-py[http]'"
_REMOTE_HELP = (
    "\n\n  1. brain mcp token --rotate          # writes <brain_home>/mcp_token (mode 0600)\n"
    "  2. {step2}\n\n"
    "Never expose this port to the public internet or port-forward it. Put both\n"
    "machines on a private network (e.g. Tailscale) and bind the private address.\n"
    "See docs/guides/mcp-http-transport.md."
)
REFUSE_NO_ALLOW_REMOTE = (
    "refusing to bind {host} — a non-loopback bind requires BOTH an explicit "
    "--allow-remote flag AND a bearer token." + _REMOTE_HELP
)
REFUSE_NO_TOKEN_REMOTE = (
    "refusing to bind {host} without a bearer token — the token is mandatory off "
    "loopback." + _REMOTE_HELP
)
#: Step 2 for a wildcard bind (rev-118 #7): it answers only loopback Host values,
#: so "--host 0.0.0.0 --allow-remote" alone starts a server no other device can use.
_WILDCARD_STEP2 = (
    "brain mcp serve --host <this machine's private address> --allow-remote\n"
    "     (or keep --host {host} and add --allowed-host <the name clients use>: a\n"
    "     wildcard bind answers only loopback Host values)"
)
REFUSE_NO_AUTH_WIDENED = (
    "--no-auth cannot be combined with --allow-remote or --allowed-host: both open the "
    "server beyond this machine's loopback names, and then a token is mandatory."
)
REFUSE_NO_TOKEN = (
    "no bearer token: create one with `brain mcp token --rotate`, or pass --no-auth "
    "to serve this loopback bind without one."
)


@dataclass(frozen=True)
class HttpServeOptions:
    """One ``brain mcp serve`` invocation's options, before validation."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    allow_remote: bool = False
    allow_writes: bool = False
    include_confidential: bool = False
    allowed_hosts: tuple[str, ...] = ()
    token: TokenInfo | None = None
    no_auth: bool = False
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    log_level: str = "INFO"


@dataclass(frozen=True)
class BindPlan:
    """What :func:`validate_options` decided: where to bind and which Host values to trust."""

    host: str
    addresses: Resolved
    kind: str
    allowed_hosts: frozenset[str]


def _check_ranges(opts: HttpServeOptions) -> None:
    """Refuse an option outside its range; every message names the flag."""
    if not 1 <= opts.port <= 65535:
        raise McpTransportError(f"--port must be 1 to 65535 (got {opts.port})")
    if not _PATH.fullmatch(opts.path) or opts.path in _RESERVED_PATHS:
        raise McpTransportError(
            "--path must be '/' and 1 to 64 letters, digits, '/', '_' or '-', "
            "and not /healthz or /readyz"
        )
    if not MIN_BODY_BYTES <= opts.max_body_bytes <= MAX_BODY_BYTES:
        raise McpTransportError(
            f"--max-body-bytes must be {MIN_BODY_BYTES} to {MAX_BODY_BYTES}"
        )
    if not MIN_CALL_TIMEOUT_S <= opts.call_timeout_s <= MAX_CALL_TIMEOUT_S:
        raise McpTransportError(
            f"--call-timeout must be {MIN_CALL_TIMEOUT_S:g} to {MAX_CALL_TIMEOUT_S:g} seconds"
        )
    if opts.log_level.upper() not in _LOG_LEVELS:
        raise McpTransportError("--log-level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")


def _extra_host(value: str) -> str:
    """One ``--allowed-host`` value as the Host it arrives as: no port, no wildcard."""
    refused = McpTransportError(
        f"--allowed-host {value!r} must be a hostname or IP address, with no scheme, "
        "port, path or wildcard"
    )
    try:
        canonical = validate_host(value)
    except HostRefused:
        raise refused from None
    addresses = literal_addresses(canonical)
    if any(address.is_unspecified for address in addresses):
        raise refused
    return f"[{canonical}]" if ":" in canonical else canonical


def validate_options(opts: HttpServeOptions, *, resolver: Any = None) -> BindPlan:
    """The one place the bind policy lives. Raises :class:`McpTransportError`.

    ``resolver`` replaces ``socket.getaddrinfo`` in tests, so no test touches DNS.
    """
    _check_ranges(opts)
    try:
        host = validate_host(opts.host)
    except HostRefused as exc:
        raise McpTransportError(str(exc)) from None
    extras = {_extra_host(value) for value in opts.allowed_hosts}
    try:
        addresses = resolve_bind_addresses(host, resolver)
    except BrainError as exc:
        raise McpTransportError(str(exc)) from None
    kind = address_class(host, addresses)
    if opts.no_auth and (opts.allow_remote or opts.allowed_hosts):
        raise McpTransportError(REFUSE_NO_AUTH_WIDENED)
    if kind != "loopback":
        step2 = (
            _WILDCARD_STEP2.format(host=host)
            if kind == "wildcard"
            else f"brain mcp serve --host {host} --allow-remote"
        )
        if not opts.allow_remote:
            raise McpTransportError(REFUSE_NO_ALLOW_REMOTE.format(host=host, step2=step2))
        if opts.token is None:
            raise McpTransportError(REFUSE_NO_TOKEN_REMOTE.format(host=host, step2=step2))
    if not opts.no_auth and opts.token is None:
        raise McpTransportError(REFUSE_NO_TOKEN)
    if not opts.no_auth and opts.token is not None and not opts.token.mode_ok:
        raise McpTransportError(
            f"token file {opts.token.path} is mode {opts.token.mode_octal}; expected 0600. "
            f"Fix with: chmod 600 {opts.token.path}"
        )
    return BindPlan(
        host=host,
        addresses=addresses,
        kind=kind,
        allowed_hosts=host_allowlist(host, addresses) | extras,
    )


def build_transport_security(plan: BindPlan) -> Any:
    """The SDK's own Host/Origin check: the plan's hosts, any port, and NO browser origin."""
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = sorted(plan.allowed_hosts)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, *(f"{host}:*" for host in hosts)],
        # Empty: a request carrying any Origin (a browser) gets 403; MCP
        # clients send none, and the SDK lets an absent Origin through.
        allowed_origins=[],
    )


class HttpServer(ToolRegistry, Protocol):
    """The SDK server surface the HTTP transport uses (``MCPServer`` on mcp 2.x)."""

    def streamable_http_app(self, **kwargs: Any) -> Any:
        """The streamable-HTTP ASGI app for the registered tools."""
        ...


def build_http_app(
    server: HttpServer,
    opts: HttpServeOptions,
    plan: BindPlan,
    *,
    probe: Callable[[], Awaitable[dict[str, Any]]],
    max_calls: int = MAX_CALLS,
) -> ASGIApp:
    """The SDK app wrapped in every guard, outermost last in this function."""
    app: ASGIApp = server.streamable_http_app(
        streamable_http_path=opts.path,
        json_response=True,
        stateless_http=True,
        max_request_body_size=opts.max_body_bytes,
        transport_security=build_transport_security(plan),
        host=plan.host,
    )
    if not opts.include_confidential:
        app = ConfidentialGateMiddleware(app, max_body_bytes=opts.max_body_bytes)
    app = ResponseGuardMiddleware(
        app,
        max_response_bytes=MAX_RESPONSE_BYTES,
        timeout_s=opts.call_timeout_s,
    )
    # sec-118 #3/#4: work is capped in the app, orphaned workers included.
    app = WorkLimitMiddleware(app, max_calls=max_calls)
    app = ReadyzRoute(app, probe=probe)
    if not opts.no_auth:
        assert opts.token is not None  # validate_options guarantees it
        app = BearerAuthMiddleware(app, token=opts.token, exempt_paths=frozenset({HEALTHZ_PATH}))
    app = HealthzRoute(app)
    # sec-118b N3: the endpoint answers POST only (no never-ending GET stream).
    # Inside the Host check, so a rebinding request is refused on Host first.
    app = EndpointMethodMiddleware(app, endpoint=opts.path)
    app = cast(
        ASGIApp,
        TrustedHostMiddleware(
            cast(Any, app), allowed_hosts=sorted(plan.allowed_hosts), www_redirect=False
        ),
    )
    # Outermost but for the log: only the three exact paths reach anything else.
    app = PathAllowlistMiddleware(
        app, paths=frozenset({opts.path, HEALTHZ_PATH, READYZ_PATH})
    )
    # sec-118b N4: every refusal closes its connection.
    app = CloseOnErrorMiddleware(app)
    return RequestLogMiddleware(app, endpoint=opts.path)


def distribution_version(name: str) -> str:
    """The installed version of distribution ``name``, or ``"unknown"``."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def with_statement_timeout(database_url: str, timeout_ms: int) -> str:
    """``database_url`` with ``statement_timeout`` added to its libpq ``options``.

    Every connection the HTTP server opens (one per tool call, RM-46.D5) then
    cancels a statement that outlives the call's deadline, instead of running on
    after the 504 (sec-118 #4). Existing ``options`` are kept. Works on a URI and
    on a ``key=value`` connection string.
    """
    setting = f"-c statement_timeout={timeout_ms}"
    if "://" not in database_url:
        current = conninfo_to_dict(database_url).get("options")
        merged = f"{current} {setting}" if current else setting
        return make_conninfo(database_url, options=merged)
    parts = urlsplit(database_url)
    # Split by hand and keep every other parameter's raw text: parse_qsl reads
    # '+' as a space, libpq reads it literally (sec-118b).
    fields = [field for field in parts.query.split("&") if field]
    existing = [
        unquote(field.partition("=")[2])
        for field in fields
        if unquote(field.partition("=")[0]) == "options"
    ]
    kept = [field for field in fields if unquote(field.partition("=")[0]) != "options"]
    merged = " ".join([*existing, setting])
    encoded = "&".join([*kept, "options=" + quote(merged, safe="")])
    return urlunsplit(parts._replace(query=encoded))


def database_reachable(database_url: str) -> bool:
    """``SELECT 1`` on a fresh connection, bounded in connect and statement time."""
    try:
        with psycopg.connect(
            database_url,
            connect_timeout=READY_CONNECT_TIMEOUT_S,
            options=f"-c statement_timeout={READY_STATEMENT_TIMEOUT_MS}",
        ) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error:
        return False
    return True


def readiness_probe(
    cfg: Config, *, read_only: bool, tools: int
) -> Callable[[], Awaitable[dict[str, Any]]]:
    """``/readyz``'s body: whether the brain is usable. No corpus statistics, no paths."""

    # Its own worker thread, so a busy server still answers readiness.
    limiter: anyio.CapacityLimiter | None = None

    async def probe() -> dict[str, Any]:
        nonlocal limiter
        if limiter is None:
            limiter = anyio.CapacityLimiter(1)
        reachable = await anyio.to_thread.run_sync(
            database_reachable, cfg.database_url, limiter=limiter
        )
        return {
            "status": "ok" if reachable else "degraded",
            "database": "ok" if reachable else "unavailable",
            "embedder": cfg.embedder,
            "read_only": read_only,
            "tools": tools,
            "version": distribution_version("secondbrain-py"),
        }

    return probe


def uvicorn_config_kwargs(plan: BindPlan, port: int, log_level: str) -> dict[str, Any]:
    """The fixed uvicorn hardening (RM-46.D12), in one place a test can pin."""
    return {
        "host": plan.host,
        "port": port,
        "log_level": log_level.lower(),
        "access_log": False,
        # The client address comes from the socket: no X-Forwarded-For trust.
        "proxy_headers": False,
        "server_header": False,
        # Connections, loosely: idle ones are closed by the idle deadline, and
        # work is capped in the app (WorkLimitMiddleware). sec-118 #3.
        "limit_concurrency": MAX_CONNECTIONS,
        "http": idle_deadline_protocol(IDLE_TIMEOUT_S),
        "timeout_keep_alive": TIMEOUT_KEEP_ALIVE_S,
        "timeout_graceful_shutdown": TIMEOUT_GRACEFUL_SHUTDOWN_S,
        "lifespan": "on",
    }


def banner_lines(
    opts: HttpServeOptions,
    plan: BindPlan,
    port: int,
    *,
    served: int,
    total: int,
    embedder: str,
    hidden: int = 0,
) -> list[str]:
    """What ``brain mcp serve`` prints to stderr once it is bound. Never the token."""
    origin = bind_origin(plan.host, port, plan.addresses)
    graph = f"; {hidden} graph and search-log tools withheld" if hidden else ""
    if opts.allow_writes:
        mode = (
            f"READ-WRITE ({served} of {total} tools; --allow-writes: write tools exposed{graph})"
        )
    else:
        writes = total - served - hidden
        mode = f"READ-ONLY ({served} of {total} tools; {writes} write tools withheld{graph})"
    confidential = (
        "SERVED (--include-confidential)"
        if opts.include_confidential
        else "EXCLUDED (pass --include-confidential to serve)"
    )
    if opts.no_auth or opts.token is None:
        auth = "NONE (--no-auth; loopback only)"
    else:
        source = str(opts.token.path) if opts.token.path is not None else "BRAIN_MCP_TOKEN"
        auth = f"bearer token from {source} (fp {opts.token.fingerprint})"
    exposure = (
        "Bind is loopback-only. To reach this from another device, put BOTH machines on"
        " a private network (e.g. Tailscale) — never port-forward this to the internet."
        if plan.kind == "loopback"
        else "Bind is NOT loopback: reachable from the network, guarded by the token."
        " Keep it on a private network (e.g. Tailscale) — never port-forward it."
    )
    return [
        "brain mcp serve — streamable HTTP",
        f"  endpoint      {origin}{opts.path}",
        f"  health        {origin}{HEALTHZ_PATH}",
        f"  mode          {mode}",
        f"  confidential  {confidential}",
        f"  auth          {auth}",
        f"  allow-host    {', '.join(sorted(plan.allowed_hosts))}",
        f"  embedder      {embedder}",
        exposure,
        "Ctrl-C to stop.",
    ]


def _run_uvicorn(  # pragma: no cover - exercised by tests/test_mcp_http_protocol.py's subprocess
    app: ASGIApp, sockets: list[socket.socket], kwargs: dict[str, Any]
) -> None:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, **kwargs))
    server.run(sockets=sockets)


def _configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper())
    logging.basicConfig(stream=sys.stderr, level=numeric)
    logging.getLogger("brain.mcp").setLevel(numeric)


Runner = Callable[[ASGIApp, list[socket.socket], dict[str, Any]], None]


def serve_http(
    opts: HttpServeOptions,
    cfg: Config,
    *,
    server: HttpServer | None = None,
    mcp_major: int = MCP_MAJOR,
    resolver: Any = None,
    run: Runner = _run_uvicorn,
    announce: Callable[[str], None] | None = None,
) -> None:
    """Validate, bind, build state, withhold write tools, and serve until stopped.

    ``server`` defaults to the real tool registry; tests pass a throwaway one.
    ``run`` and ``announce`` stand in for uvicorn and stderr in tests.
    """
    if mcp_major < 2:
        raise McpTransportError(
            f"brain mcp serve needs mcp 2.x (installed: {distribution_version('mcp')}); "
            f"run {HTTP_EXTRA_HINT}"
        )
    # Fail closed before binding if uvicorn moved what the idle deadline reads.
    mcp_http_conn.check_uvicorn_internals()
    plan = validate_options(opts, resolver=resolver)
    target = server if server is not None else cast(HttpServer, mcp_server.mcp_app)
    try:
        port, sockets = claim_port(
            opts.port, host=plan.host, addresses=plan.addresses, auto=False
        )
    except BrainError as exc:
        raise McpTransportError(str(exc)) from None
    try:
        _configure_logging(opts.log_level)
        # sec-118 #4: no statement outlives the call that started it.
        timeout_ms = int(opts.call_timeout_s * 1000)
        cfg = replace(cfg, database_url=with_statement_timeout(cfg.database_url, timeout_ms))
        mcp_server.start_state(
            cfg,
            persistent_db=False,
            hide_confidential=not opts.include_confidential,
            # sec-118c LOW-2: brain_ask stops working, not just waiting, at the deadline.
            call_timeout_s=opts.call_timeout_s,
        )
        names = anyio.run(registered_tool_names, target)
        withheld = () if opts.allow_writes else apply_read_only(target, names)
        # Round 5: the entity graph is derived from every document, confidential
        # ones included, so the graph family needs --include-confidential; so do
        # the pinned extras (weekly communities, the search log; sec-118e/f).
        hidden = () if opts.include_confidential else apply_hide_mode(
            target, [name for name in names if name not in withheld]
        )
        withheld = (*withheld, *hidden)
        # sec-118 #5: an unexpected failure answers its class, never its text.
        sanitize_tool_errors(
            target,
            [name for name in names if name not in withheld],
            # sec-118b N1: a person filter's email keys are not needed remotely.
            drop_keys=frozenset() if opts.include_confidential else frozenset({"person_keys"}),
        )
        served = len(names) - len(withheld)
        probe = readiness_probe(cfg, read_only=not opts.allow_writes, tools=served)
        app = build_http_app(target, opts, plan, probe=probe)
        say = announce or (lambda line: print(line, file=sys.stderr))
        for line in banner_lines(
            opts, plan, port, served=served, total=len(names), embedder=cfg.embedder,
            hidden=len(hidden),
        ):
            say(line)
        run(app, sockets, uvicorn_config_kwargs(plan, port, opts.log_level))
    finally:
        for sock in sockets:
            sock.close()
