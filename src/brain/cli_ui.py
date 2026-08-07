"""`brain ui` — the Typer surface over :mod:`brain.ui`.

Rendering and gating only; every decision lives in ``brain.ui.server`` so it
stays testable without a Typer runner. Follows the established extracted-command
shape (``cli_backup.py``, ``cli_connect.py``): ``cli.py`` imports *this* module,
never the reverse.

**Import-cost discipline.** ``cli.py`` already carries an explicit comment about
avoiding expensive module-scope imports. This module imports only ``typer``,
``Config``, and the error types at module level; ``brain.ui.server`` — and
through it ``starlette`` and ``uvicorn`` — is imported inside the command body.
``tests/test_ui_import_cost.py`` asserts that importing ``brain.cli`` leaves
both out of ``sys.modules``.
"""
from __future__ import annotations

from typing import Any

import typer

from .config import Config
from .errors import BrainError

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
) -> None:
    """Print the startup panel, in the `brain doctor` / `brain setup` style."""
    typer.echo("")
    typer.echo("🧠 brain ui")
    typer.echo("")
    typer.echo(f"  URL        {url}")
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
    port: int = typer.Option(8765, "--port", help="Port to bind."),
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
) -> None:
    """Serve the local web UI: browse, search, and edit your brain."""
    if ctx.invoked_subcommand is not None:
        return

    from .ui.security import is_loopback
    from .ui.server import build_context, resolve_port, serve

    loopback = is_loopback(host)
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

    try:
        resolved_port = resolve_port(port, host=host, auto=auto_port)
        context = build_context(
            cfg,
            host=host,
            port=resolved_port,
            read_only=read_only,
            token=token,
            include_confidential=include_confidential,
            embedder=_build_embedder(cfg),
        )
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc

    if resolved_port != port:
        typer.secho(
            f"port {port} was busy — using {resolved_port} instead", fg="yellow"
        )

    url = f"http://{host}:{resolved_port}/"
    _panel(
        url=url if loopback else f"{url}#t={token}",
        cfg=cfg,
        read_only=read_only,
        loopback=loopback,
        serve_confidential=context.serve_confidential_bodies,
        notices=context.notices,
        note_count=None,
    )

    try:
        serve(
            context, host=host, port=resolved_port, open_browser=open_browser
        )
    except BrainError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1) from exc
    typer.echo("brain ui stopped")


def register_ui_commands(app: typer.Typer) -> None:
    """Attach the ``ui`` sub-app to ``app``.

    Called from ``cli.py``; Typer lists commands in registration order, so the
    position of that call decides where ``ui`` appears in ``brain --help``.
    """
    app.add_typer(ui_app, name="ui")
