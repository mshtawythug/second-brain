"""``brain mcp``: the MCP server's operator commands (RM-46).

``brain mcp serve`` serves the MCP tools over streamable HTTP; ``brain mcp
token`` mints and describes the bearer token it requires. Thin orchestration:
the policy lives in :mod:`brain.mcp_http` (imported inside ``serve``, so
``brain.cli`` never pays for uvicorn or the MCP server) and :mod:`brain.mcp_auth`
(a stdlib leaf). A refused option exits 2, a broken config 1.

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so
``cli.py`` is not edited for it. ``brain-mcp`` itself stays the argument-free
stdio entry point that MCP clients spawn.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

from .config import Config, ConfigError
from .errors import McpTokenError, McpTransportError
from .mcp_auth import (
    TokenInfo,
    default_token_path,
    generate_token,
    read_token,
    resolve_token,
    write_token,
)
from .settings.mcp_http import load_mcp_http_settings

mcp_app = typer.Typer(
    help="Serve the brain's MCP tools to other machines, and manage the token.",
    no_args_is_help=True,
)


def _fail(message: str, code: int) -> typer.Exit:
    """Print ``message`` to stderr and return the exit to raise."""
    typer.secho(f"Error: {message}", fg="red", err=True)
    return typer.Exit(code)


#: Rotation rewrites the file; a running server holds the token it read at start.
RESTART_NOTICE = "restart brain mcp serve to apply it: a running server keeps the old token"
#: ``brain mcp serve`` prefers BRAIN_MCP_TOKEN to any file it would find by default.
ENV_OVERRIDE_NOTICE = (
    "BRAIN_MCP_TOKEN is set: brain mcp serve uses it, not this file, unless it is "
    "started with --token-file"
)


def _token_path(explicit: Path | None) -> tuple[Path, bool]:
    """The file to act on, and whether ``BRAIN_MCP_TOKEN`` overrides it for ``serve``.

    ``--path``, else ``$BRAIN_MCP_TOKEN_PATH``, else ``<brain_home>/mcp_token``.
    ``Config.load_minimal`` publishes the dotenv chain first, so a value set in a
    ``.env`` is honoured; it does not need ``DATABASE_URL``.
    """
    try:
        cfg = Config.load_minimal()
        settings = load_mcp_http_settings(os.environ)
    except ConfigError as exc:
        raise _fail(str(exc), 2) from exc
    if explicit is not None:
        return explicit, False
    path = settings.token_path or default_token_path(cfg.brain_home)
    return path, settings.token is not None


def _describe(path: Path, info: TokenInfo | None, *, show: bool) -> dict[str, Any]:
    """The ``--json`` payload: exactly six keys."""
    return {
        "path": str(path),
        "exists": info is not None,
        "mode": info.mode_octal if info is not None else None,
        "mode_ok": info.mode_ok if info is not None else None,
        "fingerprint": info.fingerprint if info is not None else None,
        "token": info.secret if info is not None and show else None,
    }


@mcp_app.command("token")
def token(
    rotate: bool = typer.Option(
        False, "--rotate", help="Generate a new 256-bit token, replacing any existing one."
    ),
    show: bool = typer.Option(
        False, "--show", help="Print the token itself (to paste into a client config)."
    ),
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Token file. Default: $BRAIN_MCP_TOKEN_PATH or <brain_home>/mcp_token.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Create, rotate or describe the bearer token `brain mcp serve` requires."""
    target, env_overrides = _token_path(path)
    info: TokenInfo | None
    try:
        if rotate:
            write_token(target, generate_token())
        info = read_token(target) if os.path.lexists(target) else None
    except McpTokenError as exc:
        raise _fail(str(exc), 2) from exc
    if as_json:
        typer.echo(json.dumps(_describe(target, info, show=show)))
    elif info is None:
        typer.echo(f"no token at {target}; create one with: brain mcp token --rotate")
    else:
        _print_info(target, info, show=show)
    # Notices go to stderr under --json, so stdout stays one JSON document.
    if rotate:
        typer.echo(RESTART_NOTICE, err=as_json)
    if env_overrides:
        typer.echo(ENV_OVERRIDE_NOTICE, err=as_json)
    if info is None:
        raise typer.Exit(1)


def _print_info(path: Path, info: TokenInfo, *, show: bool) -> None:
    """The human description: never the secret unless ``--show``."""
    mode_note = "OK" if info.mode_ok else f"TOO OPEN — fix with: chmod 600 {path}"
    typer.echo(f"token file   {path}")
    typer.echo(f"mode         {info.mode_octal}  {mode_note}")
    typer.echo(f"fingerprint  {info.fingerprint}")
    if show:
        typer.echo(f"token        {info.secret}")
    else:
        typer.echo("Use --show to print the token. Treat it like a password.")


@mcp_app.command("serve")
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Interface to bind. Non-loopback requires --allow-remote AND a token.",
    ),
    port: int = typer.Option(8787, "--port", help="TCP port to listen on."),
    path: str = typer.Option("/mcp", "--path", help="URL path of the MCP endpoint."),
    allow_remote: bool = typer.Option(
        False,
        "--allow-remote",
        help="Acknowledge that a non-loopback bind exposes the brain to the network.",
    ),
    allow_writes: bool = typer.Option(
        False,
        "--allow-writes",
        help="Also serve the write tools. Off by default: HTTP serves read tools only.",
    ),
    include_confidential: bool = typer.Option(
        False,
        "--include-confidential",
        help="Let callers ask for confidential documents. Off by default: refused.",
    ),
    allowed_host: list[str] | None = typer.Option(
        None,
        "--allowed-host",
        help="Extra Host header value to accept (repeatable), e.g. a tailnet name.",
    ),
    token_file: Path | None = typer.Option(
        None,
        "--token-file",
        help="Bearer-token file. Default: $BRAIN_MCP_TOKEN_PATH or <brain_home>/mcp_token.",
    ),
    no_auth: bool = typer.Option(
        False, "--no-auth", help="Serve without a bearer token. Loopback only."
    ),
    max_body_bytes: int = typer.Option(
        1024 * 1024, "--max-body-bytes", help="Largest request body accepted (1 KiB to 16 MiB)."
    ),
    call_timeout: float = typer.Option(
        300.0, "--call-timeout", help="Seconds a tool call may take before a 504 (1 to 3600)."
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", help="DEBUG, INFO, WARNING, ERROR or CRITICAL."
    ),
) -> None:
    """Serve the MCP tools over streamable HTTP, for another device on a private network."""
    try:
        cfg = Config.load()
    except ConfigError as exc:
        raise _fail(str(exc), 1) from exc
    try:
        settings = load_mcp_http_settings(os.environ)
        token = (
            None
            if no_auth
            else resolve_token(settings, explicit=token_file, brain_home=cfg.brain_home)
        )
    except (ConfigError, McpTokenError) as exc:
        raise _fail(str(exc), 2) from exc
    from .mcp_http import HttpServeOptions, serve_http

    opts = HttpServeOptions(
        host=host,
        port=port,
        path=path,
        allow_remote=allow_remote,
        allow_writes=allow_writes,
        include_confidential=include_confidential,
        allowed_hosts=tuple(allowed_host or ()),
        token=token,
        no_auth=no_auth,
        max_body_bytes=max_body_bytes,
        call_timeout_s=call_timeout,
        log_level=log_level.upper(),
    )
    try:
        serve_http(opts, cfg)
    except McpTransportError as exc:
        raise _fail(str(exc), 2) from exc
    typer.echo("brain mcp serve stopped", err=True)


def register(app: typer.Typer) -> None:
    """Attach the ``mcp`` sub-app to the root app."""
    app.add_typer(mcp_app, name="mcp")
