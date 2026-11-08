"""``brain tags``: list, rename, merge, suggest-merges (RM-21) and summarize (RM-55).

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

``summarize`` reads both tiers, as ``brain ask`` does, and sends the tag's document summaries to
the chat model at ``OLLAMA_HOST`` (:mod:`brain.tag_summary`). Its database connection is closed
before the model runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, NoReturn

import psycopg
import typer

from .config import Config, ConfigError
from .db import connect
from .errors import describe_failure
from .format import emit_json
from .tag_mapping import (
    MappingReport,
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
from .tag_summary import (
    TAG_SUMMARY_DEFAULT_LIMIT,
    TAG_SUMMARY_FAILURES,
    TAG_SUMMARY_LIMIT_CEILING,
    TagSummary,
    TagSummaryInputError,
    describe_tag_summary_failure,
    fetch_tag_documents,
    resolve_tag,
    synthesize,
)

if TYPE_CHECKING:
    from .ask import ChatJson
    from .ingest import Embedder

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


def echo_missing_file(change: TagChange) -> None:
    """Warn that a changed document's ``vault_path`` is not on disk (row updated, file skipped)."""
    typer.secho(
        f"file missing on disk for {change.doc_id[:8]} ({change.vault_path}); "
        "DB updated, file skipped.",
        fg=typer.colors.YELLOW,
        err=True,
    )


def echo_file_failure(change: TagChange, exc: Exception) -> None:
    """Report a file that could not be rewritten, by exception CLASS only.

    A YAML error's message quotes the offending line of the file, which is document content,
    so only :func:`~brain.errors.describe_failure` is printed. The row is already updated, so a
    re-run will not retry the file: the line says so and gives the tags to set by hand.
    """
    typer.secho(
        f"file rewrite failed for {change.doc_id[:8]} ({change.vault_path}): "
        f"{describe_failure(exc)}; DB updated, file not rewritten. Fix it by hand "
        f"(tags: {change.after}); a re-run will not retry it.",
        fg=typer.colors.RED,
        err=True,
    )


def failed_clause(report: MappingReport) -> str:
    """``", N file(s) failed"`` for a summary line, or nothing when every file was written."""
    return f", {report.files_failed} file(s) failed" if report.files_failed else ""


def exit_if_files_failed(report: MappingReport) -> None:
    """Exit 1 after a run that left any file unwritten; everything else has been done."""
    if report.files_failed:
        raise typer.Exit(code=1)


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

    with _session(cfg) as conn:
        report = run_mapping(
            conn,
            mapping,
            vault_root=cfg.vault_path,
            apply=apply,
            only_mapped=True,
            on_change=_show,
            on_missing_file=echo_missing_file,
            on_file_error=echo_file_failure,
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
        f"{report.files_missing} file-missing skipped{failed_clause(report)}; "
        f"updated {len(watches)} watch filter(s)"
    )
    exit_if_files_failed(report)


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


def _build_chat(cfg: Config) -> ChatJson:
    """The chat callable, via the ``brain.cli`` patch point ``brain ask`` uses."""
    from . import cli as _cli

    return _cli._build_chat(cfg)


def _build_embedder(cfg: Config) -> Embedder:
    """The embedder (for its offline ``count_tokens``), via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_embedder(cfg)  # type: ignore[attr-defined]


def _print_summary(result: TagSummary) -> None:
    """The synthesis, its cited sources, and how much of the tag it read. Plain text, no markup."""
    typer.echo(result.summary or "(no summary)")
    if result.citations:
        typer.echo("\n━ Sources\n")
        for c in result.citations:
            typer.echo(f"[{c.ref}]  {c.title} ({c.document_id[:8]}, {c.date.date().isoformat()})")
    left_out = f", {result.dropped} left out by the input budget" if result.dropped else ""
    typer.secho(f"\n{result.considered} document(s) considered{left_out}", dim=True)


@tags_app.command("summarize")
def summarize(
    tag: str = typer.Argument(..., help="The tag to summarize."),
    limit: int = typer.Option(
        TAG_SUMMARY_DEFAULT_LIMIT,
        "--limit",
        min=1,
        max=TAG_SUMMARY_LIMIT_CEILING,
        help="Read at most this many of the tag's most recent documents.",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit JSON: {tag, summary, citations, considered, dropped}."
    ),
) -> None:
    """A short, cited synthesis of what a tag's most recent documents say.

    Summaries of the tag's documents (confidential ones too) go to the chat model at OLLAMA_HOST.
    """
    try:
        canonical = resolve_tag(tag)
    except TagSummaryInputError as exc:
        _fail(str(exc))
    cfg = _config()
    try:
        with _session(cfg) as conn:
            docs = fetch_tag_documents(conn, canonical, limit=limit, sensitivity=None)
    except psycopg.Error as exc:
        _fail(f"tags summarize: {describe_failure(exc)}")
    # The connection is closed: a model call can take a while, and holds no database resource.
    try:
        result = synthesize(
            canonical, docs, chat=_build_chat(cfg), cfg=cfg, cost=_build_embedder(cfg).count_tokens
        )
    except TAG_SUMMARY_FAILURES as exc:
        _fail(f"tags summarize: {describe_tag_summary_failure(exc)}")
    if json_out:
        emit_json(result.to_json())
    elif not docs:
        typer.secho(f"no documents tagged '{result.tag}'", err=True)
    else:
        _print_summary(result)


def register(app: typer.Typer) -> None:
    """Attach the ``tags`` sub-app to the root app."""
    app.add_typer(tags_app, name="tags")
