"""`brain ingest-apple-notes` — the notes in the Apple Notes folders the owner names (RM-36).

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so
``cli.py`` is not touched. The ``osascript`` read and the document shape live in
:mod:`brain.ingest.apple_notes`; this module checks the scope and the host, runs
the ingest loop and prints the report.

**Scope (protocol point 5).** ``--folder NAME`` (repeatable) is required; "all
notes" is not offered. ``--since YYYY-MM-DD`` optionally keeps notes modified on
or after that day (local time).

**The host.** Notes is read through ``osascript``, a macOS built-in, so any
other platform exits 1 before anything runs. :data:`host_platform` is the seam
the tests swap, so the suite runs on Linux CI.

**The cap (protocol point 9).** At most ``--max`` notes a run (default
:data:`DEFAULT_MAX`), newest modification first; the rest are counted and
reported. Every new note is summarized (``--no-enrich`` skips it) and, with the
graph on, concept-extracted at minutes per note.

**Re-runs (protocol point 3).** ``ingest_document`` deduplicates by
``(applenotes, <note id>)``: an edited note updates in place, and an unchanged
body is a no-op there; a moved note or a new modification time is applied with
``update_document(metadata_patch=…)``, which neither re-chunks nor embeds.

**Sensitivity.** ``confidential`` unless ``--sensitivity normal`` (RM-36.D2):
personal notes are private by nature. Locked notes are never read; they are
counted. Attachments are not ingested.

**Errors.** An :class:`~brain.ingest.apple_notes.AppleNotesError` message quotes
no ``osascript`` output, so it is printed as it is; any other failure is printed
by class only (``describe_failure``).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime
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
    _validate_sensitivity_or_exit,
)
from .config import Config
from .db import connect
from .errors import SecretGuardError, describe_failure
from .ingest import Embedder, ExtractedDoc, ingest_document
from .ingest import apple_notes as an
from .ingest.apple_notes import AppleNotesError, NotesBatch, NotesRequest
from .ingest.text_safety import one_line
from .ingest.volatile_patch import refresh_volatile, stored_document
from .sensitivity import CONFIDENTIAL
from .tags import normalize_tags

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher
    from .graph_rag.sync import GraphSyncer

DEFAULT_MAX = 200
SCOPE_REFUSAL = 'ingest-apple-notes requires --folder NAME (repeatable); "all notes" is not offered'
_COMMAND = "ingest-apple-notes"
NOT_MACOS = "ingest-apple-notes needs macOS: Apple Notes is read through osascript"

#: The production runner; tests swap it for a fake (``monkeypatch.setattr``).
apple_notes_runner: an.NotesRunner = an.osascript_runner
#: The platform the command checks; tests swap it so the suite runs on any host.
host_platform: str = sys.platform


def ingest_apple_notes(
    folder: list[str] = typer.Option(
        [], "--folder", help="A Notes folder to read, by exact name (repeatable; required)."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only notes modified on or after this day (YYYY-MM-DD)."
    ),
    max_notes: int = typer.Option(
        DEFAULT_MAX, "--max", min=1, help="At most this many notes a run, newest first."
    ),
    sensitivity: str = typer.Option(
        CONFIDENTIAL, "--sensitivity", help="confidential (default) or normal."
    ),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to each note."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be ingested; write nothing."
    ),
    enrich: bool = typer.Option(True, "--enrich/--no-enrich", help="Add the Ollama auto-summary."),
    allow_secrets: bool = typer.Option(False, "--allow-secrets", help=_ALLOW_SECRETS_HELP),
) -> None:
    """Ingest the notes of named Apple Notes folders (macOS, via osascript).

    Requires --folder. Stores notes as confidential unless --sensitivity normal.
    Skips locked notes (counted) and attachments. The first run triggers macOS's
    Automation permission prompt for Notes.
    """
    request = _request_or_exit(folder, since, max_notes, today=datetime.now().date())
    level = _validate_sensitivity_or_exit(sensitivity)
    if host_platform != "darwin":
        typer.secho(NOT_MACOS, fg="red", err=True)
        raise typer.Exit(code=1)
    try:
        batch = an.read_notes(request, runner=apple_notes_runner)
    except AppleNotesError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    _report_read(batch, request)
    if dry_run:
        for note in batch.notes:
            typer.echo(f"  {one_line(note.folder)} / {one_line(note.name) or an.UNTITLED}")
        return
    _ingest_all(
        batch,
        # Tags are canonical at every write boundary; ingest_document
        # normalizes only when it merges into an existing row.
        tags=normalize_tags(list(tag)),
        sensitivity=level,
        enrich=enrich,
        allow_secrets=allow_secrets,
    )


def _request_or_exit(
    folders: list[str], since: str | None, limit: int, *, today: date
) -> NotesRequest:
    names = tuple(dict.fromkeys(folders))
    if not names:
        _refuse(SCOPE_REFUSAL)
    if any(not name.strip() for name in names):
        _refuse(f"{_COMMAND}: --folder must not be blank")
    first: date | None = None
    if since is not None:
        try:
            first = date.fromisoformat(since)
        except ValueError:
            _refuse(f"{_COMMAND}: --since must be YYYY-MM-DD")
        assert first is not None  # _refuse exits
        if first > today:
            _refuse(f"{_COMMAND}: --since is in the future")
    return NotesRequest(folders=names, since=first, limit=limit)


def _refuse(message: str) -> None:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=2)


def _report_read(batch: NotesBatch, request: NotesRequest) -> None:
    """Print what the read found; exit 1 when no named folder exists."""
    for name in batch.missing:
        typer.secho(f"no Notes folder named {one_line(name)!r}", fg="yellow", err=True)
    if len(batch.missing) == len(request.folders):
        typer.secho("none of the named folders exist in Notes", fg="red", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"found {len(batch.notes)} note(s)")
    if batch.locked:
        typer.echo(f"{batch.locked} locked note(s) skipped (locked notes cannot be read)")
    if batch.malformed:
        typer.secho(f"{batch.malformed} note(s) could not be read", fg="yellow", err=True)
    if batch.remainder:
        typer.secho(
            f"stopped at the --max cap of {request.limit}: {batch.remainder} more note(s) "
            "match; raise --max or narrow --since",
            fg="yellow",
        )


@dataclass
class _Run:
    """What every note of one run is ingested with, and the run's counts."""

    cfg: Config
    tags: list[str]
    sensitivity: str
    embedder: Embedder
    enricher: OllamaEnricher | None
    graph_syncer: GraphSyncer
    allow_secrets: bool
    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("ingested", "updated", "refreshed", "unchanged", "empty", "skipped", "failed"), 0
        )
    )


def _ingest_all(
    batch: NotesBatch, *, tags: list[str], sensitivity: str, enrich: bool, allow_secrets: bool
) -> None:
    cfg = Config.load()
    run = _Run(
        cfg=cfg,
        tags=tags,
        sensitivity=sensitivity,
        embedder=_build_embedder(cfg),
        enricher=_build_enricher(cfg) if enrich else None,
        graph_syncer=_build_graph_syncer(cfg),
        allow_secrets=allow_secrets,
    )
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for note in batch.notes:
            # The conversion runs inside the guard too: a body is the note author's
            # HTML, and whatever it raises must cost one note, not the run (#103).
            _guarded(note.id, run, lambda n=note: _ingest_note(conn, n, run))
    c = run.counts
    typer.echo(
        f"{c['ingested']} ingested, {c['updated']} updated, {c['refreshed']} refreshed, "
        f"{c['unchanged']} unchanged, {c['empty']} empty, {c['skipped']} skipped, "
        f"{c['failed']} failed"
    )


def _guarded(note_id: str, run: _Run, step: Any) -> None:
    """Run one note's ingest; a failure is counted and printed, never fatal."""
    try:
        outcome = step()
    except SecretGuardError as exc:
        # Per note, like ingest-gmail's per thread: one false positive must not
        # abort the run. The guard's own message masks the finding.
        typer.secho(f"  refused {note_id}", fg="red", err=True)
        typer.secho(str(exc), fg="red", err=True)
        outcome = "failed"
    except (psycopg.Error, ValueError, KeyError) as exc:
        typer.secho(f"  failed {note_id}: {describe_failure(exc)}", fg="red", err=True)
        outcome = "failed"
    run.counts[outcome] += 1
    if outcome not in ("unchanged", "empty", "skipped", "failed"):
        typer.echo(f"  {outcome} {note_id}")


def _ingest_note(conn: psycopg.Connection[Any], note: an.Note, run: _Run) -> str:
    """Convert and write one note; ``empty`` or ``skipped`` when there is nothing to write."""
    if len(note.body_html) > an.MAX_BODY_CHARS:
        typer.secho(
            f"  skipped {note.id}: body over {an.MAX_BODY_CHARS} characters",
            fg="yellow",
            err=True,
        )
        return "skipped"
    doc = an.build_document(note)
    if doc is None:
        return "empty"
    return _write(conn, note.id, doc, run)


def _write(conn: psycopg.Connection[Any], note_id: str, doc: ExtractedDoc, run: _Run) -> str:
    """Ingest one note; return ``ingested|updated|refreshed|unchanged``."""
    stored = stored_document(conn, an.SOURCE_KIND, note_id)
    result = ingest_document(
        conn,
        embedder=run.embedder,
        doc=doc,
        source_kind=an.SOURCE_KIND,
        source_external_id=note_id,
        source_metadata={"folder": doc.metadata["folder"]},
        tags=run.tags,
        vault_root=run.cfg.vault_path,
        enricher=run.enricher,
        enrich=run.enricher is not None,
        enrich_min_tokens=run.cfg.enrich_min_tokens,
        graph_syncer=run.graph_syncer,
        secret_guard=run.cfg.secret_guard,
        allow_secrets=run.allow_secrets,
        sensitivity=run.sensitivity,
    )
    _emit_secret_notice(result.secret_notice)
    _emit_egress_notice(result.egress_notice)
    if result.created or result.body_changed:
        return "ingested" if result.created else "updated"
    assert result.document_id is not None  # every non-replace path returns the row id
    return refresh_volatile(
        conn,
        document_id=result.document_id,
        stored=stored[1] if stored is not None else {},
        metadata=doc.metadata,
        keys=an.VOLATILE_KEYS,
        vault_root=run.cfg.vault_path,
        graph_syncer=run.graph_syncer,
    )


def register(app: typer.Typer) -> None:
    """Attach ``brain ingest-apple-notes`` to ``app`` (through ``cli_registry``)."""
    app.command(name="ingest-apple-notes")(ingest_apple_notes)
