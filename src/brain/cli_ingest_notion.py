"""`brain ingest-notion` — pages from a Notion "Markdown & CSV" export (RM-35).

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so
``cli.py`` is not touched. :mod:`brain.ingest.notion_archive` lists and reads
the export (a folder or a zip, untrusted) and :mod:`brain.ingest.notion_export`
turns names and text into pages; this module runs the ingest loop and reports.

**Scope (protocol point 5, an accepted deviation).** The export path is the
scope: the owner chose what to export. ``--since`` keeps a page whose
last-edited property is on or after that day; a page without one is kept and
counted. No Notion API is called and nothing needs a credential.

**Dedup (protocol point 3).** Each page is ``(notion, <page id>)`` with
``source_path=None``, so a re-export to a new folder updates in place.

**The cap (protocol point 9, ruling R6).** ``--max`` (default
:data:`DEFAULT_MAX`) counts pages *written*; an unchanged page is free, so
re-runs drain a large workspace. Once the cap is reached the rest are compared
with what is stored, and the number still to write is printed. Every new page
is summarized (``--no-enrich`` skips it) and, with the graph on,
concept-extracted at minutes per page: 200 pages is about 30 hours of graph
lane in the worst case.

**Output (ruling R7).** Pages are named by id, never by title, since a title is
someone else's text; failures are printed by class (``describe_failure``).
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import typer

from .cli_ingest import (
    _ALLOW_SECRETS_HELP,
    _SENSITIVITY_HELP,
    _build_embedder,
    _build_enricher,
    _build_graph_syncer,
    _emit_egress_notice,
    _emit_secret_notice,
    _validate_sensitivity_or_exit,
)
from .config import Config
from .db import connect
from .errors import BrainError, SecretGuardError, describe_failure
from .ingest import Embedder, ingest_document
from .ingest import notion_archive as na
from .ingest import notion_export as ne
from .sensitivity import DEFAULT_SENSITIVITY
from .tags import normalize_tags

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher
    from .graph_rag.sync import GraphSyncer

DEFAULT_MAX = 200
USAGE = "ingest-notion"

#: The reader's caps; tests swap in small ones (``monkeypatch.setattr``).
notion_export_limits = na.ExportLimits()

_SKIP_LINES = (
    (ne.SKIP_ATTACHMENT, "attachment(s) and image(s) skipped"),
    (ne.SKIP_NO_ID, "file(s) without a Notion page id skipped (never guessed)"),
    (ne.SKIP_DUPLICATE_ID, "page(s) whose id occurs more than once skipped"),
    (ne.SKIP_DATABASE_ROW, "database row page(s) skipped (pass --with-databases)"),
    (ne.SKIP_ARCHIVE, "nested archive(s) skipped (unzip it and pass the inner archive)"),
    (na.SKIP_LINK, "link(s) or special file(s) skipped"),
    (na.SKIP_TOO_LARGE, "file(s) over the size cap skipped"),
)


def ingest_notion(
    export_path: Path = typer.Argument(..., help="The Notion export: a folder or a .zip."),
    with_databases: bool = typer.Option(
        False, "--with-databases", help="Also ingest database rows, one document per row."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Pages last edited on or after this day (YYYY-MM-DD)."
    ),
    max_pages: int = typer.Option(
        DEFAULT_MAX, "--max", min=1, help="Write at most this many pages this run."
    ),
    sensitivity: str = typer.Option(DEFAULT_SENSITIVITY, "--sensitivity", help=_SENSITIVITY_HELP),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to each document."),
    enrich: bool = typer.Option(True, "--enrich/--no-enrich", help="Add the Ollama auto-summary."),
    allow_secrets: bool = typer.Option(False, "--allow-secrets", help=_ALLOW_SECRETS_HELP),
) -> None:
    """Ingest the pages of a Notion "Markdown & CSV" export (folder or .zip).

    Re-exporting and re-running updates pages in place (keyed by page id).
    Images and attachments are skipped. At most --max pages are written a run;
    run again to continue.
    """
    first_day = _since_or_exit(since)
    if not (export_path.is_dir() or export_path.suffix.lower() == ".zip"):
        _refuse(f"{USAGE}: the export path must be a folder or a .zip")
    level = _validate_sensitivity_or_exit(sensitivity)
    try:
        with na.open_export(export_path, limits=notion_export_limits) as export:
            plan = ne.plan_export(export.files, with_databases=with_databases)
            _report_plan(plan, export.skipped)
            if with_databases:
                _warn_rowless_databases(export, plan)
            run = _Run.start(
                tags=normalize_tags(list(tag)),
                level=level,
                enrich=enrich,
                allow_secrets=allow_secrets,
                max_pages=max_pages,
                first_day=first_day,
            )
            _ingest_all(export, plan, run)
    except na.NotionExportError as exc:
        typer.secho(f"{USAGE}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc


def _refuse(message: str) -> None:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=2)


def _since_or_exit(since: str | None) -> date | None:
    if since is None:
        return None
    try:
        return date.fromisoformat(since)
    except ValueError:
        _refuse(f"{USAGE}: --since must be YYYY-MM-DD")
    raise AssertionError("unreachable")  # pragma: no cover - _refuse exits


def _report_plan(plan: ne.ExportPlan, listing_skips: dict[str, int]) -> None:
    typer.echo(f"found {len(plan.pages)} page(s)")
    skipped = {**plan.skipped, **listing_skips}
    for key, text in _SKIP_LINES:
        if skipped.get(key):
            typer.echo(f"  {skipped[key]} {text}")
    if plan.databases:
        typer.echo(f"  {len(plan.databases)} database table(s) (CSV) not stored")


def _warn_rowless_databases(export: na.ExportReader, plan: ne.ExportPlan) -> None:
    """Ruling R1: a table with rows but no row pages means the layout changed; say so."""
    for db in plan.databases:
        if db.row_pages:
            continue
        rows = ne.csv_row_count(export.read_text(db.table))
        if rows:
            typer.secho(
                f"database {db.database_id}: its table lists {rows} row(s) but the export "
                "has no row pages, so none were ingested",
                fg="yellow",
                err=True,
            )


@dataclass
class _Run:
    """What every page of one run is ingested with, and the run's counts."""

    cfg: Config
    tags: list[str]
    level: str
    embedder: Embedder
    enricher: OllamaEnricher | None
    graph_syncer: GraphSyncer
    allow_secrets: bool
    max_pages: int
    first_day: date | None
    counts: Counter[str] = field(default_factory=Counter)

    @classmethod
    def start(cls, *, enrich: bool, **kwargs: Any) -> _Run:
        cfg = Config.load()
        return cls(
            cfg=cfg,
            embedder=_build_embedder(cfg),
            enricher=_build_enricher(cfg) if enrich else None,
            graph_syncer=_build_graph_syncer(cfg),
            **kwargs,
        )

    @property
    def written(self) -> int:
        return self.counts["ingested"] + self.counts["updated"]


def _ingest_all(export: na.ExportReader, plan: ne.ExportPlan, run: _Run) -> None:
    with connect(run.cfg.database_url) as conn:
        conn.autocommit = True
        try:
            for ref in plan.pages:
                page = ne.build_page(ref, export.read_text(ref.file))
                if not _in_window(page, run):
                    continue
                if run.written >= run.max_pages:
                    run.counts["remaining"] += _is_due(conn, page)
                    continue
                run.counts[_guarded(conn, page, run)] += 1
        finally:
            _report_run(run)


def _in_window(page: ne.NotionPage, run: _Run) -> bool:
    if run.first_day is None:
        return True
    if page.last_edited is None:
        run.counts["undated"] += 1  # no readable last-edited date: kept, never guessed
        return True
    if page.last_edited.date() < run.first_day:
        run.counts["older"] += 1
        return False
    return True


def _guarded(conn: psycopg.Connection[Any], page: ne.NotionPage, run: _Run) -> str:
    """Ingest one page; a failure is counted and printed, never fatal."""
    try:
        outcome = _write(conn, page, run)
    except SecretGuardError as exc:
        # Per page: one false positive must not abort the run. The guard's own
        # message masks the finding.
        typer.secho(f"  refused {page.page_id}", fg="red", err=True)
        typer.secho(str(exc), fg="red", err=True)
        return "failed"
    except (psycopg.Error, ValueError, BrainError) as exc:
        typer.secho(f"  failed {page.page_id}: {describe_failure(exc)}", fg="red", err=True)
        return "failed"
    if outcome != "unchanged":
        typer.echo(f"  {outcome} {page.page_id}")
    return outcome


def _write(conn: psycopg.Connection[Any], page: ne.NotionPage, run: _Run) -> str:
    result = ingest_document(
        conn,
        embedder=run.embedder,
        doc=page.doc,
        source_kind=ne.SOURCE_KIND,
        source_external_id=page.page_id,
        tags=run.tags,
        vault_root=run.cfg.vault_path,
        enricher=run.enricher,
        enrich=run.enricher is not None,
        enrich_min_tokens=run.cfg.enrich_min_tokens,
        graph_syncer=run.graph_syncer,
        secret_guard=run.cfg.secret_guard,
        allow_secrets=run.allow_secrets,
        sensitivity=run.level,
    )
    _emit_secret_notice(result.secret_notice)
    _emit_egress_notice(result.egress_notice)
    if result.created:
        return "ingested"
    return "updated" if result.body_changed else "unchanged"


def _is_due(conn: psycopg.Connection[Any], page: ne.NotionPage) -> bool:
    """Whether a page past the cap would still be written: new, or its body changed.

    The hash is ``ingest_document``'s (SHA-256 of the body). A body the secret
    guard redacts is stored hashed after redaction, so such a page is counted
    as due every time: the remainder is an upper bound, never an undercount.
    """
    row = conn.execute(
        "SELECT d.content_hash FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE s.kind = %s AND s.external_id = %s",
        (ne.SOURCE_KIND, page.page_id),
    ).fetchone()
    digest = hashlib.sha256(page.doc.content.encode("utf-8")).hexdigest()
    return row is None or row[0] != digest


def _report_run(run: _Run) -> None:
    c = run.counts
    typer.echo(
        f"{c['ingested']} ingested, {c['updated']} updated, {c['unchanged']} unchanged, "
        f"{c['failed']} failed"
    )
    if c["older"]:
        typer.echo(f"{c['older']} page(s) last edited before --since skipped")
    if c["undated"]:
        typer.echo(f"{c['undated']} page(s) without a readable last-edited date kept")
    if c["remaining"]:
        typer.secho(
            f"stopped at --max {run.max_pages}: {c['remaining']} page(s) remain; "
            "run again to continue",
            fg="yellow",
        )


def register(app: typer.Typer) -> None:
    """Attach ``brain ingest-notion`` to ``app`` (through ``cli_registry``)."""
    app.command(name="ingest-notion")(ingest_notion)
