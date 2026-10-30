"""``brain dedup``: find, list and label near-duplicate documents (RM-19).

Thin Typer orchestration over :mod:`brain.near_dup_store`. ``scan`` signs missing or stale
documents and records verified pairs; ``list`` shows them; ``dismiss`` and ``confirm`` set a
pair's label. Nothing here deletes or merges a document: deleting stays ``brain rm``
(RM-19.D1). The brief daemon runs ``brain dedup scan --limit 500`` on its own line before the
brief, so a failed scan never stops the brief (RM-19.D3).

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so ``cli.py`` is not
edited for it. The terminal is inside the F6 trust boundary, so ``list`` is permissive.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NoReturn

import psycopg
import typer
from rich.table import Table

from .config import Config, ConfigError
from .db import connect
from .errors import IdPrefixError
from .format import console, emit_json
from .near_dup import MIN_TOKENS
from .near_dup_store import (
    STATUSES,
    NearDupError,
    list_pairs,
    set_status,
    tables_present,
)
from .near_dup_store import (
    scan as run_scan,
)
from .queries import resolve_document_prefix

dedup_app = typer.Typer(
    name="dedup",
    help="Near-duplicate documents: find, list, dismiss or confirm pairs.",
    no_args_is_help=True,
)


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=1)


def _config() -> Config:
    try:
        return Config.load()
    except ConfigError as exc:
        _fail(f"Configuration error: {exc}")


@contextmanager
def _session(cfg: Config) -> Iterator[psycopg.Connection[Any]]:
    """An autocommit connection (each scanned document commits alone); maps refusals to exit 1."""
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        if not tables_present(conn):
            _fail("Near-duplicate detection needs migration 030: run `brain init`.")
        try:
            yield conn
        except (NearDupError, IdPrefixError, ValueError) as exc:
            # ValueError: a document paired with itself (near_dup.ordered_pair).
            _fail(str(exc))


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {plural or word + 's'}"


def _status_option() -> Any:
    return typer.Option(
        "open", "--status", help=f"Which pairs to list: {', '.join(STATUSES)}."
    )


@dedup_app.command("scan")
def scan(
    recompute_all: bool = typer.Option(
        False, "--all", help="Re-sign every document, not only new and edited ones."
    ),
    limit: int | None = typer.Option(
        None, "--limit", min=1, help="Sign at most this many documents in this run."
    ),
) -> None:
    """Sign new and edited documents and record the near-duplicate pairs found."""
    cfg = _config()
    with _session(cfg) as conn:
        report = run_scan(conn, recompute_all=recompute_all, limit=limit)
    typer.echo(
        f"Examined {_plural(report.examined, 'document')} "
        f"({report.short} under {MIN_TOKENS} words); "
        f"{_plural(report.pairs_new, 'new pair', 'new pairs')}, {report.pairs_dropped} dropped."
    )
    if report.remaining:
        typer.echo(f"{_plural(report.remaining, 'document')} left for the next scan.")


@dedup_app.command("list")
def list_cmd(
    status: str = _status_option(),
    limit: int = typer.Option(50, "--limit", min=1, help="List at most this many pairs."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List recorded pairs, most similar first."""
    cfg = _config()
    with _session(cfg) as conn:
        rows = list_pairs(conn, status=status, limit=limit)
    if as_json:
        emit_json([row.to_dict() for row in rows])
        return
    if not rows:
        typer.echo(f"No {status} pairs.")
        return
    table = Table(title=f"Possible duplicates ({status})")
    for column in ("Similarity", "Document A", "Document B"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            f"{row.similarity:.2f}",
            f"{row.doc_a.title} ({row.doc_a.document_id[:8]})",
            f"{row.doc_b.title} ({row.doc_b.document_id[:8]})",
        )
    console.print(table)


def _label(first: str, second: str, status: str) -> None:
    cfg = _config()
    with _session(cfg) as conn:
        doc_x = resolve_document_prefix(conn, first)
        doc_y = resolve_document_prefix(conn, second)
        set_status(conn, doc_x, doc_y, status)
    typer.echo(f"Marked the pair {doc_x[:8]} / {doc_y[:8]} {status}.")


@dedup_app.command("dismiss")
def dismiss(
    first: str = typer.Argument(..., help="One document's id or id prefix."),
    second: str = typer.Argument(..., help="The other document's id or id prefix."),
) -> None:
    """Mark a pair as not duplicates; no later scan re-opens it."""
    _label(first, second, "dismissed")


@dedup_app.command("confirm")
def confirm(
    first: str = typer.Argument(..., help="One document's id or id prefix."),
    second: str = typer.Argument(..., help="The other document's id or id prefix."),
) -> None:
    """Mark a pair as duplicates (a label only; delete one with `brain rm`)."""
    _label(first, second, "confirmed")


def register(app: typer.Typer) -> None:
    """Attach the ``dedup`` sub-app to the root app."""
    app.add_typer(dedup_app, name="dedup")
