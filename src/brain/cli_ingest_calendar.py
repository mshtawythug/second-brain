"""`brain ingest-calendar` — Google Calendar events as documents (RM-30).

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so
``cli.py`` is not touched. The event-to-document logic lives in the leaf
:mod:`brain.ingest.calendar`; this module validates the scope, runs the ingest
loop and prints the report.

**Scope (protocol point 5).** ``--since`` and ``--until`` are both required, and
no window is ever bulk-imported: it is at most :data:`MAX_WINDOW_DAYS` days, and
``--until`` is at most :data:`MAX_DAYS_AHEAD` days after today (UTC). The limit
on future days exists because the recency boost clamps a future document's age
to zero, so an event far ahead would rank as the newest document there is.

**Re-runs (protocol point 3).** ``ingest_document`` deduplicates by ``(calendar,
<event or series id>)``. An unchanged body is a no-op there; the volatile
fields (a series' occurrences, its latest date, the attendees' addresses) are
then applied with ``update_document(metadata_patch=…)``, which neither
re-chunks nor calls the embedder.

**People.** ``--person`` resolves through ``directory_entries``, so when a
document is created, its body changes, or its addresses change, each
attendee's ``(name, address)`` pair is recorded there with ``source='calendar'``,
as ``directory.refresh_calendar`` does.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import typer

from .cli_ingest import (
    _ALLOW_SECRETS_HELP,
    _build_embedder,
    _build_enricher,
    _build_graph_syncer,
    _emit_egress_notice,
    _emit_secret_notice,
)
from .config import Config
from .db import connect
from .errors import SecretGuardError
from .ingest import Embedder, ingest_document, update_document
from .ingest import calendar as calendar_ingest
from .ingest.calendar import CalendarDoc, CalendarError
from .sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY
from .vault.derived_links import real_gws_runner
from .vault.derived_links.directory import DirectoryStore

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher
    from .graph_rag.sync import GraphSyncer

MAX_WINDOW_DAYS = 92
MAX_DAYS_AHEAD = 14
SCOPE_REFUSAL = (
    "ingest-calendar requires --since and --until "
    "(at most 92 days, ending at most 14 days ahead)"
)
#: Metadata keys that change without the body changing (module docstring).
_VOLATILE_KEYS = ("occurrences", "date", "participants")


def ingest_calendar(
    since: str | None = typer.Option(None, "--since", help="First day, inclusive (YYYY-MM-DD)."),
    until: str | None = typer.Option(None, "--until", help="Last day, exclusive (YYYY-MM-DD)."),
    calendar: str = typer.Option("primary", "--calendar", help="Calendar id."),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to each document."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List titles and dates only."),
    enrich: bool = typer.Option(
        False, "--enrich", help="Add the Ollama auto-summary (off by default: events are short)."
    ),
    allow_secrets: bool = typer.Option(False, "--allow-secrets", help=_ALLOW_SECRETS_HELP),
) -> None:
    """Ingest Google Calendar events via the `gws` CLI: one document per event or series.

    Requires --since and --until: at most 92 days, ending at most 14 days ahead.

    Skips declined, untitled, working-location and out-of-office events.

    Stores private events as confidential.
    """
    window = _window_or_exit(since, until, today=datetime.now(UTC).date())
    try:
        events = calendar_ingest.list_events(calendar, *window, runner=real_gws_runner)
        batch = calendar_ingest.build_documents(
            events,
            master_lookup=lambda series_id: calendar_ingest.get_event(
                calendar, series_id, runner=real_gws_runner
            ),
        )
    except CalendarError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"found {len(events)} event(s): {len(batch.documents)} document(s), "
        f"{batch.skipped} skipped (declined, untitled, cancelled, working location "
        "or out of office)"
    )
    if dry_run:
        for cd in batch.documents:
            typer.echo(f"  {_dry_run_line(cd)}")
        return
    if batch.documents:
        _ingest_all(
            batch.documents,
            calendar=calendar,
            tags=list(tag),
            enrich=enrich,
            allow_secrets=allow_secrets,
        )


def _window_or_exit(
    since: str | None, until: str | None, *, today: date
) -> tuple[datetime, datetime]:
    """``[since, until)`` as UTC datetimes, or exit 2 with the refusal message."""
    if since is None or until is None:
        _refuse(SCOPE_REFUSAL)
    assert since is not None and until is not None  # _refuse exits
    try:
        first, last = date.fromisoformat(since), date.fromisoformat(until)
    except ValueError:
        _refuse(f"{SCOPE_REFUSAL}: dates must be YYYY-MM-DD")
    if last <= first:
        _refuse(f"{SCOPE_REFUSAL}: --until must be after --since")
    if (last - first).days > MAX_WINDOW_DAYS:
        _refuse(f"{SCOPE_REFUSAL}: the window is {(last - first).days} days")
    if last > today + timedelta(days=MAX_DAYS_AHEAD):
        _refuse(f"{SCOPE_REFUSAL}: --until {last} is more than 14 days ahead")
    return (
        datetime.combine(first, time.min, UTC),
        datetime.combine(last, time.min, UTC),
    )


def _refuse(message: str) -> None:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=2)


def _dry_run_line(cd: CalendarDoc) -> str:
    meta = cd.doc.metadata
    occurrences = meta.get("occurrences")
    series = f" (series, {occurrences['count']} occurrence(s))" if occurrences else ""
    return f"{str(meta.get('date', ''))[:10]}  {cd.doc.title}{series}"


def _ingest_all(
    documents: tuple[CalendarDoc, ...],
    *,
    calendar: str,
    tags: list[str],
    enrich: bool,
    allow_secrets: bool,
) -> None:
    cfg = Config.load()
    embedder = _build_embedder(cfg)
    enricher = _build_enricher(cfg) if enrich else None
    graph_syncer = _build_graph_syncer(cfg)
    counts = {"ingested": 0, "updated": 0, "refreshed": 0, "unchanged": 0, "failed": 0}
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for cd in documents:
            try:
                outcome = _ingest_one(
                    conn,
                    cd,
                    cfg=cfg,
                    calendar=calendar,
                    tags=tags,
                    embedder=embedder,
                    enricher=enricher,
                    graph_syncer=graph_syncer,
                    allow_secrets=allow_secrets,
                )
            except SecretGuardError as exc:
                # Per document, like ingest-gmail's per thread: one false positive
                # must not abort the run.
                typer.secho(f"  refused {cd.doc.title}", fg="red", err=True)
                typer.secho(str(exc), fg="red", err=True)
                outcome = "failed"
            except (psycopg.Error, ValueError, KeyError) as exc:
                typer.secho(f"  failed {cd.doc.title}: {exc}", fg="red", err=True)
                outcome = "failed"
            counts[outcome] += 1
            if outcome != "unchanged":
                typer.echo(f"  {outcome} {cd.doc.title}")
    typer.echo(
        f"{counts['ingested']} ingested, {counts['updated']} updated, "
        f"{counts['refreshed']} occurrences refreshed, {counts['unchanged']} unchanged, "
        f"{counts['failed']} failed"
    )


def _ingest_one(
    conn: psycopg.Connection[Any],
    cd: CalendarDoc,
    *,
    cfg: Config,
    calendar: str,
    tags: list[str],
    embedder: Embedder,
    enricher: OllamaEnricher | None,
    graph_syncer: GraphSyncer,
    allow_secrets: bool,
) -> str:
    """Ingest one document; return ``ingested|updated|refreshed|unchanged``."""
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=cd.doc,
        source_kind=calendar_ingest.SOURCE_KIND,
        source_external_id=cd.external_id,
        source_metadata={"calendar_id": calendar},
        tags=tags,
        vault_root=cfg.vault_path,
        enricher=enricher,
        enrich=enricher is not None,
        enrich_min_tokens=cfg.enrich_min_tokens,
        graph_syncer=graph_syncer,
        secret_guard=cfg.secret_guard,
        allow_secrets=allow_secrets,
        sensitivity=CONFIDENTIAL if cd.confidential else DEFAULT_SENSITIVITY,
    )
    _emit_secret_notice(result.secret_notice)
    _emit_egress_notice(result.egress_notice)
    assert result.document_id is not None  # every non-replace path returns the row id
    if result.created or result.body_changed:
        _record_attendees(conn, cd)
        return "ingested" if result.created else "updated"
    patch = _volatile_patch(conn, result.document_id, cd.doc.metadata)
    if not patch:
        return "unchanged"
    # Metadata only: no new_content and no embedder, so no re-chunk and no embed.
    update_document(
        conn, document_id=result.document_id, metadata_patch=patch, vault_root=cfg.vault_path
    )
    if "participants" in patch:
        _record_attendees(conn, cd)
    return "refreshed"


def _volatile_patch(
    conn: psycopg.Connection[Any], document_id: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    row = conn.execute("SELECT metadata FROM documents WHERE id = %s", (document_id,)).fetchone()
    stored: dict[str, Any] = (row[0] if row else None) or {}
    return {
        key: metadata[key]
        for key in _VOLATILE_KEYS
        if key in metadata and stored.get(key) != metadata[key]
    }


def _record_attendees(conn: psycopg.Connection[Any], cd: CalendarDoc) -> None:
    store = DirectoryStore(conn)
    for name, email in cd.attendees:
        try:
            store.upsert_pair(display_name=name, email=email, source="calendar")
        except ValueError:
            continue  # a malformed address: skip it, as refresh_calendar does


def register(app: typer.Typer) -> None:
    """Attach ``brain ingest-calendar`` to ``app`` (through ``cli_registry``)."""
    app.command(name="ingest-calendar")(ingest_calendar)
