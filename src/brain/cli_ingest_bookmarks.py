"""`brain ingest-bookmarks` — posts the owner liked or bookmarked, from a data archive (RM-44).

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so
``cli.py`` is not touched. The file read is :mod:`brain.ingest.social_archive`
(the archive is hostile input), the post logic and document shape
:mod:`brain.ingest.social_posts`; this module checks the scope, runs the ingest
loop and prints the report.

**Scope (protocol point 5, the ticket's accepted deviation).** ``--format
x-archive`` and the archive's top folder are required; the archive is the scope
(the owner chose what to export), narrowed by ``--since`` (the day a post was
WRITTEN, the only date an archive carries for a like) and capped by ``--max``.
A ``.zip`` is refused with advice to unzip it (plan D1): no archive parser is
reachable from here. Nothing is fetched; there is no network code.

**Re-runs (protocol point 3).** A post whose ``(social, x-<id>)`` row exists gets
only its ``collections`` patched, unioned with what is stored
(``refresh_volatile``): no re-chunk, no embed, so an identical re-run changes
nothing. A stored post's body is never rebuilt.

**Sensitivity.** New posts are ``normal`` (the ticket), or ``--sensitivity``. A
stored post follows the escalate-only rule ``ingest-web`` uses
(:mod:`brain.cli_sensitivity_rerun`): an explicit ``confidential`` raises it, an
explicit ``normal`` never lowers it.

**Cost (protocol point 9).** ``social_post`` skips concept extraction, and no
summary is ever made (plan D6): no enricher is built.

**Privacy.** Nothing here logs. Titles reach only the owner's terminal, through
``one_line``; the per-post label is the post URL this command built. Errors are
fixed messages or a class name (``describe_failure``).
"""
from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as day_start
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import psycopg
import typer

from .cli_ingest import (
    _ALLOW_SECRETS_HELP,
    _build_embedder,
    _build_graph_syncer,
    _emit_egress_notice,
    _emit_secret_notice,
    _validate_sensitivity_or_exit,
)
from .cli_sensitivity_rerun import escalate_stored_sensitivity
from .config import Config
from .db import connect
from .errors import SecretGuardError, describe_failure
from .ingest import Embedder, ingest_document
from .ingest import social_posts as sp
from .ingest.social_archive import SocialArchiveError, read_archive
from .ingest.volatile_patch import rare_outcomes, refresh_volatile, settle, stored_document
from .sensitivity import DEFAULT_SENSITIVITY
from .tags import normalize_tags
from .vault.sensitivity_propagate import propagate_sensitivity_to_vault

if TYPE_CHECKING:
    from .graph_rag.sync import GraphSyncer

DEFAULT_MAX = 2000
FORMATS = ("x-archive",)
_COMMAND = "ingest-bookmarks"
SCOPE_REFUSAL = (
    "ingest-bookmarks requires --format x-archive and the archive's top folder "
    "(the one holding data/)"
)
ZIP_REFUSAL = (
    "ingest-bookmarks: unzip the archive first and pass its top folder (the one holding data/)"
)


def ingest_bookmarks(
    archive: Path | None = typer.Argument(
        None, help="The archive's top folder (it holds data/), unzipped."
    ),
    fmt: str | None = typer.Option(
        None, "--format", help="The archive format: x-archive (required)."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only posts WRITTEN on or after this day, YYYY-MM-DD."
    ),
    max_posts: int = typer.Option(
        DEFAULT_MAX, "--max", min=1, help="At most this many posts a run, newest first."
    ),
    sensitivity: str | None = typer.Option(
        None,
        "--sensitivity",
        help="normal (default) or confidential; confidential also raises stored posts.",
    ),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to each post."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be ingested; write nothing."
    ),
    allow_secrets: bool = typer.Option(False, "--allow-secrets", help=_ALLOW_SECRETS_HELP),
) -> None:
    """Ingest the posts you liked or bookmarked, from your own X data archive.

    Reads only data/like.js and data/bookmark.js (and their parts); the payload is
    parsed as JSON, never run. One document per post; nothing is fetched.
    """
    root, first_day = _scope_or_exit(fmt, archive, since, today=datetime.now().date())
    level = _validate_sensitivity_or_exit(sensitivity or DEFAULT_SENSITIVITY)
    try:
        entries = read_archive(root)
    except SocialArchiveError as exc:
        typer.secho(f"{_COMMAND}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    collected = sp.collect_posts(entries.likes, entries.bookmarks, now=datetime.now(UTC))
    window_start = (
        None if first_day is None
        else datetime.combine(first_day, day_start.min).astimezone(UTC)
    )
    batch = sp.select_posts(collected.posts, since=window_start, limit=max_posts)
    _report_read(collected, batch, bookmarks_present=entries.bookmarks_present, limit=max_posts)
    if dry_run:
        for post in batch.posts:
            when = post.written_at.date().isoformat() if post.written_at else "undated"
            typer.echo(f"  {when}  {sp.build_document(post).title}")
        return
    _ingest_all(
        batch,
        _Options(
            tags=normalize_tags(list(tag)),
            sensitivity=level,
            explicit_sensitivity=sensitivity is not None,
            allow_secrets=allow_secrets,
        ),
    )


def _scope_or_exit(
    fmt: str | None, archive: Path | None, since: str | None, *, today: date
) -> tuple[Path, date | None]:
    if fmt is None or archive is None:
        _refuse(SCOPE_REFUSAL)
    if fmt not in FORMATS:
        _refuse(f"{_COMMAND}: --format must be {' or '.join(FORMATS)}")
    if archive.suffix.lower() == ".zip":
        _refuse(ZIP_REFUSAL)
    if since is None:
        return archive.expanduser(), None
    try:
        first = date.fromisoformat(since)
    except ValueError:
        _refuse(f"{_COMMAND}: --since must be YYYY-MM-DD")
    if first > today:
        _refuse(f"{_COMMAND}: --since is in the future")
    return archive.expanduser(), first


def _refuse(message: str) -> NoReturn:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=2)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _report_read(
    collected: sp.Collected, batch: sp.PostBatch, *, bookmarks_present: bool, limit: int
) -> None:
    typer.echo(
        f"found {_plural(len(collected.posts), 'post', 'posts')} "
        f"({collected.liked} liked, {collected.bookmarked} bookmarked)"
    )
    if not bookmarks_present:
        typer.echo("bookmarks file not present")
    notes = (
        (collected.no_text, "entry without text skipped", "entries without text skipped"),
        (collected.malformed, "malformed entry skipped", "malformed entries skipped"),
        (batch.before_since, "post before --since skipped", "posts before --since skipped"),
        (batch.undated, "undated post skipped (--since needs a date)",
         "undated posts skipped (--since needs a date)"),
    )
    for count, one, many in notes:
        if count:
            typer.echo(_plural(count, one, many))
    if batch.remainder:
        more = _plural(batch.remainder, "more post qualifies", "more posts qualify")
        typer.secho(
            f"stopped at the --max cap of {limit}: {more}; raise --max or narrow --since",
            fg="yellow",
        )


@dataclass(frozen=True)
class _Options:
    tags: list[str]
    sensitivity: str
    explicit_sensitivity: bool
    allow_secrets: bool


@dataclass
class _Run:
    """What every post of one run is written with, and the run's counts."""

    options: _Options
    cfg: Config
    embedder: Embedder
    graph_syncer: GraphSyncer
    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("ingested", "updated", "refreshed", "unchanged", "empty", "same_body", "failed"), 0
        )
    )


def _ingest_all(batch: sp.PostBatch, options: _Options) -> None:
    cfg = Config.load()
    run = _Run(
        options=options,
        cfg=cfg,
        embedder=_build_embedder(cfg),
        graph_syncer=_build_graph_syncer(cfg),
    )
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for post in batch.posts:
            _guarded(post, run, functools.partial(_write_post, conn, post, run))
    counts = run.counts
    typer.echo(
        f"{counts['ingested']} ingested, {counts['updated']} updated, "
        f"{counts['refreshed']} refreshed, {counts['unchanged']} unchanged, "
        f"{counts['failed']} failed{rare_outcomes(counts, 'post')}"
    )


def _guarded(post: sp.SocialPost, run: _Run, step: Callable[[], str]) -> None:
    """Write one post; a failure is counted and printed, never fatal."""
    label = post.url
    try:
        outcome = step()
    except SecretGuardError as exc:
        # Per post, like ingest-web's per page. The guard's message masks the finding.
        typer.secho(f"  refused {label}", fg="red", err=True)
        typer.secho(str(exc), fg="red", err=True)
        outcome = "failed"
    except (psycopg.Error, ValueError, KeyError) as exc:
        typer.secho(f"  failed {label}: {describe_failure(exc)}", fg="red", err=True)
        outcome = "failed"
    run.counts[outcome] += 1
    if outcome in ("ingested", "updated", "refreshed"):
        typer.echo(f"  {outcome} {label}")


def _write_post(conn: psycopg.Connection[Any], post: sp.SocialPost, run: _Run) -> str:
    """``ingested|updated|refreshed|unchanged|empty|same_body`` for one post."""
    options = run.options
    stored = stored_document(conn, sp.SOURCE_KIND, post.external_id)
    if stored is not None:
        outcome = refresh_volatile(
            conn,
            document_id=stored[0],
            stored=stored[1],
            metadata=sp.volatile_metadata(post, stored[1]),
            keys=sp.VOLATILE_KEYS,
            vault_root=run.cfg.vault_path,
            graph_syncer=run.graph_syncer,
        )
        raised = escalate_stored_sensitivity(
            conn,
            stored[0],
            level=options.sensitivity,
            explicit=options.explicit_sensitivity,
            vault_root=run.cfg.vault_path,
            label=post.url,
            propagate=propagate_sensitivity_to_vault,
        )
        return "refreshed" if raised else outcome
    doc = sp.build_document(post)
    result = ingest_document(
        conn,
        embedder=run.embedder,
        doc=doc,
        source_kind=sp.SOURCE_KIND,
        source_external_id=post.external_id,
        source_metadata={"platform": sp.PLATFORM, "url": post.url},
        tags=options.tags,
        vault_root=run.cfg.vault_path,
        enricher=None,
        enrich=False,
        graph_syncer=run.graph_syncer,
        secret_guard=run.cfg.secret_guard,
        allow_secrets=options.allow_secrets,
        sensitivity=options.sensitivity,
    )
    _emit_secret_notice(result.secret_notice)
    _emit_egress_notice(result.egress_notice)
    return settle(
        conn,
        result,
        stored=None,
        metadata=doc.metadata,
        keys=sp.VOLATILE_KEYS,
        vault_root=run.cfg.vault_path,
        graph_syncer=run.graph_syncer,
    )


def register(app: typer.Typer) -> None:
    """Attach ``brain ingest-bookmarks`` to ``app`` (through ``cli_registry``)."""
    app.command(name="ingest-bookmarks")(ingest_bookmarks)
