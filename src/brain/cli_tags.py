"""``brain tags``: list, rename, merge and suggest-merges (RM-21).

Thin Typer orchestration over :mod:`brain.tag_mapping`, the engine ``brain backfill
normalize-tags`` also runs. ``rename`` and ``merge`` build a ``{old: new}`` mapping and run the
same per-document, idempotent loop on an autocommit connection: the ``documents.tags`` row, its
chunks' ``tags_text``, and the frontmatter of the file at ``vault_path``. Saved searches whose
tag filters name a mapped tag are rewritten too, so a watch keeps matching.

**Dry run by default.** Without ``--apply`` the commands print the mapping and every document and
watch they would change, and write nothing. A merge into an existing tag cannot be undone without
a mapping back, which is why the default is the dry run and the mapping is always printed.

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so ``cli.py`` is
not edited for it. The terminal is inside the F6 trust boundary, so ``list`` and
``suggest-merges`` count both tiers. The MCP twins (:mod:`brain.mcp_tools.tags`) are read-only
and lens-filtered.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NoReturn

import psycopg
import typer

from .config import Config, ConfigError
from .db import connect
from .format import emit_json
from .tag_mapping import (
    TagChange,
    TagMappingError,
    build_merge_mapping,
    build_rename_mapping,
    read_document_tags,
    rewrite_watch_filters,
    run_mapping,
    suggest_merges,
    tag_counts,
)

tags_app = typer.Typer(
    name="tags",
    help="Tag maintenance: list tags, rename or merge them everywhere, and suggest merges.",
    no_args_is_help=True,
)

#: How many suggestions ``suggest-merges`` prints unless ``--limit`` says otherwise.
DEFAULT_SUGGESTION_LIMIT = 50

_APPLY_HELP = "Write the changes. Without it, print what would change and write nothing."


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
    """An autocommit connection, so each document's writes commit on their own (RM-21.D2)."""
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        yield conn


def _run_rewrite(mapping: dict[str, str], *, apply: bool) -> None:
    """Print the mapping, then rewrite (or plan) every document and watch it touches."""
    cfg = _config()
    for old, new in mapping.items():
        typer.echo(f"mapping: {old} → {new}")

    def _show(change: TagChange) -> None:
        typer.echo(f"{change.doc_id[:8]}  {change.title}  {change.before} → {change.after}")

    def _missing(change: TagChange) -> None:
        typer.secho(
            f"file missing on disk for {change.doc_id[:8]} ({change.vault_path}); "
            "DB updated, file skipped.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    with _session(cfg) as conn:
        report = run_mapping(
            conn,
            mapping,
            vault_root=cfg.vault_path,
            apply=apply,
            only_mapped=True,
            on_change=_show,
            on_missing_file=_missing,
        )
        watches = rewrite_watch_filters(conn, mapping, apply=apply)
    for w in watches:
        typer.echo(f"watch {w.watch!r}: {w.key} {w.before} → {w.after}")
    if not apply:
        typer.echo(
            f"would retag {report.changed} doc(s) and {len(watches)} watch filter(s); "
            "nothing written. Re-run with --apply to write."
        )
        return
    typer.echo(
        f"retagged {report.changed} doc(s), rewrote {report.files_rewritten} file(s), "
        f"{report.files_missing} file-missing skipped; "
        f"updated {len(watches)} watch filter(s)"
    )


@tags_app.command("list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit JSON: [{tag, count}]."),
) -> None:
    """Every tag and how many documents carry it, most used first."""
    cfg = _config()
    with _session(cfg) as conn:
        counts = tag_counts(conn)
    if json_out:
        emit_json([{"tag": tag, "count": count} for tag, count in counts])
        return
    width = len(str(counts[0][1])) if counts else 1
    for tag, count in counts:
        typer.echo(f"{count:>{width}}  {tag}")


@tags_app.command("rename")
def rename(
    old: str = typer.Argument(..., help="The tag to rename."),
    new: str = typer.Argument(..., help="Its new name (may be an existing tag)."),
    apply: bool = typer.Option(False, "--apply", help=_APPLY_HELP),
) -> None:
    """Rename a tag on every document, chunk, vault file and saved search."""
    try:
        mapping = build_rename_mapping(old, new)
    except TagMappingError as exc:
        _fail(str(exc))
    _run_rewrite(mapping, apply=apply)


@tags_app.command("merge")
def merge(
    sources: list[str] = typer.Argument(..., help="The tags to merge."),
    into: str = typer.Option(..., "--into", help="The tag they become."),
    apply: bool = typer.Option(False, "--apply", help=_APPLY_HELP),
) -> None:
    """Merge tags into one; a document keeps one copy, in first-seen order."""
    try:
        mapping = build_merge_mapping(sources, into)
    except TagMappingError as exc:
        _fail(str(exc))
    _run_rewrite(mapping, apply=apply)


@tags_app.command("suggest-merges")
def suggest_merges_cmd(
    limit: int = typer.Option(
        DEFAULT_SUGGESTION_LIMIT, "--limit", min=1, help="Print at most this many."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON with each pair's evidence."),
) -> None:
    """Pairs of tags that are probably one tag: close spelling, singular/plural, co-occurrence."""
    cfg = _config()
    with _session(cfg) as conn:
        suggestions = suggest_merges(read_document_tags(conn), limit=limit)
    if json_out:
        emit_json([s.to_json() for s in suggestions])
        return
    if not suggestions:
        typer.echo("no merge suggestions")
        return
    for s in suggestions:
        typer.echo(
            f"{s.keep} ({s.keep_count}) ← {s.merge} ({s.merge_count})  "
            f"{', '.join(s.reasons)}; ratio {s.ratio:.2f}, "
            f"overlap {s.overlap:.2f} ({s.shared} shared)"
        )
        typer.echo(f"  brain tags merge {s.merge} --into {s.keep}")


def register(app: typer.Typer) -> None:
    """Attach the ``tags`` sub-app to the root app."""
    app.add_typer(tags_app, name="tags")
