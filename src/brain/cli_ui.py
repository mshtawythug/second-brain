"""`brain ui` — the Typer surface over :mod:`brain.ui`.

Rendering and gating only; every decision lives in ``brain.ui.server`` so it
stays testable without a Typer runner. Follows the established extracted-command
shape (``cli_backup.py``, ``cli_connect.py``): ``cli.py`` imports *this* module,
never the reverse.

**Import-cost discipline.** ``cli.py`` already carries an explicit comment about
avoiding expensive module-scope imports. This module imports only ``typer``,
``Config``, and the error types at module level; ``brain.ui.server`` — and
through it ``starlette`` and ``uvicorn`` — is imported inside the command body.
``tests/test_cli_ui.py::test_importing_brain_cli_does_not_pull_starlette_or_uvicorn``
asserts that importing ``brain.cli`` leaves both out of ``sys.modules``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from .config import Config
from .errors import BrainError

if TYPE_CHECKING:
    import socket

    from .ui.context import Resolved

#: The panel's --lan hint. A constant so tests can assert its absence on other
#: binds by the text itself, not by a guessed fragment (review round 6).
PHONE_LINE = "open the URL above on your phone, on this network"

#: Registered as a sub-app with ``invoke_without_command=True`` so ``brain ui``
#: runs the server directly, while leaving room for ``brain ui open`` later
#: without a breaking change. Same shape as ``capture_app``.
ui_app = typer.Typer(
    help="Serve the local web UI for your brain.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _build_embedder(cfg: Config) -> Any:
    """Build the configured embedder via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_embedder(cfg)  # type: ignore[attr-defined]


def _panel(
    *,
    url: str,
    cfg: Config,
    read_only: bool,
    loopback: bool,
    serve_confidential: bool,
    notices: tuple[str, ...],
    note_count: int | None,
    lan: bool = False,
) -> None:
    """Print the startup panel, in the `brain doctor` / `brain setup` style."""
    typer.echo("")
    typer.echo("🧠 brain ui")
    typer.echo("")
    typer.echo(f"  URL        {url}")
    if lan:
        typer.echo(f"  Phone      {PHONE_LINE}")
    if read_only:
        typer.secho("  Mode       read-only  ·  every write is blocked", fg="yellow")
    else:
        typer.echo("  Mode       read-write")

    count = f"{note_count:,} notes" if note_count is not None else ""
    typer.echo(f"  Vault      {cfg.vault_path}   {count}")
    # Host, port and database name only — never the URL, never the password.
    # `mcp_server._wrap_db_error` already established this convention.
    typer.echo(f"  Database   {_describe_db(cfg.database_url)}")
    typer.echo(f"  Embedder   {cfg.embedder}")
    typer.echo("")

    for notice in notices:
        typer.secho(f"  ⚠  {notice}", fg="yellow")
    if notices:
        typer.echo("")

    # Always state the effective confidentiality mode, so an operator who
    # passed --include-confidential on a non-loopback bind SEES that they did.
    # A flag that silently widens egress is the one worth printing.
    if loopback:
        typer.echo("  Confidential  bodies served (loopback bind)")
    elif serve_confidential:
        typer.secho(
            "  Confidential  bodies SERVED over the network "
            "(--include-confidential)",
            fg="red",
        )
    else:
        typer.echo("  Confidential  bodies withheld (non-loopback bind)")
    typer.echo("")

    if not loopback:
        typer.secho(
            "  ⚠  This server is NOT bound to loopback. Anyone who can reach\n"
            "     this address can read your entire brain.",
            fg="yellow",
        )
        typer.echo("")
    typer.echo("  Press Ctrl-C to stop.")
    typer.echo("")


def _describe_db(database_url: str) -> str:
    """``dbname @ host:port`` — never the credentials."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(database_url)
        name = (parsed.path or "/").lstrip("/") or "?"
        return f"{name} @ {parsed.hostname or '?'}:{parsed.port or '?'}"
    except ValueError:  # pragma: no cover — urlparse is extremely tolerant
        return "?"


@ui_app.callback()
def ui(
    ctx: typer.Context,
    # 1 to 65535 (review round 12): bind raised a raw OverflowError past the
    # top, and 0 bound a different ephemeral port on each address.
    port: int = typer.Option(
        8765, "--port", min=1, max=65535, help="Port to bind (1 to 65535)."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open a browser on start."
    ),
    read_only: bool = typer.Option(
        False, "--read-only", help="Serve read-only; block every mutation."
    ),
    token: str = typer.Option(
        "", "--token", help="Shared secret. Required when --host is not loopback."
    ),
    include_confidential: bool = typer.Option(
        False,
        "--include-confidential",
        help=(
            "Serve bodies of documents marked sensitivity=confidential even on "
            "a non-loopback bind. Ignored on loopback, where they are always "
            "served."
        ),
    ),
    auto_port: bool = typer.Option(
        True, "--auto-port/--no-auto-port", help="Bump to the next free port if taken."
    ),
    lan: bool = typer.Option(
        False,
        "--lan",
        help=(
            "Serve to other devices on this network (e.g. a phone): bind this "
            "machine's LAN address and require a token, minting one unless "
            "--token is given. Cannot be combined with --host."
        ),
    ),
) -> None:
    """Serve the local web UI: browse, search, and edit your brain."""
    if ctx.invoked_subcommand is not None:
        return

    from .ui import lan as lan_mod
    from .ui import server as ui_server
    from .ui.context import HostRefused, address_class, validate_host
    from .ui.server import claim_port

    # Once, here: every consumer below — the bind, the allowlist,
    # the URL — gets the canonical bare address (bind_origin re-brackets it
    # for URLs), and every refused spelling becomes a usage error. The rules
    # live in ONE function, context.validate_host (review round 9).
    try:
        host = validate_host(host)
    except HostRefused as exc:
        raise typer.BadParameter(str(exc)) from exc

    # Review round 5: the token travels in an HTTP header. Non-ASCII made every
    # API call a 500; edge whitespace is trimmed in transit, so it could never
    # match. Refuse both here, before anything binds.
    if token and not (token.isascii() and token.isprintable() and token == token.strip()):
        raise typer.BadParameter(
            "--token must be printable ASCII with no leading or trailing "
            "whitespace — it is sent in an HTTP header."
        )

    if lan:
        # Phase 7 / spec Q1: the phone-reading replacement for the wiki. Two
        # answers to "where do I bind" is a usage error, not a precedence rule.
        # Compared by NAME: Typer vendors its own click, so `click.core`'s
        # ParameterSource is a different enum and an `is` check never matches.
        source = ctx.get_parameter_source("host")
        if source is not None and source.name == "COMMANDLINE":
            raise typer.BadParameter("--lan picks the address itself; drop --host.")
        try:
            host = lan_mod.detect_lan_address()
        except BrainError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(1) from exc
        # Minted, never empty: --lan must not be a way around the token rule.
        token = token or lan_mod.new_token()

    # Review round 10: classify by what the bind RESOLVES the host to, once,
    # here — a name pointing at 0.0.0.0 is a wildcard, and `localhost` is
    # loopback only if every address it resolves to is. Literals need no DNS.
    try:
        resolved = ui_server.resolve_bind_addresses(host)
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc
    kind = address_class(host, resolved)
    loopback = kind == "loopback"
    if not loopback and not token:
        raise typer.BadParameter(
            "binding to a non-loopback address exposes your entire brain to the "
            "network; pass --token <secret> to proceed, or drop --host."
        )

    try:
        cfg = Config.load()
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc

    if not cfg.vault_path.is_dir():
        typer.secho(
            f"vault not found at {cfg.vault_path}\n  Run: brain vault init",
            fg="red",
            err=True,
        )
        raise typer.Exit(1)

    # Round 11: bound HERE, to exactly the addresses classified above, and
    # handed to uvicorn as live sockets — it never resolves `host` again, and
    # with no separate probe there is no window for another process to take
    # the port before it is served.
    try:
        resolved_port, sockets = claim_port(
            port, host=host, auto=auto_port, addresses=resolved
        )
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc

    try:
        _start(
            cfg,
            host=host,
            port=port,
            resolved_port=resolved_port,
            sockets=sockets,
            resolved=resolved,
            kind=kind,
            token=token,
            read_only=read_only,
            include_confidential=include_confidential,
            open_browser=open_browser,
            lan=lan,
        )
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc
    finally:
        # serve() closes them too; this covers a failure before it runs.
        for sock in sockets:
            sock.close()
    typer.echo("brain ui stopped")


def _start(
    cfg: Config,
    *,
    host: str,
    port: int,
    resolved_port: int,
    sockets: list[socket.socket],
    resolved: Resolved,
    kind: str,
    token: str,
    read_only: bool,
    include_confidential: bool,
    open_browser: bool,
    lan: bool,
) -> None:
    """Build the context on the bound port, print the panel, and serve."""
    from .ui import lan as lan_mod
    from .ui.server import browser_url, build_context, serve

    context = build_context(
        cfg,
        host=host,
        port=resolved_port,
        read_only=read_only,
        token=token,
        include_confidential=include_confidential,
        embedder=_build_embedder(cfg),
        resolved=resolved,
    )

    if resolved_port != port:
        typer.secho(
            f"port {port} was busy — using {resolved_port} instead", fg="yellow"
        )

    notices = context.notices
    if kind == "wildcard":
        notices = (*notices, lan_mod.WILDCARD_NOTICE)
    _panel(
        url=browser_url(host, resolved_port, token, resolved=resolved),
        cfg=cfg,
        read_only=read_only,
        loopback=kind == "loopback",
        serve_confidential=context.serve_confidential_bodies,
        notices=notices,
        note_count=None,
        lan=lan,
    )
    serve(
        context,
        host=host,
        port=resolved_port,
        sockets=sockets,
        open_browser=open_browser,
    )


def register_ui_commands(app: typer.Typer) -> None:
    """Attach the ``ui`` sub-app to ``app``.

    Called from ``cli.py``; Typer lists commands in registration order, so the
    position of that call decides where ``ui`` appears in ``brain --help``.
    """
    app.add_typer(ui_app, name="ui")
