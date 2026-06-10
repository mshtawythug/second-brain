"""`brain connect` auto-link suggestion sub-app (Plan 07).

Thin Typer orchestration over :mod:`brain.connect`: list the review queue,
refresh candidates, accept (optionally writing the wikilink into the source
vault file), reject, and show stats. All scoring + SQL + writeback primitives
live in :mod:`brain.connect`; this module only maps results to Rich/Typer
output and the plain :mod:`brain.errors` exceptions to ``typer.Exit``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import typer
from rich.table import Table

from . import connect as connect_mod
from .config import Config
from .db import connect
from .errors import (
    ConnectError,
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
)
from .format import console, emit_json

# Default rows shown by ``brain connect list`` / the bare ``brain connect``.
_LIST_DEFAULT_LIMIT = 20

connect_app = typer.Typer(
    name="connect",
    help=(
        "Proactive auto-link suggestions: surface note pairs that share "
        "entities / semantics but aren't linked yet, then accept (write the "
        "wikilink) or reject each one."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)


def _suggestion_json(row: connect_mod.SuggestionRow) -> dict[str, Any]:
    """Serialize a suggestion row for ``--json`` output."""
    return {
        "id": row.id,
        "source_doc_id": row.source_doc_id,
        "target_doc_id": row.target_doc_id,
        "source_title": row.source_title,
        "target_title": row.target_title,
        "score": round(row.score, 6),
        "graph_score": None if row.graph_score is None else round(row.graph_score, 6),
        "embed_score": None if row.embed_score is None else round(row.embed_score, 6),
        "status": row.status,
        "suggested_at": row.suggested_at,
    }


def _fmt_leg(value: float | None) -> str:
    """Render a leg score (graph / embed) or an em dash when absent."""
    return "—" if value is None else f"{value:.2f}"


def _render_list(*, limit: int, json_output: bool, show_all: bool) -> None:
    """Shared body for the bare ``brain connect`` and ``brain connect list``."""
    cfg = Config.load()
    status = None if show_all else "pending"
    with connect(cfg.database_url) as conn:
        rows = connect_mod.iter_suggestions(conn, status=status, limit=limit)
    if json_output:
        emit_json([_suggestion_json(r) for r in rows])
        return
    if not rows:
        typer.echo("no suggestions" if show_all else "no pending suggestions")
        return
    title = "Link suggestions" if show_all else "Pending link suggestions"
    table = Table(title=title)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Source title")
    table.add_column("Target title")
    table.add_column("Score", justify="right")
    table.add_column("Graph", justify="right")
    table.add_column("Embed", justify="right")
    if show_all:
        table.add_column("Status")
    for r in rows:
        cells = [
            r.id[:8],
            r.source_title,
            r.target_title,
            f"{r.score:.3f}",
            _fmt_leg(r.graph_score),
            _fmt_leg(r.embed_score),
        ]
        if show_all:
            cells.append(r.status)
        table.add_row(*cells)
    console.print(table)


@connect_app.callback(invoke_without_command=True)
def connect_default(
    ctx: typer.Context,
    limit: int = typer.Option(
        _LIST_DEFAULT_LIMIT, "--limit", "-n", help="Max suggestions to show."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead."),
    show_all: bool = typer.Option(
        False, "--all", help="Show every status, not just pending."
    ),
) -> None:
    """Show the pending suggestion queue (alias for ``brain connect list``)."""
    if ctx.invoked_subcommand is not None:
        return
    _render_list(limit=limit, json_output=json_output, show_all=show_all)


@connect_app.command("list")
def connect_list(
    limit: int = typer.Option(
        _LIST_DEFAULT_LIMIT, "--limit", "-n", help="Max suggestions to show."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead."),
    show_all: bool = typer.Option(
        False, "--all", help="Show every status, not just pending."
    ),
) -> None:
    """List the link-suggestion review queue (pending by default)."""
    _render_list(limit=limit, json_output=json_output, show_all=show_all)


@connect_app.command("refresh")
def connect_refresh(
    doc: str | None = typer.Option(
        None, "--doc", help="Limit refresh to a single source doc (id prefix)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute candidates without writing to the DB."
    ),
) -> None:
    """Recompute auto-link candidates and upsert pending suggestions.

    Blends an entity-graph affinity leg with an embedding affinity leg (RRF),
    drops already-linked and below-threshold pairs, and persists the top
    suggestions per source doc. Accepted/rejected rows are never overwritten.
    """
    cfg = Config.load()
    try:
        with connect(cfg.database_url) as conn:
            conn.autocommit = True
            result = connect_mod.refresh_suggestions(
                conn, cfg, doc_prefix=doc, dry_run=dry_run
            )
    except (
        IdPrefixTooShort,
        IdPrefixNotHex,
        IdPrefixNotFound,
        IdPrefixAmbiguous,
        ConnectError,
    ) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    prefix = "would write" if dry_run else "wrote"
    typer.echo(
        f"scanned {result.source_docs} source doc(s); "
        f"{result.candidates} candidate pair(s); "
        f"{prefix} {result.written} suggestion(s)"
    )


def _resolve(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve a suggestion-id prefix or exit non-zero with a message."""
    try:
        return connect_mod.resolve_suggestion_prefix(conn, prefix)
    except ConnectError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc


@connect_app.command("accept")
def connect_accept(
    suggestion_id: str = typer.Argument(..., help="Suggestion id prefix (>= 6 chars)."),
    write: bool = typer.Option(
        False, "--write", help="Insert the wikilink into the source vault file."
    ),
) -> None:
    """Mark a suggestion accepted; with ``--write``, insert the wikilink.

    The wikilink is appended (path-form alias) under a ``## See Also`` section
    at the end of the source doc's vault file. The write is idempotent — a
    repeated accept never duplicates the link.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        resolved = _resolve(conn, suggestion_id)
        try:
            ctx = connect_mod.load_action_context(conn, resolved)
        except ConnectError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(code=1) from exc
        # Write the wikilink FIRST so a write failure (missing file / IO error)
        # leaves the suggestion pending rather than frozen accepted-without-link.
        written = _write_wikilink(cfg, ctx) if write else False
        try:
            connect_mod.set_suggestion_status(conn, resolved, "accepted")
        except ConnectError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(code=1) from exc
    suffix = "  (wikilink written)" if written else (
        "  (wikilink already present)" if write else ""
    )
    typer.echo(f"✓ accepted {resolved[:8]}{suffix}")


def _write_wikilink(cfg: Config, action: connect_mod.ActionResult) -> bool:
    """Insert the accepted suggestion's wikilink into the source vault file.

    Returns ``True`` when a new link was written, ``False`` when it was already
    present. Exits non-zero when the source/target vault paths are missing
    (the link can't be located) so the user gets clear feedback.
    """
    if action.source_vault_path is None:
        typer.secho(
            "cannot write wikilink: source doc has no vault file",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)
    if action.target_vault_path is None:
        typer.secho(
            "cannot write wikilink: target doc has no vault path",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)
    source_file = Path(cfg.vault_path) / action.source_vault_path
    if not source_file.is_file():
        typer.secho(
            f"cannot write wikilink: source vault file not found at {source_file}",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)
    wikilink = connect_mod.build_see_also_wikilink(
        action.target_vault_path, action.target_title
    )
    return connect_mod.append_see_also_link(source_file, wikilink)


@connect_app.command("reject")
def connect_reject(
    suggestion_id: str = typer.Argument(..., help="Suggestion id prefix (>= 6 chars)."),
) -> None:
    """Mark a suggestion rejected; it is frozen and not re-proposed on refresh."""
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        resolved = _resolve(conn, suggestion_id)
        try:
            connect_mod.set_suggestion_status(conn, resolved, "rejected")
        except ConnectError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(code=1) from exc
    typer.echo(f"✓ rejected {resolved[:8]}")


@connect_app.command("stats")
def connect_stats(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead."),
) -> None:
    """Show pending / accepted / rejected suggestion counts."""
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        counts = connect_mod.suggestion_counts(conn)
    if json_output:
        emit_json(counts)
        return
    typer.echo(
        f"pending {counts['pending']}  "
        f"accepted {counts['accepted']}  "
        f"rejected {counts['rejected']}"
    )
