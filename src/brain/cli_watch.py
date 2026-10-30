"""``brain watch``: saved searches and watch lists (RM-15).

Thin Typer orchestration over :mod:`brain.saved_search` (the rows and hit reads) and
:mod:`brain.saved_search_check` (seeding, checking, peeking). The daily brief checks every enabled
watch and lists the new hits; these commands manage the watches and check them by hand.

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so ``cli.py`` is not
edited for it. The embedder is reached through ``brain.cli._build_embedder`` at call time (the
CLI's one patch point, as ``brain.cli_search`` does), and only for a semantic watch.

The terminal is inside the F6 trust boundary, so every listing here is permissive.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, NoReturn

import psycopg
import typer
from rich.table import Table

from .config import Config, ConfigError
from .db import connect
from .durations import since_window
from .errors import PersonAmbiguous, PersonNotFound
from .format import console, emit_json
from .ingest import Embedder
from .saved_search import (
    MODE_LEXICAL,
    MODE_SEMANTIC,
    SavedSearch,
    SavedSearchError,
    WatchFilters,
    WatchHit,
    get_watch,
    hits_since,
    list_watches,
    remove_watch,
    set_enabled,
    watch_tables_present,
)
from .saved_search_check import (
    CheckResult,
    Matcher,
    add_watch,
    build_matcher,
    check_enabled,
    check_watch,
    limit_note,
    peek_enabled,
    peek_watch,
)
from .source_kinds import InvalidSourceKind

watch_app = typer.Typer(
    name="watch",
    help=(
        "Saved searches: save a query, and each morning's brief lists the documents "
        "that newly match it."
    ),
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
    """An autocommit connection, so each check is its own transaction; maps the core's errors."""
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        if not watch_tables_present(conn):
            _fail("Saved searches need migration 029: run `brain init`.")
        try:
            yield conn
        except SavedSearchError as exc:
            _fail(str(exc))


def _matcher(cfg: Config) -> Matcher:
    def _embedder() -> Embedder:
        from . import cli as _cli

        embedder: Embedder = _cli._build_embedder(cfg)  # type: ignore[attr-defined]
        return embedder

    return build_matcher(cfg, embedder=_embedder)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}es"


@watch_app.command("add")
def add(
    name: str = typer.Argument(..., help="A short name for the watch."),
    query: str = typer.Argument(..., help="The search query to watch."),
    source: str | None = typer.Option(None, "--source", help="Only this source kind."),
    tag: str | None = typer.Option(None, "--tag", help="Only documents with this tag."),
    person: str | None = typer.Option(
        None, "--person", help="Only documents this person took part in."
    ),
    kind: str | None = typer.Option(
        None, "--kind", help="Only this documents.content_type (transcript, email, note, ...)."
    ),
    without_tag: str | None = typer.Option(
        None, "--without-tag", help="Leave out documents with this tag."
    ),
    semantic: bool = typer.Option(
        False,
        "--semantic",
        help="Match by meaning (needs the embedder) instead of by words (the default).",
    ),
) -> None:
    """Save a search. Its current matches are recorded as already seen."""
    cfg = _config()
    filters = WatchFilters(
        source=source, tag=tag, person=person, kind=kind, without_tag=without_tag
    )
    mode = MODE_SEMANTIC if semantic else MODE_LEXICAL
    with _session(cfg) as conn:
        try:
            watch, seeded = add_watch(
                conn, name=name, query=query, filters=filters, mode=mode, matcher=_matcher(cfg)
            )
        except InvalidSourceKind as exc:
            raise typer.BadParameter(str(exc), param_hint="--source") from exc
        except (PersonNotFound, PersonAmbiguous) as exc:
            raise typer.BadParameter(str(exc), param_hint="--person") from exc
    typer.echo(
        f"Added watch {watch.name!r} ({watch.mode}); "
        f"{_plural(seeded, 'existing match')} recorded as already seen."
    )


def _watch_json(watch: SavedSearch) -> dict[str, Any]:
    return {
        "name": watch.name,
        "query": watch.query,
        "mode": watch.mode,
        "enabled": watch.enabled,
        "filters": watch.filters.to_json(),
        "created_at": watch.created_at.isoformat(),
        "last_checked_at": watch.last_checked_at.isoformat(),
    }


@watch_app.command("list")
def list_cmd(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List every watch."""
    cfg = _config()
    with _session(cfg) as conn:
        watches = list_watches(conn)
    if json_output:
        emit_json([_watch_json(watch) for watch in watches])
        return
    if not watches:
        typer.echo("No watches. Add one with `brain watch add NAME QUERY`.")
        return
    table = Table(title="Watches", show_lines=False)
    for column in ("Name", "Query", "Mode", "State", "Filters", "Last checked"):
        table.add_column(column, overflow="fold")
    for watch in watches:
        table.add_row(
            watch.name,
            watch.query,
            watch.mode,
            "on" if watch.enabled else "paused",
            ", ".join(f"{k}={v}" for k, v in watch.filters.to_json().items()) or "-",
            watch.last_checked_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


def _check_json(result: CheckResult, *, peek: bool) -> dict[str, Any]:
    return {
        "watch": result.watch,
        "mode": result.mode,
        "skipped": result.skipped,
        "peek": peek,
        "unrecorded": result.unrecorded,
        "capped": result.capped,
        "new": [
            {"id": m.document_id, "title": m.title, "source": m.source_kind}
            for m in result.matches
        ],
    }


@watch_app.command("check")
def check(
    name: str | None = typer.Argument(None, help="One watch (default: every enabled watch)."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    peek: bool = typer.Option(
        False, "--peek", help="Report new matches without recording them."
    ),
) -> None:
    """Check watches now and record their new matches (the brief does this every morning)."""
    cfg = _config()
    matcher = _matcher(cfg)
    with _session(cfg) as conn:
        run: Callable[[], list[CheckResult]]
        if peek:
            run = (
                (lambda: [peek_watch(conn, name, matcher)])
                if name is not None
                else (lambda: peek_enabled(conn, matcher))
            )
        else:
            run = (
                (lambda: [check_watch(conn, name, matcher)])
                if name is not None
                else (lambda: check_enabled(conn, matcher))
            )
        results = run()
    if json_output:
        emit_json([_check_json(result, peek=peek) for result in results])
        return
    if not results:
        typer.echo("No enabled watches.")
    for result in results:
        if result.skipped is not None:
            typer.echo(f"{result.watch} ({result.mode}): {result.skipped}")
            continue
        typer.echo(f"{result.watch} ({result.mode}): {len(result.matches)} new")
        for match in result.matches:
            typer.echo(f"  • [{match.source_kind or 'manual'}] {match.title}")
        if (limit := limit_note(result, with_count=True)) is not None:
            typer.echo(f"  {limit}")


def _hit_json(hit: WatchHit) -> dict[str, Any]:
    return {
        "watch": hit.watch,
        "id": hit.document_id,
        "title": hit.title,
        "source": hit.source_kind,
        "first_seen_at": hit.first_seen_at.isoformat(),
        "notified": hit.notified,
    }


@watch_app.command("hits")
def hits(
    name: str | None = typer.Argument(None, help="One watch (default: every watch)."),
    since: str = typer.Option(
        "7d", "--since", help="Window by first seen; a bare number is HOURS. Suffixes: 7d/24h/90m."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Recent hits, by when each was first seen, whether or not a brief has listed them."""
    hours = since_window(since, unit="hours")
    cfg = _config()
    with _session(cfg) as conn:
        if name is not None:
            get_watch(conn, name)
        rows = hits_since(conn, hours=hours, name=name)
    if json_output:
        emit_json([_hit_json(hit) for hit in rows])
        return
    if not rows:
        typer.echo("No hits in that window.")
    for hit in rows:
        mark = "" if hit.notified else "  (new)"
        typer.echo(
            f"[{hit.watch}] {hit.first_seen_at:%Y-%m-%d %H:%M}  {hit.title}{mark}"
        )


@watch_app.command("rm")
def rm(name: str = typer.Argument(..., help="The watch to remove.")) -> None:
    """Remove a watch and its recorded hits."""
    cfg = _config()
    with _session(cfg) as conn:
        remove_watch(conn, name)
    typer.echo(f"Removed watch {name!r}.")


@watch_app.command("pause")
def pause(name: str = typer.Argument(..., help="The watch to pause.")) -> None:
    """Stop checking a watch (its hits are kept)."""
    cfg = _config()
    with _session(cfg) as conn:
        set_enabled(conn, name, enabled=False)
    typer.echo(f"Paused watch {name!r}.")


@watch_app.command("resume")
def resume(name: str = typer.Argument(..., help="The watch to resume.")) -> None:
    """Check a paused watch again."""
    cfg = _config()
    with _session(cfg) as conn:
        set_enabled(conn, name, enabled=True)
    typer.echo(f"Resumed watch {name!r}.")


def register(app: typer.Typer) -> None:
    """Attach the ``watch`` sub-app to the root app."""
    app.add_typer(watch_app, name="watch")
