"""``brain mcp``: the MCP server's operator commands (RM-46).

``brain mcp token`` mints and describes the bearer token. Thin orchestration
over :mod:`brain.mcp_auth`, which is a stdlib leaf, so this module is cheap to
import with ``brain.cli``.

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
from .errors import McpTokenError
from .mcp_auth import (
    TokenInfo,
    default_token_path,
    generate_token,
    read_token,
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


def _token_path(explicit: Path | None) -> Path:
    """``--path``, else ``$BRAIN_MCP_TOKEN_PATH``, else ``<brain_home>/mcp_token``.

    ``Config.load_minimal`` publishes the dotenv chain first, so a path set in a
    ``.env`` is honoured; it does not need ``DATABASE_URL``.
    """
    if explicit is not None:
        return explicit
    try:
        cfg = Config.load_minimal()
        settings = load_mcp_http_settings(os.environ)
    except ConfigError as exc:
        raise _fail(str(exc), 2) from exc
    return settings.token_path or default_token_path(cfg.brain_home)


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
    target = _token_path(path)
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


def register(app: typer.Typer) -> None:
    """Attach the ``mcp`` sub-app to the root app."""
    app.add_typer(mcp_app, name="mcp")
