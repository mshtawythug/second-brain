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

**Errors are printed by class only** (``describe_failure``): a ``gws`` failure
can quote its stderr, and a database error's DETAIL can quote row values.

**The ``gws`` read** gets :data:`GWS_TIMEOUT_SECONDS` and a
:data:`GWS_MAX_STDOUT_BYTES` cap (:func:`calendar_gws_runner`), because a
``--page-all`` read over 92 days is far larger than the single page the
runner's 30-second default was sized for.

**Series are merged, never moved backwards** (``calendar.merge_volatile``): a
run over an earlier window keeps the later ``date`` and the latest ten
occurrences. A series whose master could not be read keeps its stored body.

**People.** ``--person`` resolves through ``directory_entries``, so when a
document is created, its body changes, or its addresses change, each
attendee's ``(name, address)`` pair is recorded there with ``source='calendar'``,
as ``directory.refresh_calendar`` does.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
from .errors import SecretGuardError, describe_failure
from .ingest import Embedder, ingest_document
from .ingest import calendar as calendar_ingest
from .ingest.calendar import CalendarDoc, CalendarError
from .ingest.volatile_patch import (
    rare_outcomes,
    refresh_volatile,
    settle,
    stored_document,
)
from .sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY
from .tags import normalize_tags
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
#: One ``--page-all`` read of up to 92 days of events (module docstring).
GWS_TIMEOUT_SECONDS = 120
GWS_MAX_STDOUT_BYTES = 16 * 1024 * 1024


def calendar_gws_runner(args: list[str]) -> str:
    """The production ``gws`` runner, with this command's timeout and stdout cap."""
    return real_gws_runner(
        args, timeout=GWS_TIMEOUT_SECONDS, max_stdout_bytes=GWS_MAX_STDOUT_BYTES
    )


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
        events = calendar_ingest.list_events(calendar, *window, runner=calendar_gws_runner)
        batch = calendar_ingest.build_documents(
            events,
            master_lookup=lambda series_id: calendar_ingest.get_event(
                calendar, series_id, runner=calendar_gws_runner
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
    if batch.fallbacks:
        typer.echo(
            f"{batch.fallbacks} series master(s) could not be read: a stored series keeps "
            "its body and gets only its dates refreshed"
        )
    if dry_run:
        for cd in batch.documents:
            typer.echo(f"  {_dry_run_line(cd)}")
        return
    if batch.documents:
        _ingest_all(
            batch.documents,
            calendar=calendar,
            window=window,
            # Tags are canonical at every write boundary; ingest_document
            # normalizes only when it merges into an existing row.
            tags=normalize_tags(list(tag)),
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
    series = ""
    if occurrences:
        series = f" (series, {occurrences['run_window_count']} occurrence(s) in this window)"
    return f"{str(meta.get('date', ''))[:10]}  {cd.doc.title}{series}"


@dataclass(frozen=True)
class _Run:
    """What every document of one run is ingested with."""

    cfg: Config
    calendar: str
    tags: list[str]
    window: tuple[datetime, datetime]
    embedder: Embedder
    enricher: OllamaEnricher | None
    graph_syncer: GraphSyncer
    allow_secrets: bool


def _ingest_all(
    documents: tuple[CalendarDoc, ...],
    *,
    calendar: str,
    tags: list[str],
    window: tuple[datetime, datetime],
    enrich: bool,
    allow_secrets: bool,
) -> None:
    cfg = Config.load()
    run = _Run(
        cfg=cfg,
        calendar=calendar,
        tags=tags,
        window=window,
        embedder=_build_embedder(cfg),
        enricher=_build_enricher(cfg) if enrich else None,
        graph_syncer=_build_graph_syncer(cfg),
        allow_secrets=allow_secrets,
    )
    counts = dict.fromkeys(
        ("ingested", "updated", "refreshed", "unchanged", "empty", "same_body", "failed"), 0
    )
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for cd in documents:
            try:
                outcome = _ingest_one(conn, cd, run)
            except SecretGuardError as exc:
                # Per document, like ingest-gmail's per thread: one false positive
                # must not abort the run.
                typer.secho(f"  refused {cd.doc.title}", fg="red", err=True)
                typer.secho(str(exc), fg="red", err=True)
                outcome = "failed"
            except (psycopg.Error, ValueError, KeyError) as exc:
                typer.secho(
                    f"  failed {cd.doc.title}: {describe_failure(exc)}", fg="red", err=True
                )
                outcome = "failed"
            counts[outcome] += 1
            if outcome == "same_body":
                typer.secho(
                    f"  same body as another event: {cd.external_id}", fg="yellow", err=True
                )
            elif outcome == "empty":
                typer.echo(f"  empty {cd.external_id}")
            elif outcome != "unchanged":
                typer.echo(f"  {outcome} {cd.doc.title}")
    typer.echo(
        f"{counts['ingested']} ingested, {counts['updated']} updated, "
        f"{counts['refreshed']} occurrences refreshed, {counts['unchanged']} unchanged, "
        f"{counts['failed']} failed{rare_outcomes(counts, 'event')}"
    )


def _ingest_one(conn: psycopg.Connection[Any], cd: CalendarDoc, run: _Run) -> str:
    """Ingest one document; ``ingested|updated|refreshed|unchanged|empty|same_body``."""
    stored = stored_document(conn, calendar_ingest.SOURCE_KIND, cd.external_id)
    stored_meta = stored[1] if stored is not None else {}
    # An earlier window must not move a series backwards, and an occurrence the
    # window no longer holds is dropped (calendar.merge_volatile).
    metadata = calendar_ingest.merge_volatile(stored_meta, cd.doc.metadata, window=run.window)
    # Each attendee is recorded at most once per run: ``upsert_pair`` bumps an
    # occurrence count, and the directory resolves a name by the summed count.
    recorded = stored is None or stored_meta.get("participants") != metadata.get("participants")
    if recorded:
        # Before the write, so the graph reconcile it triggers resolves the
        # attendees through the directory.
        _record_attendees(conn, cd)
    if stored is not None and cd.body_from_fallback and not cd.confidential:
        # The master could not be read, so this body came from an instance and
        # would rewrite (re-chunk, re-embed) a body built from the master. Keep
        # the stored body; refresh only the metadata. A confidential one still
        # goes through ingest_document: its escalate-only sensitivity update
        # (and the hosted-egress veto) matters more than the churn.
        return _refresh_metadata(conn, stored[0], stored_meta, metadata, run)
    result = ingest_document(
        conn,
        embedder=run.embedder,
        doc=replace(cd.doc, metadata=metadata),
        source_kind=calendar_ingest.SOURCE_KIND,
        source_external_id=cd.external_id,
        source_metadata={"calendar_id": run.calendar},
        tags=run.tags,
        vault_root=run.cfg.vault_path,
        enricher=run.enricher,
        enrich=run.enricher is not None,
        enrich_min_tokens=run.cfg.enrich_min_tokens,
        graph_syncer=run.graph_syncer,
        secret_guard=run.cfg.secret_guard,
        allow_secrets=run.allow_secrets,
        sensitivity=CONFIDENTIAL if cd.confidential else DEFAULT_SENSITIVITY,
    )
    _emit_secret_notice(result.secret_notice)
    _emit_egress_notice(result.egress_notice)
    # One rule for every sourced CLI (volatile_patch.settle, BL-34): only this
    # event's own row is refreshed; a returned row that is another event's
    # (identical text: one meeting on two calendars, a duplicated invite) is
    # reported as same_body and never patched.
    outcome = settle(
        conn,
        result,
        stored=stored,
        metadata=metadata,
        keys=_VOLATILE_KEYS,
        vault_root=run.cfg.vault_path,
        graph_syncer=run.graph_syncer,
    )
    if outcome in ("ingested", "updated") and not recorded:
        _record_attendees(conn, cd)  # a changed body can mean changed names
    return outcome


def _refresh_metadata(
    conn: psycopg.Connection[Any],
    document_id: str,
    stored: dict[str, Any],
    metadata: dict[str, Any],
    run: _Run,
) -> str:
    """Apply the volatile keys that differ from ``stored``; ``refreshed`` or ``unchanged``.

    The graph syncer goes along, so a participants change re-indexes the people.
    """
    return refresh_volatile(
        conn,
        document_id=document_id,
        stored=stored,
        metadata=metadata,
        keys=_VOLATILE_KEYS,
        vault_root=run.cfg.vault_path,
        graph_syncer=run.graph_syncer,
    )


def _record_attendees(conn: psycopg.Connection[Any], cd: CalendarDoc) -> None:
    store = DirectoryStore(conn)
    # ``calendar._attendees`` keeps only addresses ``is_email_like`` accepts, so
    # ``upsert_pair``'s empty-address ValueError cannot happen here.
    for name, email in cd.attendees:
        store.upsert_pair(display_name=name, email=email, source="calendar")


def register(app: typer.Typer) -> None:
    """Attach ``brain ingest-calendar`` to ``app`` (through ``cli_registry``)."""
    app.command(name="ingest-calendar")(ingest_calendar)
