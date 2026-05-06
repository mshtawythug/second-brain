"""brain — second brain CLI."""
import json as _json  # aliased — `json` conflicts with the --json output flag name
import logging
import shutil
import subprocess
import sys
import time as _time
import uuid
from datetime import UTC, datetime
from datetime import date as date_cls
from pathlib import Path
from typing import Any

import httpx
import psycopg
import typer
import yaml
from rich.table import Table

from .backfill import backfill_search, backfill_source_rows
from .config import Config, ConfigError
from .db import connect, connect_raw, ensure_embedding_column, run_migrations
from .edit_session import (
    EditorAbortedError,
    EditorError,
    EditorParseFailedError,
    EditorUnchangedError,
    build_payload,
    run_editor_session,
)
from .editor import EditorError as RawEditorError
from .editor import run_editor_on
from .embeddings import make_embedder
from .errors import (
    DraftSkipped,
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
)
from .format import console, emit_json, search_table
from .ingest import (
    Embedder,
    UpdateResult,
    apply_tags,
    extract_path,
    ingest_document,
    supported_extensions,
    update_document,
)
from .ingest import gmail as gmail_ingest
from .ingest.gmail import GmailError
from .ingest.stdin import make_doc as _stdin_make_doc
from .queries import (
    MirrorDriftSummary,
    count_chunks_missing_embedding,
    embedding_column_state,
    fetch_document,
    finalize_embedding_index,
    iter_chunks_missing_embedding,
    iter_orphan_mirror_files,
    iter_stale_mirror_files,
    list_documents,
    mirror_drift_summary,
    resolve_document_prefix,
    summary_counts,
    sync_chunk_search_metadata,
)
from .search import hybrid_search
from .tags import normalize_tag, normalize_tags
from .vault import init_vault
from .vault.daily_index import regenerate_daily_index
from .vault.derived_links import (
    DirectoryStore,
    extract_krisp_speakers,
    real_gws_runner,
    rebuild_derived_for,
    refresh_calendar,
    refresh_contacts,
    rescan_gmail_directory,
)
from .vault.derived_links.fence import rewrite_derived_fences
from .vault.export import export_vault, regenerate_vault_file
from .vault.frontmatter import dump_frontmatter, parse_frontmatter, rewrite_tags
from .vault.graph import (
    backlinks_for,
    graph_data,
    outgoing_links_for,
)
from .vault.graph import orphans as _orphans_query
from .vault.graph_format import to_dot, to_json, to_mermaid
from .vault.quartz_overlay import OverlayError, apply_overlay, plan_overlay
from .vault.quartz_overlay import repo_root as _brain_repo_root
from .vault.rename import RenameError, RenameOp, apply_rename, plan_rename
from .vault.slug import slugify
from .vault.sync import SyncReport, sync_one_file, sync_vault
from .vault.templates import list_template_names, render_template
from .vault.watch import WatchConfig, run_watcher

logger = logging.getLogger(__name__)

_KRISP_INGEST_HELP = (
    "Importing Krisp calls — Krisp has no CLI, so transcripts are pulled by "
    "Claude via the Krisp MCP (mcp__claude_ai_Krisp__search_meetings) and "
    "piped into `brain ingest-stdin`. From any Claude conversation, ask e.g. "
    '"ingest last week\'s Krisp calls" and Claude will fetch each transcript '
    "and pipe it in with --source krisp, --external-id <meeting_id>, --title, "
    "--content-type transcript, --date YYYY-MM-DD, and a --metadata JSON blob "
    "({participants, duration_min}). Re-ingest is a no-op unless --force; "
    "Krisp ingest also refreshes the Calendar/Contacts directory used by the "
    "linker. See `brain ingest-stdin --help` for the full flag list."
)


app = typer.Typer(
    name="brain",
    help="Local personal knowledge base. Hybrid search over your career corpus.",
    epilog=_KRISP_INGEST_HELP,
    no_args_is_help=True,
)

vault_app = typer.Typer(
    name="vault",
    help="Vault management.",
    no_args_is_help=True,
)
app.add_typer(vault_app, name="vault")

vault_directory_app = typer.Typer(
    name="directory",
    help="Inspect and rebuild the linker's name↔email directory.",
    no_args_is_help=True,
)
vault_app.add_typer(vault_directory_app, name="directory")

note_app = typer.Typer(
    name="note",
    help="Authoring commands for vault notes.",
    no_args_is_help=True,
)
app.add_typer(note_app, name="note")

backfill_app = typer.Typer(
    name="backfill",
    help="One-shot data-hygiene utilities for legacy rows.",
    no_args_is_help=True,
)
app.add_typer(backfill_app, name="backfill")


@app.callback()
def _main() -> None:
    """brain — second brain CLI root."""


@app.command()
def init() -> None:
    """Apply database migrations and align embedding column with active embedder.

    After running every SQL file in ``migrations/``, reconciles the
    ``chunks.embedding`` column dim against ``BRAIN_EMBEDDER``'s native
    output. On a fresh DB this drops + re-adds the column at the right
    dim; on an existing DB with chunks already present it errors clearly
    and tells the user how to do a destructive reset (the only safe way
    to switch backends).
    """
    cfg = Config.load()
    embedder = make_embedder(cfg)
    search_backfill_report: backfill_search.BackfillReport | None = None
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        applied = run_migrations(conn)
        ensure_embedding_column(conn, embedder)
        # Migration 009 added title_text/tags_text/search_extras columns to
        # chunks but seeded them as NULL. When 009 is applied for the first
        # time on a populated DB, run the backfill in the same init pass so
        # search ranking is correct without a separate operator step. On a
        # fresh DB (zero chunks) the backfill is a cheap no-op. On
        # subsequent init runs 009 is no longer in ``applied`` so we skip
        # the work entirely.
        if "009_chunks_weighted_tsv.sql" in applied:
            search_backfill_report = backfill_search.run(conn)
    if applied:
        for name in applied:
            typer.echo(f"applied {name}")
    else:
        typer.echo("no migrations to apply")
    typer.echo(f"embedder        {cfg.embedder} (dim={embedder.dim})")
    if search_backfill_report is not None:
        typer.echo(
            f"backfill search Stage A: {search_backfill_report.stage_a_rows} "
            f"row(s) / Stage B: {search_backfill_report.stage_b_rows} row(s) "
            f"/ total chunks: {search_backfill_report.total_chunks}"
        )


def _ollama_loaded_models(payload: Any) -> list[str]:
    """Extract the list of model names from an ``/api/tags`` payload.

    Returns ``[]`` for any structurally unexpected shape so callers can treat
    "doctor doesn't know" the same as "model not present" — a soft warning,
    never a failure.
    """
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for entry in models:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _model_loaded(wanted: str, loaded: list[str]) -> bool:
    """True iff ``wanted`` matches one of ``loaded`` exactly or modulo the
    ``:tag`` suffix (Ollama lists ``qwen3-embedding:8b`` as the full tag)."""
    if wanted in loaded:
        return True
    # If the user configured a bare repo without a tag, accept any tag.
    bare = wanted.split(":", 1)[0]
    return any(name.split(":", 1)[0] == bare for name in loaded)


# Per-backend Ollama model that ``brain doctor`` checks for. The voyage
# backend has no Ollama dependency — handled separately.
_BACKEND_OLLAMA_MODEL = {
    "arctic": "snowflake-arctic-embed2",
    # qwen3 reads from cfg.qwen3_model (user-configurable) so that's looked
    # up dynamically in _check_ollama; this dict only carries the static one.
}


def _check_voyage(cfg: Config, failures: list[str]) -> None:
    """Doctor sub-check: verify ``VOYAGE_API_KEY`` is set when backend is voyage."""
    if cfg.voyage_api_key:
        typer.echo("voyage          OK (api key set)")
        return
    failures.append("VOYAGE_API_KEY not set")
    typer.secho(
        "voyage          FAIL — VOYAGE_API_KEY not set",
        fg="red",
        err=True,
    )


def _check_ollama(cfg: Config, failures: list[str]) -> None:
    """Doctor sub-check: ping Ollama and verify the backend's model is loaded."""
    if cfg.embedder == "qwen3":
        wanted = cfg.qwen3_model
    else:
        wanted = _BACKEND_OLLAMA_MODEL.get(cfg.embedder, cfg.qwen3_model)
    try:
        with httpx.Client(
            base_url=cfg.ollama_host, timeout=httpx.Timeout(5.0)
        ) as client:
            response = client.get("/api/tags")
            response.raise_for_status()
            tags_payload = response.json()
        loaded_models = _ollama_loaded_models(tags_payload)
        if _model_loaded(wanted, loaded_models):
            typer.echo(f"ollama          OK ({cfg.ollama_host})")
        else:
            # Soft warning — daemon up but the configured model isn't pulled.
            # Don't fail doctor; embed calls will surface "no such model"
            # later if anyone tries to use it.
            typer.secho(
                f"ollama          OK ({cfg.ollama_host}) — model {wanted} "
                f"NOT loaded — run `ollama pull {wanted}`",
                fg="yellow",
            )
    except (httpx.HTTPError, ValueError) as e:
        # ValueError covers a non-JSON /api/tags response (json.JSONDecodeError).
        failures.append(f"ollama: {e}")
        typer.secho(f"ollama          FAIL — {e}", fg="red", err=True)


@app.command()
def doctor() -> None:
    """Check environment, database connection, and external dependencies.

    Backend-aware: voyage runs the API-key check; arctic and qwen3 ping
    Ollama and verify their respective models are loaded.
    """
    failures: list[str] = []

    try:
        cfg = Config.load()
        typer.echo(f"env             OK (embedder={cfg.embedder})")
    except ConfigError as e:
        typer.secho(f"env             FAIL — {e}", fg="red", err=True)
        raise typer.Exit(code=1) from e

    try:
        with connect(cfg.database_url) as conn:
            conn.execute("SELECT 1")
            ext = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            ).fetchone()
            if ext:
                typer.echo(f"postgres        OK (pgvector {ext[0]})")
                _report_embedding_column(conn)
                _report_mirror_drift(conn, vault_path=cfg.vault_path)
            else:
                failures.append("pgvector extension not installed (run brain init)")
                typer.echo("postgres        FAIL — pgvector not installed")
    except psycopg.Error as e:
        failures.append(f"database: {e}")
        typer.secho(f"postgres        FAIL — {e}", fg="red", err=True)

    if cfg.embedder == "voyage":
        _check_voyage(cfg, failures)
    else:
        _check_ollama(cfg, failures)

    if shutil.which("gws"):
        typer.echo("gws CLI         OK")
    else:
        typer.echo("gws CLI         missing — Gmail ingestion disabled")

    _check_npx()

    if failures:
        raise typer.Exit(code=1)


def _check_npx() -> None:
    """Doctor sub-check: probe ``npx`` for the Quartz integration.

    Soft check — Quartz is optional; missing npx is a warning, never a
    failure. We only print one line either way:

    - ``quartz/npx       OK (npx 10.x.x at /path/to/npx)`` — present.
    - ``quartz/npx       not installed`` — absent.

    Treats every error path as "not installed": missing on PATH,
    timeout, non-zero exit, or unparseable stdout. Doctor never fails
    on Quartz absence — `brain vault render` is the only command that
    needs it and it surfaces its own setup errors when invoked.
    """
    npx_path = shutil.which("npx")
    if npx_path is None:
        typer.secho(
            "quartz/npx      not installed — `brain vault render` will fail; "
            "install Node.js if you want HTML rendering",
            fg="yellow",
        )
        return
    try:
        completed = subprocess.run(  # noqa: S603 — list-form args, no shell
            [npx_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        # Daemon hung, npx not executable, etc. Treat as "not
        # installed" — we don't want a flaky probe to red-flag doctor.
        typer.secho(
            "quartz/npx      not installed — `brain vault render` will fail; "
            "install Node.js if you want HTML rendering",
            fg="yellow",
        )
        return
    if completed.returncode != 0:
        typer.secho(
            "quartz/npx      not installed — `brain vault render` will fail; "
            "install Node.js if you want HTML rendering",
            fg="yellow",
        )
        return
    version = completed.stdout.strip() or "?"
    typer.echo(
        f"quartz/npx      OK (npx {version} at {npx_path}) — "
        "`brain vault render` available"
    )


@app.command()
def status() -> None:
    """Show counts and last-ingest timestamp."""
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        counts = summary_counts(conn)

    typer.echo(f"documents       {counts.documents}")
    typer.echo(f"chunks          {counts.chunks}")
    typer.echo(f"sources         {counts.sources}")
    typer.echo(f"last ingest     {counts.last_ingest or 'never'}")
    typer.echo("\nby source:")
    for kind, count in counts.by_kind:
        typer.echo(f"  {kind:<12} {count}")


def _report_embedding_column(conn: psycopg.Connection[Any]) -> None:
    """Print a one-line status for the ``chunks.embedding`` column.

    Informational only — never fails the doctor check. Reports column type,
    NOT NULL status, and (for low-dim backends) HNSW index presence. For
    Qwen3 (4096 dims) the index is absent by design — pgvector caps
    HNSW/IVFFlat at 2000 dims for ``vector``.
    """
    state = embedding_column_state(conn)
    parts = [state.column_type]
    if state.not_null:
        parts.append("NOT NULL")
    else:
        parts.append("nullable")
    if state.has_index:
        parts.append("indexed [hnsw]")
    summary = ", ".join(parts)
    typer.echo(f"embedding       OK ({summary})")
    if not state.not_null:
        typer.secho(
            "                — run `brain reembed` to backfill and finalize",
            fg="yellow",
        )


def _report_mirror_drift(
    conn: psycopg.Connection[Any], *, vault_path: Path
) -> None:
    """Print a one-line "vault drift" status for the ``_ingested/`` mirror tier.

    Informational only — never fails the doctor check. Counts ingested
    rows, rows missing ``vault_path``, on-disk orphan files (file present,
    no DB row matches its frontmatter id), and ghost rows (DB claims a
    ``vault_path`` whose file is missing from disk).

    A clean state prints "OK"; any non-zero counter flips the line yellow
    and follows up with one suggested-fix line per actionable counter so
    the user knows the next move without grepping the README.

    The vault directory may not exist yet (fresh install before
    ``brain vault init``); in that case we skip the check entirely with a
    soft "not initialized" line. Doctor never fails here — vault drift is
    a hygiene signal, not a runtime blocker.
    """
    if not vault_path.is_dir():
        typer.echo(f"vault drift     not initialized ({vault_path} missing)")
        return
    summary = mirror_drift_summary(conn, vault_path=vault_path)
    counters = (
        f"{summary.total_ingested_rows} mirrors, "
        f"{summary.rows_with_null_vault_path} NULL vault_path, "
        f"{summary.orphan_files} orphan files, "
        f"{summary.ghost_rows} ghost rows"
    )
    if _drift_clean(summary):
        typer.echo(f"vault drift     OK ({counters})")
        return
    typer.secho(f"vault drift     drift detected ({counters})", fg="yellow")
    if summary.rows_with_null_vault_path:
        typer.secho(
            "                — `brain vault export --force` to populate "
            "NULL vault_path",
            fg="yellow",
        )
    if summary.orphan_files:
        typer.secho(
            "                — `brain vault prune-orphans` to inspect "
            "orphan files (dry-run)",
            fg="yellow",
        )
    if summary.ghost_rows:
        typer.secho(
            "                — `brain vault export --force` to recreate "
            "missing files (or `brain rm <id>` per ghost)",
            fg="yellow",
        )


def _drift_clean(summary: MirrorDriftSummary) -> bool:
    """True iff every actionable drift counter is zero."""
    return (
        summary.rows_with_null_vault_path == 0
        and summary.orphan_files == 0
        and summary.ghost_rows == 0
    )


def _build_embedder(cfg: Config) -> Embedder:
    """Build the configured embedder. Indirected so tests can substitute a fake.

    Returns the :class:`Embedder` Protocol — callers should not depend on the
    concrete backend (Qwen3 today; potentially others tomorrow).
    """
    return make_embedder(cfg)


@app.command()
def ingest(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to the document."),
    force: bool = typer.Option(
        False, "--force", help="Re-ingest even if content already exists."
    ),
) -> None:
    """Ingest a single file (TXT/MD/PDF/DOCX)."""
    cfg = Config.load()
    embedder = _build_embedder(cfg)
    doc = extract_path(path)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        result = ingest_document(
            conn,
            embedder=embedder,
            doc=doc,
            source_kind="manual",
            tags=list(tag),
            force=force,
            vault_root=cfg.vault_path,
        )
    verb = "ingested" if result.created else "skipped (already ingested)"
    typer.echo(f"{verb}: {path.name} → {result.document_id}")


@app.command(name="ingest-dir")
def ingest_dir(
    path: Path = typer.Argument(..., exists=True, file_okay=False, readable=True),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to every document."),
    ext: str | None = typer.Option(
        None,
        "--ext",
        help="Comma-separated extensions to include (default: all supported).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List files that would be ingested without writing."
    ),
) -> None:
    """Recursively ingest a directory of files."""
    cfg = Config.load()
    extensions = (
        [f".{e.strip().lstrip('.').lower()}" for e in ext.split(",")]
        if ext
        else supported_extensions()
    )
    files = [
        p
        for p in Path(path).rglob("*")
        if p.is_file() and p.suffix.lower() in extensions
    ]
    typer.echo(f"found {len(files)} file(s)")
    if dry_run:
        for f in files:
            typer.echo(f"  would ingest: {f}")
        return

    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for f in files:
            try:
                doc = extract_path(f)
                result = ingest_document(
                    conn,
                    embedder=embedder,
                    doc=doc,
                    source_kind="manual",
                    tags=list(tag),
                    vault_root=cfg.vault_path,
                )
                verb = "ingested" if result.created else "skipped"
                typer.echo(f"  {verb}: {f.name}")
            except (ValueError, OSError, psycopg.Error) as e:
                typer.secho(f"  failed: {f.name} — {e}", fg="red")


@app.command(name="ingest-stdin")
def ingest_stdin(
    source: str = typer.Option(
        ..., "--source", help="Source kind (krisp, slack, gmail, ...)."
    ),
    external_id: str = typer.Option(
        ..., "--external-id", help="Stable id from the upstream system."
    ),
    title: str = typer.Option(..., "--title", help="Document title."),
    content_type: str = typer.Option(
        "transcript", "--content-type", help="Content type label (e.g. transcript, note)."
    ),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to the document."),
    metadata: str | None = typer.Option(
        None, "--metadata", help="JSON metadata blob merged into source + document metadata."
    ),
    date: str | None = typer.Option(
        None, "--date", help="Date stamp (ISO); stored under metadata.date."
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-ingest even if content already exists."
    ),
) -> None:
    """Ingest content piped on stdin (used by Claude for Krisp/Slack)."""
    content = sys.stdin.read()
    if not content.strip():
        typer.secho("stdin was empty", fg="red", err=True)
        raise typer.Exit(code=1)
    meta: dict[str, Any] = _json.loads(metadata) if metadata else {}
    if date:
        meta.setdefault("date", date)
    doc = _stdin_make_doc(
        content=content,
        title=title,
        content_type=content_type,
        metadata=meta,
    )

    cfg = Config.load()
    embedder = _build_embedder(cfg)
    # Krisp ingest triggers Calendar/Contacts directory refresh via the gws
    # CLI; other sources don't need a runner. Refresh failures are warnings,
    # not errors — the ingest itself still succeeds.
    gws_runner = real_gws_runner if source == "krisp" else None
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        result = ingest_document(
            conn,
            embedder=embedder,
            doc=doc,
            source_kind=source,
            source_external_id=external_id,
            source_metadata=meta,
            tags=list(tag),
            force=force,
            gws_runner=gws_runner,
            vault_root=cfg.vault_path,
        )
    verb = "ingested" if result.created else "skipped (already ingested)"
    typer.echo(f"{verb}: {title} → {result.document_id}")


@app.command(name="ingest-gmail")
def ingest_gmail(
    query: str | None = typer.Option(None, "--query", "-q", help="Raw Gmail search query."),
    label: str | None = typer.Option(None, "--label", "-l", help="Gmail label to scope to."),
    from_addr: str | None = typer.Option(None, "--from", help="Filter by sender address."),
    since: str | None = typer.Option(None, "--since", help="Earliest date (YYYY/MM/DD)."),
    until: str | None = typer.Option(None, "--until", help="Latest date (YYYY/MM/DD)."),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to each document."),
    max_results: int = typer.Option(50, "--max", help="Max messages to fetch."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List matches without ingesting."
    ),
) -> None:
    """Ingest Gmail messages via the `gws` CLI, batched per thread.

    P2.3 collapses N messages sharing a ``threadId`` into a single
    ``content_type='email_thread'`` document via :func:`to_extracted_thread`.
    Re-ingesting an unchanged thread is a no-op (P2.2 same-hash short-circuit);
    a thread that has grown by one message updates the existing row in place
    so downstream links / derived_links continue to point at a stable UUID.

    At least one scope flag is required (no bulk-inbox ingests).
    """
    if not any([query, label, from_addr, since, until]):
        typer.secho(
            "ingest-gmail requires at least one scope flag: "
            "--query, --label, --from, --since, --until",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)

    cfg = Config.load()
    stubs = gmail_ingest.list_messages(
        query=query,
        label=label,
        since=since,
        until=until,
        from_addr=from_addr,
        max_results=max_results,
    )
    typer.echo(f"found {len(stubs)} message(s)")
    if not stubs:
        typer.echo("no messages matched")
        return

    # Group stubs by Gmail ``threadId`` while preserving list-order so the
    # dry-run report is deterministic across runs that hit the same query.
    threads: dict[str, list[dict[str, Any]]] = {}
    for stub in stubs:
        tid = stub.get("threadId") or stub.get("id")
        if not isinstance(tid, str) or not tid:
            # Defensive: malformed stubs without an id at all are unreachable
            # against real Gmail traffic, but skip rather than crash so a
            # partial response from `gws` doesn't poison the whole batch.
            continue
        threads.setdefault(tid, []).append(stub)

    total_messages = sum(len(t) for t in threads.values())

    if dry_run:
        typer.echo(f"would ingest {len(threads)} thread(s):")
        for tid, ts in threads.items():
            subject = _gmail_thread_subject_for_dry_run(ts)
            typer.echo(
                f"  [thread_id={tid} messages={len(ts)}] Subject: {subject}"
            )
        typer.echo(
            f"total: {len(threads)} threads, {total_messages} messages → "
            f"{len(threads)} documents"
        )
        return

    embedder = _build_embedder(cfg)
    ingested = 0
    skipped = 0
    skipped_drafts = 0
    failed = 0
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for tid, ts in threads.items():
            try:
                messages = [gmail_ingest.read_message(stub["id"]) for stub in ts]
                doc = gmail_ingest.to_extracted_thread(messages)
                result = ingest_document(
                    conn,
                    embedder=embedder,
                    doc=doc,
                    source_kind="gmail",
                    source_external_id=tid,
                    source_metadata={
                        "thread_id": tid,
                        "from": doc.metadata.get("from"),
                        "date": doc.metadata.get("date"),
                    },
                    tags=list(tag),
                    vault_root=cfg.vault_path,
                )
                # P2.2 thread upsert: ``created`` is True only on first
                # insert; ``body_changed`` is True when an existing thread
                # was rewritten in place (new message appended). Either
                # counts as "ingested" for the per-thread summary; an
                # unchanged thread (both False) is "skipped".
                if result.created or result.body_changed:
                    typer.echo(
                        f"  ingested thread {tid} ({len(ts)} messages)"
                    )
                    ingested += 1
                else:
                    typer.echo(f"  skipped thread {tid} (unchanged)")
                    skipped += 1
            except DraftSkipped as e:
                # Drafts are unsent emails the user typed but never sent —
                # ingesting them pollutes search ("did I send X?" returns
                # drafts as evidence of sent messages). Skip and surface
                # the count separately so it's clear how many threads were
                # filtered for this reason vs failed for other reasons.
                typer.echo(f"  skipped thread {tid} (draft): {e}")
                skipped_drafts += 1
                continue
            except (GmailError, psycopg.Error, ValueError, KeyError) as e:
                typer.secho(
                    f"  failed thread {tid} ({len(ts)} messages): {e}",
                    fg="red",
                )
                failed += 1
                continue
    typer.echo(
        f"{ingested} ingested, {skipped} skipped (unchanged), "
        f"{skipped_drafts} skipped (drafts), {failed} failed"
    )


def _gmail_thread_subject_for_dry_run(stubs: list[dict[str, Any]]) -> str:
    """Best-effort subject lookup for ``brain ingest-gmail --dry-run``.

    Reads the FIRST message of the thread to pull its ``Subject`` header — a
    single ``read_message`` call per thread is acceptably cheap for a dry-run
    report (``ingest-gmail`` callers typically scope to <100 threads). On any
    failure the function returns ``"(unable to fetch)"`` so a single bad
    message can't abort the whole report; the actual ingest pass will hit
    the same failure and surface it via the structured per-thread error path.
    """
    try:
        first_id = stubs[0]["id"]
        full = gmail_ingest.read_message(first_id)
    except (GmailError, KeyError, IndexError):
        return "(unable to fetch)"
    payload = full.get("payload") or {}
    headers = payload.get("headers") or []
    for h in headers:
        if (h.get("name") or "").lower() == "subject":
            return h.get("value") or "(no subject)"
    return "(no subject)"


@app.command()
def reembed(
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Max chunks to embed (default: all)."
    ),
    batch_size: int = typer.Option(32, "--batch-size", help="Embedding batch size."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report counts without embedding."
    ),
    all_chunks: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Re-embed every chunk (not just NULL). Use after switching backends.",
    ),
    finalize: bool = typer.Option(
        True,
        "--finalize/--no-finalize",
        help="After backfill, apply NOT NULL on chunks.embedding.",
    ),
) -> None:
    """Backfill ``chunks.embedding`` for rows missing an embedding.

    After ``brain init``, chunks have NULL embeddings until this command
    runs. Idempotent — safe to re-run after a crash; only rows still NULL
    are touched.

    Pass ``--all`` to re-embed every chunk regardless of NULL state. Use
    this after switching ``BRAIN_EMBEDDER`` backends, where existing
    embeddings are still present in the column but live in the wrong
    vector space.

    By default, after backfill completes (0 NULL rows remain), applies
    NOT NULL on the embedding column. For backends with ``dim <= 2000``
    (arctic, voyage), additionally creates an HNSW cosine index. For Qwen3
    (4096 dims) the index is skipped — pgvector caps HNSW at 2000 dims
    for ``vector``; sequential scan is acceptable at personal-corpus scale.

    Pass ``--no-finalize`` to skip the constraint + index step (e.g. for
    incremental runs over multiple sessions).
    """
    cfg = Config.load()
    embedder = _build_embedder(cfg)

    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        target_total = count_chunks_missing_embedding(
            conn, include_embedded=all_chunks
        )
        target = min(limit, target_total) if limit is not None else target_total
        scope = "chunk(s) total" if all_chunks else "chunk(s) have NULL embedding"

        if dry_run:
            typer.echo(f"would embed {target} chunk(s)")
            typer.echo(f"  ({target_total} {scope})")
            return

        if target_total == 0:
            typer.echo("nothing to embed (all chunks have embeddings)")
        else:
            embedded = 0
            for batch in iter_chunks_missing_embedding(
                conn, batch_size=batch_size, include_embedded=all_chunks
            ):
                if limit is not None and embedded >= limit:
                    break
                if limit is not None:
                    batch = batch[: limit - embedded]
                vectors = embedder.embed(
                    [c.content for c in batch], input_type="document"
                )
                for c, vec in zip(batch, vectors, strict=True):
                    conn.execute(
                        "UPDATE chunks SET embedding=%s WHERE id=%s",
                        (vec, c.id),
                    )
                embedded += len(batch)
                typer.echo(f"  embedded {embedded}/{target}")

            verb = "re-embedded" if all_chunks else "backfilled"
            typer.echo(f"{verb} {embedded} chunk(s)")

        if finalize:
            remaining = count_chunks_missing_embedding(conn)
            if remaining == 0:
                try:
                    finalize_embedding_index(conn, embedder)
                    typer.echo("finalized: embedding column is now NOT NULL")
                except ValueError as e:
                    typer.secho(f"finalize failed: {e}", fg="red", err=True)
                    raise typer.Exit(code=1) from e
            else:
                typer.echo(
                    f"finalize skipped: {remaining} chunk(s) still have NULL embedding"
                )


@app.command()
def search(
    query: str = typer.Argument(...),
    limit: int = typer.Option(5, "--limit", "-n"),
    source: str | None = typer.Option(None, "--source"),
    tag: str | None = typer.Option(None, "--tag"),
    since_days: int | None = typer.Option(None, "--since", help="Days lookback"),
    json_output: bool = typer.Option(False, "--json"),
    fts_only: bool = typer.Option(False, "--fts-only"),
) -> None:
    """Hybrid search across the brain."""
    cfg = Config.load()
    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        results = hybrid_search(
            conn,
            embedder=embedder,
            query=query,
            limit=limit,
            source_kind=source,
            tag=tag,
            since_days=since_days,
            fts_only=fts_only,
            vector_sim_floor=cfg.vector_sim_floor,
        )

    if json_output:
        emit_json(
            [
                {
                    "id": r.document_id,
                    "title": r.title,
                    "source_kind": r.source_kind,
                    "snippet": r.snippet,
                    "score": r.score,
                    "content_type": r.content_type,
                    "tags": r.tags,
                }
                for r in results
            ]
        )
        return
    if not results:
        typer.echo("(no results)")
        return
    console.print(search_table(results))


def _resolve_id(conn: psycopg.Connection[Any], prefix: str) -> str:
    """Resolve a UUID prefix (min 6 chars) to a full document id.

    Thin wrapper around :func:`brain.queries.resolve_document_prefix` that maps
    its plain exceptions to Typer-flavored ones (``BadParameter`` for argument
    validation, ``Exit`` + a red stderr line for runtime resolution failures).
    """
    try:
        return resolve_document_prefix(conn, prefix)
    except (IdPrefixTooShort, IdPrefixNotHex) as e:
        raise typer.BadParameter(str(e)) from e
    except (IdPrefixNotFound, IdPrefixAmbiguous) as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(code=1) from e


@app.command()
def show(
    id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print a document by id (or 6+ char prefix)."""
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        doc_id = _resolve_id(conn, id)
        doc = fetch_document(conn, doc_id)
    assert doc is not None  # _resolve_id confirmed the doc exists
    if json_output:
        emit_json(
            {
                "id": doc.id,
                "title": doc.title,
                "content": doc.content,
                "content_type": doc.content_type,
                "tags": doc.tags,
                "source_path": doc.source_path,
                "ingested_at": doc.ingested_at,
                "source_kind": doc.source_kind,
            }
        )
        return
    typer.echo(f"# {doc.title}")
    typer.echo(f"id:           {doc.id}")
    typer.echo(f"source:       {doc.source_kind or 'manual'} ({doc.content_type})")
    typer.echo(f"tags:         {', '.join(doc.tags) or '(none)'}")
    typer.echo(f"ingested:     {doc.ingested_at}")
    typer.echo("")
    typer.echo(doc.content or "")


@app.command(name="list")
def list_docs(
    source: str | None = typer.Option(None, "--source"),
    tag: str | None = typer.Option(None, "--tag"),
    limit: int = typer.Option(20, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List documents in the brain."""
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        rows = list_documents(conn, source=source, tag=tag, limit=limit)
    if json_output:
        emit_json(
            [
                {
                    "id": r.id,
                    "title": r.title,
                    "content_type": r.content_type,
                    "tags": r.tags,
                    "source_kind": r.source_kind,
                    "ingested_at": r.ingested_at,
                }
                for r in rows
            ]
        )
        return
    for r in rows:
        kind = r.source_kind or "manual"
        typer.echo(f"{r.id[:8]}  {kind:<8}  {r.content_type:<10}  {r.title}")


@app.command(context_settings={"ignore_unknown_options": True})
def tag(
    id: str = typer.Argument(...),
    mods: list[str] = typer.Argument(...),
    regenerate_file: bool = typer.Option(
        False,
        "--regenerate-file",
        help=(
            "If the doc's vault mirror is missing, recreate it from the DB "
            "before applying tags. Errors out for vault-tier authored notes."
        ),
    ),
) -> None:
    """Add (+name) or remove (-name) tags. Example: brain tag abc1234 +interview -draft

    When the document has a ``vault_path``, the change is also written to the
    file's frontmatter so the next ``brain vault sync`` does not re-read
    stale ``tags: []`` from disk and overwrite the DB. The rewrite is
    idempotent — re-running with the same arguments touches neither DB nor
    file. Pass ``--regenerate-file`` to recreate a missing ``_ingested/``
    mirror from the DB row (vault-tier authored notes are refused).
    """
    add = [m[1:] for m in mods if m.startswith("+") and len(m) > 1]
    remove = [m[1:] for m in mods if m.startswith("-") and len(m) > 1]
    if not (add or remove):
        raise typer.BadParameter("expected +tag or -tag arguments")
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id)
        # Capture vault_path / kind BEFORE the DB write so the suffix matches
        # the row state we read; the DB write below cannot change either field.
        row = conn.execute(
            "SELECT vault_path, kind FROM documents WHERE id = %s",
            (doc_id,),
        ).fetchone()
        assert row is not None  # _resolve_id confirmed the doc exists
        vault_path_rel: str | None = row[0]
        kind: str = row[1]
        new_tags = apply_tags(conn, doc_id, add=add, remove=remove)
        suffix = _tag_file_writeback(
            conn,
            cfg=cfg,
            vault_path_rel=vault_path_rel,
            kind=kind,
            new_tags=new_tags,
            doc_id=doc_id,
            regenerate_file=regenerate_file,
        )
    typer.echo(f"updated tags on {doc_id[:8]}{suffix}")


def _tag_file_writeback(
    conn: psycopg.Connection[Any],
    *,
    cfg: Config,
    vault_path_rel: str | None,
    kind: str,
    new_tags: list[str],
    doc_id: str,
    regenerate_file: bool,
) -> str:
    """Apply the post-``apply_tags`` file-system side effects for ``brain tag``.

    Returns the suffix to append to the CLI's "updated tags on <id>" line —
    the suffix shape is part of the user-facing contract and is matched by
    downstream tests, so keep the strings stable across changes.

    Behavior matches the matrix in
    ``docs/plans/2026-04-30-brain-tag-frontmatter-write.md``:

    - ``vault_path`` NULL → DB-only, no warning.
    - File exists → :func:`rewrite_tags` (idempotent — no-op when tags already
      match disk).
    - File missing + ``kind='ingested'`` + ``--regenerate-file`` →
      :func:`regenerate_vault_file` then :func:`rewrite_tags`.
    - File missing + ``kind='ingested'`` (no flag) → DB-only + yellow warn.
    - File missing + ``kind='vault'`` + ``--regenerate-file`` → ``BadParameter``
      (regenerating an authored note from DB risks data loss).
    - File missing + ``kind='vault'`` (no flag) → DB-only + yellow warn.
    """
    if vault_path_rel is None:
        return " (db only)"
    abs_path = cfg.vault_path / vault_path_rel
    if abs_path.exists():
        rewrite_tags(abs_path, new_tags)
        return " (file)"
    if kind == "ingested" and regenerate_file:
        written_path = regenerate_vault_file(
            conn, doc_id, vault_path=cfg.vault_path
        )
        rewrite_tags(written_path, new_tags)
        return " (file regenerated)"
    if kind == "ingested":
        typer.secho(
            "file missing on disk; tagged DB only. "
            "Pass --regenerate-file to recreate the vault mirror from the DB.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return " (db only, file missing)"
    if kind == "vault" and regenerate_file:
        raise typer.BadParameter(
            "cannot --regenerate-file a vault-tier authored note; "
            "restore from backup or git instead"
        )
    # kind == "vault" without --regenerate-file
    typer.secho(
        "vault-tier authored note is missing on disk; "
        "restore from backup or git rather than regenerating.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    return " (db only, vault file missing)"


def _print_update_result(result: UpdateResult, doc_id: str) -> None:
    """Print a one-line summary of an update."""
    label = doc_id[:8]
    if not result.fields_changed:
        typer.echo(f"updated {label} (no changes)")
        return
    typer.echo(f"updated {label} ({'|'.join(result.fields_changed)})")


def _has_mutating_edit_flag(
    *,
    title: str | None,
    content_type: str | None,
    metadata: str | None,
    content_file: Path | None,
    content_stdin: bool,
) -> bool:
    """True iff the user supplied any flag that changes a document field."""
    return any(
        [
            title is not None,
            content_type is not None,
            metadata is not None,
            content_file is not None,
            content_stdin,
        ]
    )


def _edit_via_editor(cfg: Config, doc_id: str) -> int:
    """Editor-mode implementation. Returns the desired CLI exit code.

    Splits the work into three phases so the DB connection is *not* held
    across the editor (which can block for hours):

    1. Read the current document fields, then close the connection.
    2. Render the payload, invoke the editor, parse the result.
    3. Open a fresh connection and apply the update transactionally.
    """
    # Phase 1: read.
    with connect(cfg.database_url) as conn:
        row = conn.execute(
            "SELECT title, content, content_type, tags, metadata "
            "FROM documents WHERE id=%s",
            (doc_id,),
        ).fetchone()
    assert row is not None  # caller resolved the id; row must exist
    cur_title, cur_content, cur_type, cur_tags, cur_meta = row
    cur_tags = list(cur_tags or [])
    cur_meta = dict(cur_meta or {})

    initial = build_payload(
        title=cur_title,
        content_type=cur_type,
        tags=cur_tags,
        metadata=cur_meta,
        body=cur_content,
    )

    # Phase 2: invoke editor (no DB connection held).
    label = doc_id[:8]
    try:
        header, body = run_editor_session(initial, doc_id_label=label)
    except EditorError as e:
        typer.secho(str(e), fg="red", err=True)
        return 1
    except EditorAbortedError:
        typer.secho("aborted (editor exited non-zero)", fg="red", err=True)
        return 1
    except EditorUnchangedError:
        typer.echo(f"updated {label} (no changes)")
        return 0
    except EditorParseFailedError as e:
        typer.secho(
            f"could not parse JSON header: {e}\n"
            f"your draft was preserved at {e.preserved_path}",
            fg="red",
            err=True,
        )
        return 1

    # Body normalization is for the no-op gate ONLY — POSIX editors append a
    # trailing newline, which would otherwise look like a meaningful change.
    # The user's exact body (newlines and all) is what we hand to the DB.
    body_changed = body.rstrip("\n") != cur_content.rstrip("\n")
    new_title = header.get("title") if isinstance(header.get("title"), str) else None
    new_type = (
        header.get("content_type")
        if isinstance(header.get("content_type"), str)
        else None
    )
    new_tags = header.get("tags") if isinstance(header.get("tags"), list) else None
    new_meta = header.get("metadata") if isinstance(header.get("metadata"), dict) else None

    # Phase 3: apply.
    embedder: Any = _build_embedder(cfg) if body_changed else None
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        try:
            result = update_document(
                conn,
                document_id=doc_id,
                embedder=embedder,
                new_title=new_title,
                new_content_type=new_type,
                new_content=body if body_changed else None,
                metadata_patch=new_meta,
                replace_metadata=True,
                new_tags=new_tags,
                vault_root=cfg.vault_path,
            )
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            return 1
    _print_update_result(result, doc_id)
    return 0


def _document_tier(
    conn: psycopg.Connection[Any], doc_id: str
) -> tuple[str, str | None]:
    """Return ``(kind, vault_path)`` for ``doc_id``.

    Wrapper kept narrow so the ``brain edit`` and (future) ``brain rm``
    branches that need to gate on document tier share one query and the
    same NULL-handling. Caller already validated the id via
    :func:`_resolve_id`, so the row must exist.
    """
    row = conn.execute(
        "SELECT kind, vault_path FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None  # _resolve_id confirmed the doc exists
    return str(row[0]), (str(row[1]) if row[1] is not None else None)


def _edit_vault_file(cfg: Config, doc_id: str, vault_path: str) -> int:
    """Vault-tier edit: open the file in $EDITOR, sync on exit.

    Returns the desired CLI exit code. Mirrors the JSON-header flow's
    drop-the-DB-connection-during-editor pattern: the connection is closed
    before the editor blocks (could be hours) and reopened only for the
    post-exit sync.

    Editor non-zero exit aborts; the file is left exactly as the user wrote
    it. The next ``brain vault sync`` (or another ``brain edit``) will pick
    up the in-progress changes.
    """
    file_path = (cfg.vault_path / vault_path).resolve()
    if not file_path.is_file():
        typer.secho(
            f"vault file is missing on disk: {file_path}\n"
            f"run `brain vault sync --prune` to clean up the DB row, "
            f"or restore the file before editing.",
            fg="red",
            err=True,
        )
        return 1

    try:
        rc = run_editor_on(file_path)
    except RawEditorError as e:
        typer.secho(str(e), fg="red", err=True)
        return 1
    if rc != 0:
        typer.secho("aborted (editor exited non-zero)", fg="red", err=True)
        return 1

    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        report = sync_one_file(
            conn,
            embedder=embedder,
            vault_path=cfg.vault_path,
            file_path=file_path,
        )
    if report.errors:
        for path, reason in report.errors:
            typer.secho(f"sync error: {path}: {reason}", fg="red", err=True)
        return 1
    label = doc_id[:8]
    if report.created:
        typer.echo(f"updated {label} (created)")
    elif report.updated:
        typer.echo(f"updated {label} (synced)")
    else:
        typer.echo(f"updated {label} (no changes)")
    return 0


@app.command()
def edit(
    id: str = typer.Argument(...),
    title: str | None = typer.Option(None, "--title", help="New document title."),
    content_type: str | None = typer.Option(
        None, "--content-type", help="New content type label."
    ),
    metadata: str | None = typer.Option(
        None,
        "--metadata",
        help="JSON object to merge into existing metadata "
        "(top-level keys overwrite — nested objects are not deep-merged).",
    ),
    replace_metadata: bool = typer.Option(
        False,
        "--replace-metadata",
        help="With --metadata, swap the entire JSONB blob instead of merging.",
    ),
    content_file: Path | None = typer.Option(
        None,
        "--content-file",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Replace document body with the contents of this file (re-embeds).",
    ),
    content_stdin: bool = typer.Option(
        False, "--content-stdin", help="Replace document body with stdin (re-embeds)."
    ),
) -> None:
    """Update title / content_type / metadata / body of an existing document.

    Behavior is tier-aware:

    - **Vault-tier docs** (``kind='vault'``, file-backed) — the file IS the
      source of truth. With no flags, ``$EDITOR`` opens the underlying
      ``.md`` directly (no JSON header) and a single-file sync runs on
      editor exit. Mutating flags (``--title``, ``--content-type``,
      ``--metadata``, ``--content-file``, ``--content-stdin``) are
      rejected — edit the file directly, those fields all live in
      frontmatter.
    - **Ingested-tier docs** — the existing JSON-header + body editor flow
      runs, with the same flag-mode targeted updates as before.
    """
    # Reject `--replace-metadata` without `--metadata` regardless of which
    # mode we're about to enter — silently ignoring it lets a user think the
    # full-replace happened when nothing was passed for it to swap.
    if replace_metadata and metadata is None:
        raise typer.BadParameter("--replace-metadata requires --metadata")

    has_mutating_flag = _has_mutating_edit_flag(
        title=title,
        content_type=content_type,
        metadata=metadata,
        content_file=content_file,
        content_stdin=content_stdin,
    )

    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        doc_id = _resolve_id(conn, id)
        kind, vault_path_value = _document_tier(conn, doc_id)

    # Vault-tier branch: the file is authoritative; flag-mode edits are
    # rejected (no JSON-header round-trip), no-flag mode opens the file.
    if kind == "vault" and vault_path_value:
        if has_mutating_flag:
            typer.secho(
                f"vault-tier docs are file-backed; "
                f"edit `{vault_path_value}` directly with your editor",
                fg="red",
                err=True,
            )
            raise typer.Exit(code=1)
        rc = _edit_vault_file(cfg, doc_id, vault_path_value)
        if rc != 0:
            raise typer.Exit(code=rc)
        return

    if not has_mutating_flag:
        rc = _edit_via_editor(cfg, doc_id)
        if rc != 0:
            raise typer.Exit(code=rc)
        return

    if content_file is not None and content_stdin:
        raise typer.BadParameter("--content-file and --content-stdin are mutually exclusive")

    metadata_patch: dict[str, Any] | None = None
    if metadata is not None:
        try:
            parsed = _json.loads(metadata)
        except _json.JSONDecodeError as e:
            typer.secho(f"--metadata is not valid JSON: {e}", fg="red", err=True)
            raise typer.Exit(code=1) from e
        if not isinstance(parsed, dict):
            typer.secho("--metadata must be a JSON object", fg="red", err=True)
            raise typer.Exit(code=1)
        metadata_patch = parsed

    new_content: str | None = None
    if content_file is not None:
        try:
            new_content = content_file.read_text(encoding="utf-8")
        except OSError as e:  # pragma: no cover - typer already gated on exists/readable
            typer.secho(f"could not read --content-file: {e}", fg="red", err=True)
            raise typer.Exit(code=1) from e
    elif content_stdin:
        new_content = sys.stdin.read()

    if new_content is not None and not new_content.strip():
        typer.secho("content is empty", fg="red", err=True)
        raise typer.Exit(code=1)

    embedder: Any = _build_embedder(cfg) if new_content is not None else None
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        try:
            result = update_document(
                conn,
                document_id=doc_id,
                embedder=embedder,
                new_title=title,
                new_content_type=content_type,
                new_content=new_content,
                metadata_patch=metadata_patch,
                replace_metadata=replace_metadata,
                vault_root=cfg.vault_path,
            )
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e
    _print_update_result(result, doc_id)


@app.command()
def rm(
    id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete a document (and its chunks) from the brain.

    When the document has a ``vault_path`` the on-disk mirror file under
    ``cfg.vault_path / vault_path`` is also unlinked. Without that step the
    next ``brain vault sync`` would re-ingest the file by ``content_hash``
    (or, after a slug rename, create a fresh row), silently undoing the rm.
    A missing mirror is tolerated (debug log only) — the DB delete still
    proceeds.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id)
        # Capture title + vault_path BEFORE the DELETE; the row is gone
        # afterwards and we need both for the prompt and the file unlink.
        row = conn.execute(
            "SELECT title, vault_path FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
        assert row is not None  # _resolve_id confirmed the doc exists
        title: str = row[0]
        vault_path_rel: str | None = row[1]
        if not yes:
            typer.confirm(f"Delete '{title}' ({doc_id[:8]})?", abort=True)
        conn.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
    suffix = _rm_unlink_vault_mirror(cfg=cfg, vault_path_rel=vault_path_rel)
    typer.echo(f"removed {doc_id[:8]}{suffix}")


@app.command(name="mark-draft")
def mark_draft(id: str = typer.Argument(...)) -> None:
    """Quarantine a document: set ``draft=true`` and regenerate its mirror.

    A draft doc still lives in the DB and is reachable via ``brain search`` /
    ``brain show`` / ``brain list`` (the CLI is local — the user wants to
    see drafts). Only the wiki hides it: the Quartz contentIndex emitter
    skips ``draft: true`` entries entirely, so the doc disappears from the
    explorer tree, the graph view, and full-text search on the rendered site.

    Idempotent — running it twice on an already-draft doc is a no-op and
    prints ``<short-id> is already draft``. Use ``brain mark-published`` to
    re-publish.
    """
    _set_draft(id, draft=True)


@app.command(name="mark-published")
def mark_published(id: str = typer.Argument(...)) -> None:
    """Un-quarantine a document: set ``draft=false`` and regenerate its mirror.

    Inverse of ``brain mark-draft``. Idempotent — running it on a doc that
    is already published prints ``<short-id> is already published`` and
    exits 0.
    """
    _set_draft(id, draft=False)


def _set_draft(id_prefix: str, *, draft: bool) -> None:
    """Shared body for ``mark-draft`` / ``mark-published``.

    Resolves ``id_prefix``, no-ops idempotently when the column already
    matches ``draft``, otherwise calls :func:`update_document` with
    ``new_draft=draft`` and ``vault_root=cfg.vault_path`` so the on-disk
    mirror is regenerated with the new ``draft:`` frontmatter line. Echoes
    a one-line confirmation.

    Errors (prefix not found / ambiguous) propagate via
    :func:`_resolve_id` → ``typer.Exit(code=1)``.
    """
    cfg = Config.load()
    target_state_label = "draft" if draft else "published"
    other_state_label = "published" if draft else "draft"
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id_prefix)
        row = conn.execute(
            "SELECT draft FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
        assert row is not None  # _resolve_id confirmed the row exists
        current_draft = bool(row[0])
        label = doc_id[:8]
        if current_draft == draft:
            typer.echo(f"{label} is already {target_state_label}")
            return
        try:
            update_document(
                conn,
                document_id=doc_id,
                new_draft=draft,
                vault_root=cfg.vault_path,
            )
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e
    typer.echo(f"marked {label} as {target_state_label} (was {other_state_label})")


def _rm_unlink_vault_mirror(*, cfg: Config, vault_path_rel: str | None) -> str:
    """Remove the on-disk vault mirror after ``brain rm`` deletes the DB row.

    Returns the suffix appended to the CLI's ``removed <id>`` line. The
    suffix shape is part of the user-facing contract and is asserted by
    ``tests/test_cli_rm.py`` — keep the strings stable across changes.

    - ``vault_path`` NULL → ``" (db only)"`` (e.g., raw ``ingest-stdin`` rows
      that never made it into a vault export).
    - File present + unlinked → ``" (file: <vault_path>)"``.
    - File already absent on disk → ``" (db only, file already gone)"`` so
      the user sees that the row was deleted but the cleanup was a no-op
      (e.g., the user manually removed the mirror first, or a previous
      partial rm).
    """
    if vault_path_rel is None:
        return " (db only)"
    abs_path: Path = cfg.vault_path / vault_path_rel
    if abs_path.exists():
        abs_path.unlink()
        logger.debug("brain rm: unlinked vault mirror %s", abs_path)
        return f" (file: {vault_path_rel})"
    logger.debug(
        "brain rm: vault mirror already gone at %s (skipping unlink)", abs_path
    )
    return " (db only, file already gone)"


# ---------------------------------------------------------------------------
# Vault sub-app commands.
# ---------------------------------------------------------------------------


@vault_app.command("init")
def vault_init(
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Override the configured vault path.",
    ),
) -> None:
    """Create the vault folder + default templates. Idempotent.

    Writes ``_templates/``, ``_attachments/``, ``_ingested/{krisp,slack,gmail,manual}/``,
    and ``daily/`` under the resolved vault path. Drops in default daily/note
    templates and a vault README on first run; subsequent runs leave existing
    files alone (so user edits to templates survive).
    """
    cfg = Config.load()
    target = path.expanduser() if path is not None else cfg.vault_path
    summary = init_vault(target)
    typer.echo(f"vault path:     {summary.vault_path}")
    if summary.created_dirs:
        typer.echo(f"created dirs:   {', '.join(summary.created_dirs)}")
    if summary.existing_dirs:
        typer.echo(f"existing dirs:  {', '.join(summary.existing_dirs)}")
    if summary.written_files:
        typer.echo(f"wrote files:    {', '.join(summary.written_files)}")
    if summary.preserved_files:
        typer.echo(f"left untouched: {', '.join(summary.preserved_files)}")


@vault_app.command("export")
def vault_export(
    to: Path | None = typer.Option(
        None,
        "--to",
        help="Vault folder to write into (default: configured BRAIN_VAULT_PATH).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Write into a non-empty target that wasn't created by this tool.",
    ),
) -> None:
    """One-shot dump of the current DB to a vault folder.

    Writes one ``.md`` per document with YAML frontmatter. Idempotent —
    re-running on the same path is a no-op when nothing changed (compares
    each destination's existing body content_hash against the DB row).
    """
    cfg = Config.load()
    target = to.expanduser() if to is not None else cfg.vault_path
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        try:
            summary = export_vault(conn, vault_path=target, force=force)
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e
    typer.echo(
        f"wrote {summary.written} file(s), "
        f"skipped {summary.skipped}, "
        f"errors {len(summary.errors)}"
    )
    for err in summary.errors:
        typer.secho(f"  error: {err}", fg="red", err=True)
    if summary.errors:
        raise typer.Exit(code=1)


@vault_app.command("sync")
def vault_sync(
    vault: Path | None = typer.Option(
        None,
        "--vault",
        help="Override the configured vault path for this invocation.",
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Delete vault-tier rows whose files vanished (default: warn only).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report planned changes without writing to DB or modifying files.",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help=(
            "Run as a daemon: do one initial sync, then watch the vault "
            "for changes and incrementally re-sync until Ctrl-C."
        ),
    ),
    no_link_rewrite: bool = typer.Option(
        False,
        "--no-link-rewrite",
        help=(
            "Skip the post-sync rewrite of vault-tier wiki-links to "
            "vault-root-relative path form. The DB ``links`` table is still "
            "populated; only on-disk note bodies are left untouched."
        ),
    ),
) -> None:
    """Reconcile the vault folder into the DB.

    Walks every ``.md`` file under the resolved vault path, upserts a
    ``documents`` row per file (creating + assigning a frontmatter ``id``
    on first sight), parses ``[[wiki-links]]`` into the ``links`` and
    ``unresolved_links`` tables, and re-resolves dangling refs at the end
    of the run.

    Default policy on missing files: WARN, not delete. Pass ``--prune`` to
    delete vault-tier rows whose files have vanished. Pass ``--dry-run`` to
    print the planned actions without writing anything (no id assignment,
    no DB writes, no link materialization). Pass ``--watch`` to run an
    initial full sync and then keep watching for filesystem changes,
    incrementally re-syncing affected files until SIGINT/SIGTERM.

    Exit codes:
    - 0 on success (even with warnings, errors, or unresolved links)
    - 2 if the vault path doesn't exist or isn't a directory
    """
    if watch and dry_run:
        # ``--watch`` is for live editing; ``--dry-run`` is for inspecting
        # without writing — combining them serves no use case (the watcher
        # would just spin forever skipping every event), so reject up front.
        raise typer.BadParameter(
            "--watch and --dry-run cannot be combined", param_hint="--watch"
        )

    cfg = Config.load()
    target = vault.expanduser() if vault is not None else cfg.vault_path
    if not target.is_dir():
        typer.secho(
            f"vault path is not a directory: {target}",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)

    embedder = _build_embedder(cfg)

    if watch:
        # Long-running mode. The watcher owns its own connection lifecycle
        # (it needs a fresh psycopg connection in its worker thread, not
        # one borrowed from the CLI's `with connect(...)` block).
        database_url = cfg.database_url
        typer.echo(f"watching {target} (Ctrl-C to stop)")

        def _conn_factory() -> psycopg.Connection[Any]:
            # Use ``connect_raw`` from ``brain.db`` so pgvector adapter
            # registration happens in the watcher's thread the same way
            # it does for every other CLI command. The watcher owns the
            # connection lifetime (closed inside ``run_watcher``).
            return connect_raw(database_url)

        report = run_watcher(
            _conn_factory,
            embedder=embedder,
            config=WatchConfig(
                vault_path=target,
                prune=prune,
                link_rewrite=not no_link_rewrite,
            ),
        )
        typer.echo(f"vault path:     {target}")
        deletion_phrase = (
            f"deleted {report.deleted}" if prune else f"warned {report.warned}"
        )
        typer.echo(
            f"initial sync — created {report.created}, "
            f"updated {report.updated}, "
            f"skipped {report.skipped}, "
            f"{deletion_phrase}, "
            f"links_resolved {report.links_resolved}, "
            f"links_unresolved {report.links_unresolved}, "
            f"links_rewritten {report.links_rewritten}, "
            f"errors {len(report.errors)}"
        )
        if report.id_assigned:
            noun = "file" if report.id_assigned == 1 else "files"
            typer.echo(f"assigned ids to {report.id_assigned} {noun}")
        for path, reason in report.errors:
            typer.secho(f"  error: {path}: {reason}", fg="red", err=True)
        return

    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        report = sync_vault(
            conn,
            embedder=embedder,
            vault_path=target,
            prune=prune,
            dry_run=dry_run,
            link_rewrite=not no_link_rewrite,
        )

    suffix = " (dry-run)" if dry_run else ""
    typer.echo(f"vault path:     {target}{suffix}")
    deletion_phrase = (
        f"deleted {report.deleted}" if prune else f"warned {report.warned}"
    )
    typer.echo(
        f"created {report.created}, "
        f"updated {report.updated}, "
        f"skipped {report.skipped}, "
        f"{deletion_phrase}, "
        f"links_resolved {report.links_resolved}, "
        f"links_unresolved {report.links_unresolved}, "
        f"links_rewritten {report.links_rewritten}, "
        f"errors {len(report.errors)}"
    )
    if report.id_assigned:
        verb = "would assign" if dry_run else "assigned"
        noun = "file" if report.id_assigned == 1 else "files"
        typer.echo(f"{verb} ids to {report.id_assigned} {noun}")
    for path, reason in report.errors:
        typer.secho(f"  error: {path}: {reason}", fg="red", err=True)


@vault_app.command("prune-orphans")
def vault_prune_orphans(
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Actually delete the orphan files. Without this flag, prints "
            "the list (dry-run)."
        ),
    ),
    include_stale: bool = typer.Option(
        False,
        "--include-stale",
        help=(
            "Also delete stale mirror files: those whose frontmatter id "
            "resolves to a row but whose path differs from that row's "
            "``vault_path`` (leftovers from a slug-shape change). Default "
            "off; only true orphans are processed."
        ),
    ),
    vault: Path | None = typer.Option(
        None,
        "--vault",
        help="Override the configured vault path.",
    ),
) -> None:
    """List or delete ``_ingested/`` mirror files whose frontmatter id has no
    matching ``documents`` row (or, with ``--include-stale``, also files
    pointed past by a row whose ``vault_path`` is a different file).

    Default behavior (no ``--apply``) is a dry-run: each candidate is
    printed as ``would delete: <path>`` and a final summary reports the
    count plus the hint to re-run with ``--apply``. With ``--apply`` each
    file is :py:meth:`Path.unlink`'d and the line becomes
    ``deleted: <path>``.

    The command refuses to run if ``<vault>/_ingested`` is missing
    (exit code 2) — that's a fresh / mis-configured vault, and walking it
    would silently no-op which is more confusing than an explicit error.
    Files lacking parseable frontmatter or a string ``id`` key are NEVER
    deleted: :func:`brain.queries.iter_orphan_mirror_files` already
    excludes them so user-authored content under ``_ingested/`` (e.g. the
    init-time README) survives every run.
    """
    cfg = Config.load()
    target = vault.expanduser() if vault is not None else cfg.vault_path
    ingested_dir = target / "_ingested"
    if not ingested_dir.is_dir():
        typer.secho(
            f"_ingested/ not found under vault: {ingested_dir}\n"
            f"  Run `brain vault init` first, or pass --vault.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)

    with connect(cfg.database_url) as conn:
        # Materialize the candidate list before opening write/unlink calls so
        # we don't iterate the tree while mutating it.
        orphans = list(iter_orphan_mirror_files(conn, vault_path=target))
        if include_stale:
            orphans.extend(iter_stale_mirror_files(conn, vault_path=target))

    if not orphans:
        typer.echo("0 orphan files")
        return

    deleted = 0
    for path in orphans:
        if apply:
            try:
                # missing_ok swallows the benign race where a watcher (or a
                # concurrent prune) removes the file between enumeration
                # and unlink. Real I/O failures still raise OSError.
                path.unlink(missing_ok=True)
            except OSError as e:
                typer.secho(f"  failed: {path} — {e}", fg="red", err=True)
                continue
            typer.echo(f"deleted: {path}")
            deleted += 1
        else:
            typer.echo(f"would delete: {path}")

    if apply:
        typer.echo(f"deleted: {deleted}")
    else:
        typer.echo(
            f"{len(orphans)} orphan file(s) (dry-run; pass --apply to remove)"
        )


# ---------------------------------------------------------------------------
# Quartz render — `brain vault render`.
#
# Thin wrapper around `npx quartz build`. Quartz is purpose-built for
# Obsidian-style vaults; we orchestrate it rather than reinventing
# backlinks / graph view / search in Python. The user installs Quartz
# themselves via `npx quartz create` (one-time, per vault); this
# command just shells out to the binary.
# ---------------------------------------------------------------------------


# Hard ceiling on the build subprocess. Quartz on a small vault runs in
# seconds; on a 10K-note vault still well under a minute. Five minutes
# is the "your config is broken" threshold — past that we kill the
# process so a runaway plugin can't lock the user's terminal forever.
_QUARTZ_BUILD_TIMEOUT_S = 300


def _resolve_render_to(to: Path, cwd: Path) -> Path:
    """Reject `--to` paths that escape the cwd via `..` traversal.

    Mirrors the path-traversal guard `_assert_within_vault` applies to
    `--folder` in the authoring commands. Relative paths are
    interpreted against ``cwd`` (so ``--to dist`` lands at
    ``<cwd>/dist``); absolute paths are honored verbatim. Either way
    the resolved output directory must live under ``cwd`` — an
    explicit ``--to ../escape`` or absolute path that points elsewhere
    is rejected.
    """
    expanded = to.expanduser()
    cwd_resolved = cwd.resolve()
    # Resolve relative paths against the supplied cwd, NOT the process
    # cwd — tests pass ``tmp_path / "cwd"`` here even though the actual
    # process cwd is something else. Absolute paths resolve as-is.
    resolved = (
        expanded.resolve()
        if expanded.is_absolute()
        else (cwd_resolved / expanded).resolve()
    )
    try:
        resolved.relative_to(cwd_resolved)
    except ValueError as e:
        raise typer.BadParameter(
            f"--to must stay within the current working directory; "
            f"got a path that resolves outside {cwd_resolved}",
            param_hint="--to",
        ) from e
    return resolved


def _vault_has_markdown(vault_path: Path) -> bool:
    """True iff the vault has at least one `.md` file anywhere."""
    return any(p.is_file() for p in vault_path.rglob("*.md"))


def _check_quartz_workspace(quartz_dir: Path) -> None:
    """Verify the Quartz workspace exists with the files Quartz expects.

    A well-formed `npx quartz create`-scaffolded directory always has
    `package.json` (Quartz's own package metadata) and `quartz.config.ts`
    (the user-editable config). Their absence is the single most common
    setup failure, and the error message has to walk the user back
    through the one-time setup — anything less and they get a confusing
    npx stack trace.
    """
    if not quartz_dir.is_dir():
        # Print the multi-line setup hint to stderr ourselves before
        # raising — typer's BadParameter wraps long messages inside a
        # box, which mangles them; a plain stderr write keeps the
        # `npx quartz create` string contiguous so users (and tests)
        # can grep it.
        typer.secho(
            f"Quartz workspace not found at {quartz_dir}.\n"
            f"  Run `npx quartz create` in your vault, then re-run.\n"
            f"  A sample config lives at the brain repo root: "
            f"`quartz.config.ts`.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)
    missing = [
        name
        for name in ("package.json", "quartz.config.ts")
        if not (quartz_dir / name).is_file()
    ]
    if missing:
        typer.secho(
            f"Quartz workspace at {quartz_dir} is missing: "
            f"{', '.join(missing)}.\n"
            f"  Run `npx quartz create` to scaffold a fresh workspace, "
            f"then copy the sample `quartz.config.ts` from the brain "
            f"repo root if needed.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)


@vault_app.command("render")
def vault_render(
    to: Path = typer.Option(
        Path("./dist"),
        "--to",
        help="Output directory for the rendered HTML site (default: ./dist).",
    ),
    vault: Path | None = typer.Option(
        None,
        "--vault",
        help="Override the configured vault path.",
    ),
    quartz_dir: Path | None = typer.Option(
        None,
        "--quartz-dir",
        help="Quartz workspace directory (default: <vault>/.quartz).",
    ),
    no_build: bool = typer.Option(
        False,
        "--no-build",
        help="Verify the Quartz workspace is set up without running the build.",
    ),
    overlay: bool = typer.Option(
        True,
        "--overlay/--no-overlay",
        help=(
            "Copy `quartz_overrides/` over the Quartz workspace before "
            "building. Use `--no-overlay` to skip and use whatever is "
            "already in the workspace."
        ),
    ),
    print_overlay: bool = typer.Option(
        False,
        "--print-overlay",
        help=(
            "Print the overlay plan (file pairs + rename status) and exit "
            "without copying or building. Takes precedence over "
            "`--overlay/--no-overlay`."
        ),
    ),
) -> None:
    """Render the vault to a static HTML site via Quartz.

    Shells out to `npx quartz build --directory <vault> --output <to>`.
    The user is responsible for one-time Quartz setup (see the README's
    "Wiki rendering (Quartz)" section): scaffold a workspace at
    `<vault>/.quartz/` with `npx quartz create`, then copy the sample
    `quartz.config.ts` from the brain repo root.

    Before the build, the overlay step copies `quartz_overrides/` over
    the Quartz workspace (custom Graph component, contentIndex emitter,
    etc.). Use `--no-overlay` to skip, or `--print-overlay` to see what
    would be copied without applying.

    Honours stdout/stderr passthrough so the user sees Quartz's
    progress live. Propagates a non-zero exit code from npx as exit 1.
    """
    cfg = Config.load()
    target_vault = vault.expanduser() if vault is not None else cfg.vault_path
    if not target_vault.is_dir():
        typer.secho(
            f"vault path is not a directory: {target_vault}",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)
    if not _vault_has_markdown(target_vault):
        # No .md files = nothing for Quartz to render. We don't try to
        # be clever here (e.g. by emitting a "you might want to run
        # `brain vault export` first" hint) — the user knows what they
        # have; we just bail clearly.
        typer.secho(
            f"vault at {target_vault} has no .md files — nothing to render",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)

    output_dir = _resolve_render_to(to, Path.cwd())

    workspace = (
        quartz_dir.expanduser() if quartz_dir is not None else target_vault / ".quartz"
    )
    _check_quartz_workspace(workspace)

    try:
        plan = plan_overlay(_brain_repo_root(), workspace)
    except OverlayError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(code=2) from e

    if print_overlay:
        typer.echo(f"overlay plan for {workspace}:")
        if plan.rename is not None:
            src, dest = plan.rename
            typer.echo(f"  rename: {src} → {dest}")
        elif plan.rename_state == "already_applied":
            typer.echo(
                "  rename: already applied "
                "(_upstreamContentIndex.tsx present, no upstream contentIndex.tsx)"
            )
        else:
            typer.echo(
                "  rename: skipped — neither contentIndex.tsx nor "
                "_upstreamContentIndex.tsx exists; the brain wrapper "
                "will fail at build time until Quartz is reinstalled"
            )
        for src, dest in plan.pairs:
            typer.echo(f"  copy:   {src} → {dest}")
        raise typer.Exit(code=0)

    if overlay:
        try:
            copied = apply_overlay(plan)
        except OverlayError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=2) from e
        if plan.rename is not None:
            typer.echo(
                "overlay: renamed contentIndex.tsx → _upstreamContentIndex.tsx"
            )
        elif plan.rename_state == "already_applied":
            typer.echo("overlay: rename already applied")
        else:
            typer.echo(
                "overlay: rename skipped — upstream contentIndex.tsx not "
                "found (the brain wrapper will fail at build time)"
            )
        typer.echo(f"overlay: copied {len(copied)} files into {workspace}")
    else:
        typer.echo(
            "overlay: skipped (--no-overlay) — using whatever is already "
            "in place"
        )

    if no_build:
        typer.echo(f"quartz workspace OK at {workspace}")
        return

    # `npx quartz build` reads its config from cwd, hence cwd=workspace.
    # `--directory` points it at the vault content; `--output` controls
    # where it writes the rendered site.
    args = [
        "npx",
        "quartz",
        "build",
        "--directory",
        str(target_vault),
        "--output",
        str(output_dir),
    ]
    typer.echo(f"running: {' '.join(args)} (cwd={workspace})")
    try:
        completed = subprocess.run(  # noqa: S603 — args are list-form, no shell
            args,
            cwd=str(workspace),
            check=False,
            timeout=_QUARTZ_BUILD_TIMEOUT_S,
        )
    except FileNotFoundError as e:
        # `npx` itself isn't on PATH. `brain doctor` warns about this
        # ahead of time, but we still surface a friendly error here in
        # case the user skipped it.
        typer.secho(
            f"npx not found ({e}); install Node.js (https://nodejs.org/) "
            "and re-run.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1) from e
    except subprocess.TimeoutExpired as e:
        typer.secho(
            f"quartz build exceeded {_QUARTZ_BUILD_TIMEOUT_S}s — likely a "
            "misconfigured plugin or a runaway transformer; aborting.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1) from e

    if completed.returncode != 0:
        # npx already streamed its error output to the inherited
        # stderr; we just need to propagate the failure. Map any
        # non-zero exit to 1 (the user shells will see "render
        # failed", not the raw npx code).
        typer.secho(
            f"quartz build failed with exit code {completed.returncode}",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(
        f"rendered to {output_dir} "
        f"(open {output_dir / 'index.html'} or serve with "
        f"`python -m http.server` from there)"
    )


# ---------------------------------------------------------------------------
# brain vault relink-derived — full-corpus directory + derived-links rebuild.
#
# One-shot maintenance command: rescan every Gmail document into the
# directory, refresh Calendar/Contacts via gws (best-effort), and rebuild
# every derived_links edge across the Gmail+Krisp corpus. Idempotent.
# ---------------------------------------------------------------------------


def _backfill_krisp_participant_keys(conn: psycopg.Connection[Any]) -> int:
    """Re-populate ``metadata['_participant_keys']`` for every Krisp doc from its body.

    Backfills docs ingested before B.3 (which added the pre-insert hook in
    :func:`brain.ingest._apply_pre_insert_metadata`). For each Krisp doc,
    parses speaker labels via :func:`extract_krisp_speakers` over the stored
    body and writes the sorted, normalized list back into
    ``documents.metadata['_participant_keys']``. Returns the count of docs
    updated.

    Idempotent — re-running on a doc that already has correct keys is a
    no-op aside from the UPDATE itself; running on stale keys overwrites
    them to match the current body. Wrapped in one transaction so a partial
    failure leaves the corpus in its pre-backfill state.

    This step does NOT depend on ``gws`` — it works purely on already-stored
    document content. It's the missing piece that lets R3
    (``same_day_participant``) fire on the existing Krisp corpus.
    """
    rows = conn.execute(
        """
        SELECT d.id::text, d.content, d.metadata
        FROM documents d
        JOIN sources s ON s.id = d.source_id
        WHERE s.kind = 'krisp'
        """
    ).fetchall()
    updated = 0
    with conn.transaction():
        for doc_id, content, metadata in rows:
            keys = sorted(extract_krisp_speakers(content or ""))
            new_metadata = dict(metadata or {})
            new_metadata["_participant_keys"] = keys
            conn.execute(
                "UPDATE documents SET metadata = %s::jsonb WHERE id = %s",
                (_json.dumps(new_metadata), doc_id),
            )
            updated += 1
    return updated


def _linkable_corpus_ids(conn: psycopg.Connection[Any]) -> set[str]:
    """Return every Gmail/Krisp document id as a set of strings.

    The linker only operates on these two source kinds — manual / vault docs
    don't carry the metadata shapes the rules read.
    """
    rows = conn.execute(
        """
        SELECT d.id::text
        FROM documents d
        JOIN sources s ON s.id = d.source_id
        WHERE s.kind IN ('gmail', 'krisp')
        """
    ).fetchall()
    return {str(r[0]) for r in rows}


def _directory_counts_by_source(
    conn: psycopg.Connection[Any],
) -> list[tuple[str, int]]:
    """Return ``[(source, count), ...]`` ordered by count desc."""
    rows = conn.execute(
        """
        SELECT source, count(*)::int
        FROM directory_entries
        GROUP BY source
        ORDER BY count(*) DESC, source ASC
        """
    ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def _derived_counts_by_rule(
    conn: psycopg.Connection[Any],
) -> list[tuple[str, int]]:
    """Return ``[(rule, count), ...]`` ordered by count desc."""
    rows = conn.execute(
        """
        SELECT rule, count(*)::int
        FROM derived_links
        GROUP BY rule
        ORDER BY count(*) DESC, rule ASC
        """
    ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


@vault_app.command("relink-derived")
def vault_relink_derived() -> None:
    """Full-corpus directory rebuild + derived-links rebuild.

    Four steps, all against a single Postgres connection:

    1. **Gmail directory rescan.** Walks every Gmail document and upserts
       every ``(display_name, email)`` pair from its ``from``/``to``
       headers into ``directory_entries`` with ``source='gmail'``.
    1.5. **Krisp ``_participant_keys`` backfill.** Re-derives the
       ``metadata._participant_keys`` field from every Krisp doc's stored
       body, so docs ingested before B.3's pre-insert hook land in the
       linker pass with their speakers populated (otherwise R3 has nothing
       to match on for those rows).
    2. **Calendar + Contacts refresh** via the ``gws`` CLI. Calendar is
       windowed: ``since`` = the stored ``last_refreshed_at`` from
       ``directory_refresh_state``, or year-start if no row exists; ``until``
       = now. Contacts is always a full refresh (this command is the user
       explicitly asking for one — the 24h throttle from incremental Krisp
       ingest doesn't apply). Both refreshes degrade soft: a missing ``gws``
       binary or a transient subprocess error logs a warning and the command
       still completes.
    3. **Linker pass** over the full Gmail+Krisp corpus. Passes every
       linkable doc id as a single ``rebuild_derived_for`` call so cross-doc
       pairs aren't missed by per-batch DELETE+INSERT semantics — we choose
       Option B from Task B.6 (single full-corpus call) over Option A
       (delete-all + small batches) because the corpus is small enough
       (~500 docs) to fit comfortably in memory.

    Idempotent: running twice produces the same final state because
    ``rebuild_derived_for``'s DELETE+INSERT scopes to the touched docs (and
    we touch every linkable doc on this command), and the Krisp backfill
    overwrites ``_participant_keys`` deterministically from the body.
    """
    cfg = Config.load()
    started_at = _time.perf_counter()

    with connect(cfg.database_url) as conn:
        conn.autocommit = True

        # Step 1: full Gmail directory rescan.
        typer.echo("Refreshing directory...")
        gmail_docs_seen, gmail_pairs = rescan_gmail_directory(conn)
        typer.echo(
            f"  - Gmail headers: {gmail_pairs} pairs from {gmail_docs_seen} docs"
        )

        # Step 1.5: Backfill Krisp _participant_keys from already-stored bodies.
        # Docs ingested before B.3's pre-insert hook landed have an empty (or
        # missing) ``metadata._participant_keys`` field, which means R3
        # (same_day_participant) has nothing to compare against — it silently
        # degrades to zero edges from those docs. This step is the historical
        # backfill the original spec promised on the first ``relink-derived``
        # run; it's body-driven so it works without a live ``gws``.
        typer.echo("Backfilling Krisp participant keys...")
        krisp_updated = _backfill_krisp_participant_keys(conn)
        typer.echo(f"  - Krisp docs: {krisp_updated} backfilled")

        # Step 2: Calendar + Contacts refresh via gws (best-effort).
        now = datetime.now(tz=UTC)
        cal_row = conn.execute(
            "SELECT last_refreshed_at FROM directory_refresh_state "
            "WHERE source = 'calendar'"
        ).fetchone()
        if cal_row is not None and cal_row[0] is not None:
            since = cal_row[0]
        else:
            since = datetime(now.year, 1, 1, tzinfo=UTC)
        events_seen = refresh_calendar(
            conn, since=since, until=now, runner=real_gws_runner
        )
        typer.echo(
            f"  - Calendar: {events_seen} events seen since "
            f"{since.date().isoformat()}"
        )
        contacts_seen = refresh_contacts(conn, runner=real_gws_runner)
        typer.echo(f"  - Contacts: {contacts_seen} contacts seen")

        # Step 3: linker pass over the full Gmail+Krisp corpus.
        #
        # Implementation choice (per Task B.6): Option B — pass ALL linkable
        # corpus ids as a single call to ``rebuild_derived_for`` so the
        # DELETE+INSERT inside the runner cleanly rebuilds every edge in one
        # transaction. Option A (delete-all + small batches) would also be
        # correct but is only worth the complexity at scales where a single
        # set blows out memory; this corpus is ~500 docs.
        typer.echo("Rebuilding derived edges...")
        corpus_ids = _linkable_corpus_ids(conn)
        if not corpus_ids:
            typer.echo("No linkable documents to process.")
        else:
            directory = DirectoryStore(conn)
            # ``rebuild_derived_for`` returns ``(inserted_count, affected_ids)``;
            # the affected-ids set drives the fence renderer in step 4.
            inserted, affected_ids = rebuild_derived_for(
                conn, corpus_ids, directory=directory
            )
            typer.echo(f"  - Touched docs: {len(corpus_ids)}")
            typer.echo(f"  - Inserted edges: {inserted}")
            typer.echo(f"  - Affected docs: {len(affected_ids)}")

            # Step 4 (Phase D): regenerate the fenced "Related" section in
            # every affected ``_ingested/`` file so Quartz's ``/graph`` view
            # picks up the edges we just rebuilt. The renderer skips
            # vault-tier rows and missing mirror files silently; the count
            # is the number of files actually written. Q4=b semantics —
            # writes happen even when fence content is byte-identical, so
            # ``Fence files rewritten`` matches the count of ingested-tier
            # affected docs that have a mirror on disk.
            fences_written = rewrite_derived_fences(
                conn, affected_ids, vault_path=cfg.vault_path
            )
            typer.echo(f"  - Fence files rewritten: {fences_written}")

        # Step 4: Rich summary — directory by source + derived_links by rule.
        directory_counts = _directory_counts_by_source(conn)
        derived_counts = _derived_counts_by_rule(conn)

    elapsed = _time.perf_counter() - started_at

    if directory_counts:
        directory_table = Table(title="Directory entries by source")
        directory_table.add_column("Source", style="cyan")
        directory_table.add_column("Rows", justify="right")
        for source, count in directory_counts:
            directory_table.add_row(source, str(count))
        console.print(directory_table)

    if derived_counts:
        derived_table = Table(title="Derived links by rule")
        derived_table.add_column("Rule", style="cyan")
        derived_table.add_column("Edges", justify="right")
        for rule, count in derived_counts:
            derived_table.add_row(rule, str(count))
        console.print(derived_table)

    typer.echo(f"Done in {elapsed:.1f}s.")


# ---------------------------------------------------------------------------
# brain vault directory refresh / show
#
# Diagnostic CLIs for the name↔email directory: ``refresh`` rebuilds it
# from every source (Gmail rescan + YTD calendar + full contacts) without
# touching ``derived_links`` (use ``relink-derived`` to also rebuild
# edges); ``show`` prints the current directory rows as a Rich table.
# ---------------------------------------------------------------------------


# Mirrors :data:`brain.vault.derived_links.directory._VALID_SOURCES`. We
# duplicate the literal here (instead of importing the private constant)
# so the CLI's ``--source`` validation surface stays stable independent
# of any internal refactor of the module-private set.
_DIRECTORY_VALID_SOURCES: frozenset[str] = frozenset(
    {"gmail", "calendar", "contacts", "people_yml"}
)


@vault_directory_app.command("refresh")
def vault_directory_refresh() -> None:
    """Full directory rebuild from all sources — Gmail rescan + Calendar + Contacts.

    Three steps:

    1. **Gmail rescan.** Walks every Gmail document and upserts
       ``(display_name, email)`` pairs from ``from`` / ``to`` headers.
    2. **Calendar refresh** via ``gws``. Window: ``since`` = the stored
       ``directory_refresh_state.calendar.last_refreshed_at`` if present,
       otherwise year-start; ``until`` = now.
    3. **Contacts refresh** via ``gws`` (full refresh; the 24h throttle
       from incremental Krisp ingest does not apply here — the user has
       explicitly asked for a refresh).

    Unlike ``relink-derived``, this command does **not** touch
    ``derived_links``. It's the surgical "I edited ``_people.yml`` /
    pulled new gws contacts and want the directory current" command,
    decoupled from the heavy linker pass.

    Soft-fails on missing ``gws``: a warning is logged via
    ``refresh_calendar`` / ``refresh_contacts`` and the command still
    exits 0.
    """
    cfg = Config.load()
    started_at = _time.perf_counter()

    with connect(cfg.database_url) as conn:
        conn.autocommit = True

        typer.echo("Refreshing directory...")
        gmail_docs_seen, gmail_pairs = rescan_gmail_directory(conn)
        typer.echo(
            f"  - Gmail headers: {gmail_pairs} pairs from {gmail_docs_seen} docs"
        )

        now = datetime.now(tz=UTC)
        cal_row = conn.execute(
            "SELECT last_refreshed_at FROM directory_refresh_state "
            "WHERE source = 'calendar'"
        ).fetchone()
        if cal_row is not None and cal_row[0] is not None:
            since = cal_row[0]
        else:
            since = datetime(now.year, 1, 1, tzinfo=UTC)
        events_seen = refresh_calendar(
            conn, since=since, until=now, runner=real_gws_runner
        )
        typer.echo(
            f"  - Calendar: {events_seen} events seen since "
            f"{since.date().isoformat()}"
        )
        contacts_seen = refresh_contacts(conn, runner=real_gws_runner)
        typer.echo(f"  - Contacts: {contacts_seen} contacts seen")

        directory_counts = _directory_counts_by_source(conn)

    elapsed = _time.perf_counter() - started_at

    if directory_counts:
        directory_table = Table(title="Directory entries by source")
        directory_table.add_column("Source", style="cyan")
        directory_table.add_column("Rows", justify="right")
        for source, count in directory_counts:
            directory_table.add_row(source, str(count))
        console.print(directory_table)
    else:
        # Friendly message when the directory is genuinely empty (fresh
        # corpus, no Gmail docs, gws unavailable). Distinct from the
        # source-grouped table so users don't read empty space as a bug.
        typer.echo("Directory is empty — no entries from any source.")

    typer.echo(f"Done in {elapsed:.1f}s.")


@vault_directory_app.command("show")
def vault_directory_show(
    source: str | None = typer.Option(
        None,
        "--source",
        help=(
            "Filter to one of: gmail, calendar, contacts, people_yml. "
            "Omit to show every source."
        ),
    ),
) -> None:
    """Print the directory entries grouped by source as a Rich table.

    Columns: source, email, display_name, count, first_seen, last_seen.
    Rows are ordered by ``(source, email)`` for stable, scannable output.
    With ``--source S`` only that source's rows are shown; an unknown
    source name exits non-zero with the list of valid values.
    """
    if source is not None and source not in _DIRECTORY_VALID_SOURCES:
        valid = ", ".join(sorted(_DIRECTORY_VALID_SOURCES))
        typer.echo(
            f"error: invalid --source {source!r}; expected one of: {valid}",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        if source is None:
            rows = conn.execute(
                """
                SELECT source, email, display_name, occurrence_count,
                       first_seen_at, last_seen_at
                FROM directory_entries
                ORDER BY source ASC, email ASC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT source, email, display_name, occurrence_count,
                       first_seen_at, last_seen_at
                FROM directory_entries
                WHERE source = %s
                ORDER BY email ASC
                """,
                (source,),
            ).fetchall()

    if not rows:
        if source is None:
            typer.echo("Directory is empty — no entries.")
        else:
            typer.echo(f"No directory entries with source={source!r}.")
        return

    title = (
        "Directory entries"
        if source is None
        else f"Directory entries (source={source})"
    )
    table = Table(title=title)
    table.add_column("Source", style="cyan")
    table.add_column("Email")
    table.add_column("Name")
    table.add_column("Count", justify="right")
    table.add_column("First seen")
    table.add_column("Last seen")
    for src, email, display_name, count, first_seen, last_seen in rows:
        table.add_row(
            str(src),
            str(email),
            str(display_name) if display_name else "",
            str(count),
            first_seen.isoformat(timespec="seconds")
            if first_seen is not None
            else "",
            last_seen.isoformat(timespec="seconds")
            if last_seen is not None
            else "",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Authoring commands: brain note new / brain note rename / brain daily.
# ---------------------------------------------------------------------------


def _resolve_vault(override: Path | None, cfg: Config) -> Path:
    """Pick the vault path: ``--vault`` flag wins, otherwise ``cfg.vault_path``.

    Centralised so every authoring command resolves identically and so
    ``--vault`` semantics stay consistent (expanduser applied; no other
    normalization).
    """
    return override.expanduser() if override is not None else cfg.vault_path


def _assert_within_vault(target: Path, vault_path: Path, *, label: str) -> None:
    """Reject ``target`` if it resolves outside ``vault_path``.

    Centralized so every authoring command applies the same path-traversal
    guard (``--folder ../../etc`` and similar). ``label`` is interpolated
    into the error message so the user knows which option to fix
    (``--folder``, ``--date``, etc.).

    Resolves both sides before comparing — the vault root is followed
    through symlinks too, so a user who symlinks their vault to an iCloud
    path still gets correct rejection (no false positives) when the
    resolved target stays inside the resolved vault root.
    """
    try:
        target.resolve().relative_to(vault_path.resolve())
    except ValueError as e:
        raise typer.BadParameter(
            f"{label} must stay within the vault; "
            f"got a path that resolves outside {vault_path}"
        ) from e


def _ensure_template(vault_path: Path, name: str) -> Path:
    """Resolve ``<vault>/_templates/<name>.md`` or raise BadParameter.

    The error message tells the user exactly how to recover — either run
    ``brain vault init`` (no ``_templates/`` at all) or pick a template
    name that exists.
    """
    templates_dir = vault_path / "_templates"
    if not templates_dir.is_dir():
        raise typer.BadParameter(
            f"vault has no _templates/ directory at {templates_dir} — "
            "run `brain vault init` first"
        )
    target = templates_dir / f"{name}.md"
    if not target.is_file():
        available = ", ".join(list_template_names(vault_path)) or "(none)"
        raise typer.BadParameter(
            f"template '{name}' not found at {target}; available: {available}"
        )
    return target


def _build_note_text(
    template_text: str,
    *,
    title: str,
    tags: list[str],
    today: date_cls,
    now: datetime,
) -> tuple[str, str]:
    """Render a template + force the brain-canonical frontmatter fields.

    Returns ``(file_text, document_id)``. The template's body is preserved
    verbatim; only frontmatter is rewritten so the brain-managed fields
    (``id``, ``title``, ``created``, ``updated``, ``kind``, ``tags``) are
    authoritative regardless of what the template author wrote.

    A user-template ``title:`` line is intentionally ignored — the CLI's
    ``<title>`` argument wins. That's the contract: if you wanted the
    template to control title, you'd be using a daily template (which
    derives title from the date passed in via ``vars``).
    """
    rendered = render_template(
        template_text,
        {
            "title": title,
            "date": today.isoformat(),
            "datetime": now.isoformat(timespec="seconds"),
            "slug": slugify(title),
        },
    )

    # Try to parse the rendered template's frontmatter; if it's malformed or
    # missing entirely, build a fresh header. Either way the brain-canonical
    # fields are forced — the template's body is what we preserve.
    try:
        existing_fields, body = parse_frontmatter(rendered)
    except (ValueError, yaml.YAMLError):
        # Per the spec's risk: a malformed template shouldn't crash. Fall
        # back to a fresh frontmatter + the raw rendered text as the body.
        existing_fields = {}
        body = rendered

    document_id = str(uuid.uuid4())
    iso_now = now.isoformat(timespec="seconds")
    fields: dict[str, Any] = dict(existing_fields)
    # Brain-managed fields override the template's choices in a fixed order
    # so frontmatter ordering is stable across runs.
    fields["id"] = document_id
    fields["title"] = title
    fields["created"] = iso_now
    fields["updated"] = iso_now
    fields["kind"] = "vault"
    if tags:
        fields["tags"] = list(tags)
    elif "tags" not in fields:
        fields["tags"] = []

    return dump_frontmatter(fields, body), document_id


def _run_post_write_editor_and_sync(
    cfg: Config, *, vault_path: Path, file_path: Path
) -> SyncReport | None:
    """Open ``$EDITOR`` on ``file_path`` then re-sync. Returns the report.

    Returns ``None`` if the user's editor exited non-zero (the file is
    left in place; sync is skipped). The DB connection is opened ONLY for
    the sync — it's never held across the editor blocking call.

    **Sync errors are surfaced to stderr here**, not silently returned.
    Callers don't need to (and historically didn't) re-print them. We
    deliberately do NOT raise ``typer.Exit`` on a sync error — the file
    is on disk, a future ``brain vault sync`` will pick it up — but the
    user always sees the error message so the divergence isn't invisible.
    """
    try:
        rc = run_editor_on(file_path)
    except RawEditorError as e:
        typer.secho(str(e), fg="red", err=True)
        return None
    if rc != 0:
        typer.secho(
            "editor exited non-zero — file kept; "
            "run `brain vault sync` to index later",
            fg="yellow",
            err=True,
        )
        return None
    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        report = sync_one_file(
            conn,
            embedder=embedder,
            vault_path=vault_path,
            file_path=file_path,
        )
    # Single source of truth for post-edit error reporting — every authoring
    # command that uses this helper inherits the contract automatically.
    for err_path, reason in report.errors:
        typer.secho(
            f"post-edit sync error: {err_path}: {reason}",
            fg="red",
            err=True,
        )
    return report


@note_app.command("new")
def note_new(
    title: str = typer.Argument(..., help="Note title (used for frontmatter + slug)."),
    folder: str = typer.Option(
        "",
        "--folder",
        "-f",
        help="Subdirectory under the vault root (default: vault root).",
    ),
    template: str = typer.Option(
        "note",
        "--template",
        "-T",
        help="Template name in _templates/ (default: 'note').",
    ),
    tag: list[str] = typer.Option(
        [], "--tag", "-t", help="Initial tag(s) for the note."
    ),
    no_edit: bool = typer.Option(
        False, "--no-edit", help="Skip launching $EDITOR after the file is written."
    ),
    vault: Path | None = typer.Option(
        None, "--vault", help="Override the configured vault path."
    ),
) -> None:
    """Create a new vault note from a template.

    Resolves ``<vault>/<folder>/<slug(title)>.md``. Errors if the file
    already exists (use ``brain edit <prefix>`` to modify an existing note).
    Renders ``_templates/<template>.md`` with ``{{title}}`` / ``{{date}}`` /
    ``{{datetime}}`` / ``{{slug}}`` substitutions, forces the
    brain-canonical frontmatter (id, title, created, updated, kind, tags),
    writes the file, runs a single-file sync, and (unless ``--no-edit``)
    opens ``$EDITOR`` then re-syncs on exit.
    """
    cfg = Config.load()
    vault_path = _resolve_vault(vault, cfg)
    template_path = _ensure_template(vault_path, template)
    template_text = template_path.read_text(encoding="utf-8")

    slug = slugify(title)
    target_relative = Path(folder) / f"{slug}.md" if folder else Path(f"{slug}.md")
    target = vault_path / target_relative
    # Guard against ``--folder ../../etc`` and similar — we'd otherwise
    # write outside the vault BEFORE sync ever runs and noticed.
    _assert_within_vault(target, vault_path, label="--folder")
    if target.exists():
        typer.secho(
            f"note already exists at {target_relative.as_posix()}; "
            f"use `brain edit <prefix>` to modify it",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)

    now = datetime.now()
    today = now.date()
    file_text, document_id = _build_note_text(
        template_text,
        title=title,
        tags=list(tag),
        today=today,
        now=now,
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(file_text, encoding="utf-8")

    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        sync_report = sync_one_file(
            conn,
            embedder=embedder,
            vault_path=vault_path,
            file_path=target,
        )
    if sync_report.errors:
        for path, reason in sync_report.errors:
            typer.secho(f"sync error: {path}: {reason}", fg="red", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"created {target_relative.as_posix()} (id={document_id[:8]})"
    )

    if no_edit:
        return
    _run_post_write_editor_and_sync(
        cfg, vault_path=vault_path, file_path=target
    )


@app.command()
def daily(
    date: str | None = typer.Option(
        None, "--date", help="ISO date (YYYY-MM-DD). Defaults to today (local time)."
    ),
    no_edit: bool = typer.Option(
        False, "--no-edit", help="Skip launching $EDITOR."
    ),
    vault: Path | None = typer.Option(
        None, "--vault", help="Override the configured vault path."
    ),
) -> None:
    """Open or create today's daily note.

    The path is ``<vault>/daily/<YYYY>/<YYYY-MM-DD>.md``. Idempotent — if the
    file already exists, it's opened in ``$EDITOR`` (and re-synced on exit).
    Uses ``_templates/daily.md`` to render new files; ``{{date}}`` /
    ``{{datetime}}`` are populated from the resolved date.

    Date defaults to today's local date — if you cross midnight while
    typing, you may want to pin it with ``--date`` to avoid getting the
    next-day file.
    """
    if date is not None:
        try:
            target_date = date_cls.fromisoformat(date)
        except ValueError as e:
            raise typer.BadParameter(
                f"--date must be YYYY-MM-DD ({e})"
            ) from e
    else:
        target_date = date_cls.today()

    cfg = Config.load()
    vault_path = _resolve_vault(vault, cfg)

    iso_date = target_date.isoformat()
    year_folder = f"{target_date.year:04d}"
    target_relative = Path("daily") / year_folder / f"{iso_date}.md"
    target = vault_path / target_relative
    # Defensive: the path is constructed internally so traversal isn't
    # currently possible, but a future ``--folder`` flag (or a date format
    # change) would silently break this contract. Keep the guard.
    _assert_within_vault(target, vault_path, label="--date")

    if target.is_file():
        typer.echo(f"opened {target_relative.as_posix()} (existing)")
        # P4.1: refresh the index even on the "existing" path so a user
        # who's just deleted an old daily (or migrated their vault) can
        # re-run ``brain daily`` to repair the index without touching
        # anything else. Idempotent — same dailies on disk ⇒ same
        # ``daily/index.md`` byte-for-byte ⇒ no rewrite, no sync churn.
        _refresh_daily_index(cfg, vault_path)
        if no_edit:
            return
        _run_post_write_editor_and_sync(
            cfg, vault_path=vault_path, file_path=target
        )
        return

    template_path = _ensure_template(vault_path, "daily")
    template_text = template_path.read_text(encoding="utf-8")

    # ``now`` stamps ``created`` / ``updated`` (real wall clock when the note
    # was made). ``today`` is what populates ``{{date}}`` in the template
    # (the date the note represents — possibly past/future via --date).
    now = datetime.now()
    file_text, _document_id = _build_note_text(
        template_text,
        title=iso_date,
        tags=[],
        today=target_date,
        now=now,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(file_text, encoding="utf-8")

    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        sync_report = sync_one_file(
            conn,
            embedder=embedder,
            vault_path=vault_path,
            file_path=target,
        )
    if sync_report.errors:
        for path, reason in sync_report.errors:
            typer.secho(f"sync error: {path}: {reason}", fg="red", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"created {target_relative.as_posix()}")
    # P4.1: regen the daily index now that today's note exists. The
    # index is a brain-managed bullet list of every daily; without
    # this step the home page's "Daily notes" door is a 404. Sync the
    # generated file so the indexed row reflects the new bullet list.
    _refresh_daily_index(cfg, vault_path)
    if no_edit:
        return
    _run_post_write_editor_and_sync(
        cfg, vault_path=vault_path, file_path=target
    )


def _refresh_daily_index(cfg: Config, vault_path: Path) -> None:
    """Regen ``<vault>/daily/index.md`` and sync it through the DB.

    Co-located with the ``daily`` command because both write paths
    (existing-note + fresh-note) call into it. Errors during the
    DB sync are surfaced to stderr but never raised — the index file
    is on disk already, a future ``brain vault sync`` will pick it
    up. Failing the whole command on a sync hiccup would block the
    user's primary action (creating today's daily) for a secondary
    bookkeeping concern.
    """
    if not regenerate_daily_index(vault_path):
        return
    embedder = _build_embedder(cfg)
    index_path = vault_path / "daily" / "index.md"
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        report = sync_one_file(
            conn,
            embedder=embedder,
            vault_path=vault_path,
            file_path=index_path,
        )
    for path, reason in report.errors:
        typer.secho(
            f"daily index sync error: {path}: {reason}",
            fg="yellow",
            err=True,
        )


def _print_rename_plan(op: RenameOp, vault_path: Path) -> None:
    """Pretty-print a :class:`RenameOp` for ``--dry-run`` output."""
    moved = op.new_path.resolve() != op.old_path.resolve()
    if moved:
        old_rel = op.old_path.resolve().relative_to(vault_path.resolve())
        new_rel = op.new_path.resolve().relative_to(vault_path.resolve())
        typer.echo(
            f"would rename {old_rel.as_posix()} → {new_rel.as_posix()}"
        )
    else:
        typer.echo(f"would update title: {op.old_title!r} → {op.new_title!r}")
    if not op.references:
        typer.echo("no references to rewrite")
        return
    file_count = len({r.file_path for r in op.references})
    typer.echo(
        f"would rewrite {len(op.references)} reference(s) "
        f"in {file_count} file(s):"
    )
    for ref in op.references:
        rel = ref.file_path.resolve().relative_to(vault_path.resolve())
        typer.echo(
            f"  {rel.as_posix()}:{ref.line_no}  "
            f"{ref.old_text} → {ref.new_text}"
        )


@note_app.command("rename")
def note_rename(
    id: str = typer.Argument(..., help="Document id (or 6+ char prefix)."),
    new_title: str = typer.Argument(..., help="New title."),
    no_link_refactor: bool = typer.Option(
        False,
        "--no-link-refactor",
        help="Skip rewriting [[old-title]] references in other notes.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan without changing anything."
    ),
    vault: Path | None = typer.Option(
        None, "--vault", help="Override the configured vault path."
    ),
) -> None:
    """Rename a vault note: title, file slug, and ``[[old]]`` references.

    Plans the rename first (vault scan + collision check), then applies
    atomically — every file we'd write is snapshotted first; on any error
    the snapshots are restored. With ``--dry-run`` only the plan is
    printed; no DB or disk writes occur.

    With ``--no-link-refactor``, references in other notes are left alone
    (the title in this note's frontmatter still updates, and the file is
    still moved to its new slug).
    """
    cfg = Config.load()
    vault_path = _resolve_vault(vault, cfg)

    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        document_id = _resolve_id(conn, id)
        try:
            op = plan_rename(
                conn,
                vault_path=vault_path,
                document_id=document_id,
                new_title=new_title,
            )
        except RenameError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e

    if no_link_refactor:
        op = RenameOp(
            document_id=op.document_id,
            old_title=op.old_title,
            new_title=op.new_title,
            old_path=op.old_path,
            new_path=op.new_path,
            references=(),
        )

    if dry_run:
        _print_rename_plan(op, vault_path)
        return

    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        try:
            report = apply_rename(
                conn,
                embedder=embedder,
                vault_path=vault_path,
                op=op,
            )
        except RenameError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e

    if op.references:
        file_count = len({r.file_path for r in op.references})
        typer.echo(
            f"rewrote {report.references_rewritten} reference(s) "
            f"in {file_count} file(s)"
        )
    if report.file_renamed:
        old_rel = op.old_path.resolve().relative_to(vault_path.resolve())
        new_rel = op.new_path.resolve().relative_to(vault_path.resolve())
        typer.echo(
            f"renamed {old_rel.as_posix()} → {new_rel.as_posix()}"
        )
    else:
        typer.echo(f"updated title: {op.old_title!r} → {op.new_title!r}")
    if report.sync_report and report.sync_report.errors:
        for path, reason in report.sync_report.errors:
            typer.secho(f"sync error: {path}: {reason}", fg="red", err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Phase 4 — Link graph queries: backlinks / links / orphans / graph.
# ---------------------------------------------------------------------------


_GRAPH_FORMATTERS: dict[str, Any] = {
    "json": to_json,
    "dot": to_dot,
    "mermaid": to_mermaid,
}


@app.command()
def backlinks(
    id: str = typer.Argument(..., help="Document id (or 6+ char prefix)."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List documents that link TO this one.

    Resolves the id prefix first (same semantics as ``brain show``).
    Includes metadata-derived edges by default per spec §10 Q4 — derived
    rows are first-class answers to "what's connected to this doc?" and
    pick up a ``[derived: <rule>]`` prefix in human output to make their
    provenance obvious at a glance.

    Default human output is one row per backlink: ``<short-id> <kind>
    <title>  [[link-text]]``, with the ``[derived: <rule>]`` prefix on
    derived rows. ``--json`` emits an array of ``{src_document_id,
    src_title, src_kind, link_text, link_kind, rule, weight, evidence}``
    rows in the same order — ``rule`` / ``weight`` / ``evidence`` are
    populated for derived rows and ``null`` for wiki/embed rows so the
    JSON shape stays uniform across edge kinds.

    Empty result is exit-code 0 — "no backlinks" is a valid answer.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        doc_id = _resolve_id(conn, id)
        rows = backlinks_for(conn, doc_id)
    if json_output:
        emit_json(
            [
                {
                    "src_document_id": r.src_document_id,
                    "src_title": r.src_title,
                    "src_kind": r.src_kind,
                    "link_text": r.link_text,
                    "link_kind": r.link_kind,
                    "rule": r.rule,
                    "weight": r.weight,
                    "evidence": r.evidence,
                }
                for r in rows
            ]
        )
        return
    if not rows:
        typer.echo("(no backlinks)")
        return
    for r in rows:
        # Wiki rows are unannotated. Derived rows get a `[derived: <rule>]`
        # prefix per spec §10 Q3 — rule name only (the numeric weight is
        # noise for a human reader; the rule name already conveys the tier).
        # JSON output above carries `rule` / `weight` / `evidence` for
        # programmatic use.
        prefix = (
            f"[derived: {r.rule}] "
            if r.link_kind == "derived" and r.rule is not None
            else ""
        )
        typer.echo(
            f"{prefix}{r.src_document_id[:8]}  {r.src_kind:<8}  {r.src_title}  "
            f"{r.link_text}"
        )


@app.command()
def links(
    id: str = typer.Argument(..., help="Document id (or 6+ char prefix)."),
    json_output: bool = typer.Option(False, "--json"),
    unresolved: bool = typer.Option(
        False,
        "--unresolved",
        help="Include dangling [[refs]] that don't point at any document yet.",
    ),
) -> None:
    """List documents this one links TO.

    Default output mirrors ``brain backlinks`` but for outgoing edges:
    ``<short-id> <kind> <title>  [[link-text]]``, with a
    ``[derived: <rule>]`` prefix on metadata-derived rows. Includes
    derived edges by default per spec §10 Q4 (derived storage is
    undirected, so the partner set matches ``brain backlinks`` for those
    rows). With ``--unresolved``, dangling refs are appended after
    resolved rows with ``--------  -- (unresolved)`` placeholders.

    ``--json`` emits ``{dst_document_id, dst_title, dst_kind, link_text,
    link_kind, resolved, rule, weight, evidence}`` rows. ``rule`` /
    ``weight`` / ``evidence`` are populated for derived rows and ``null``
    for wiki/embed rows so the JSON shape stays uniform across edge
    kinds. Unresolved rows have null for the dst fields and
    ``"resolved": false``.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        doc_id = _resolve_id(conn, id)
        rows = outgoing_links_for(
            conn, doc_id, include_unresolved=unresolved
        )
    if json_output:
        emit_json(
            [
                {
                    "dst_document_id": r.dst_document_id,
                    "dst_title": r.dst_title,
                    "dst_kind": r.dst_kind,
                    "link_text": r.link_text,
                    "link_kind": r.link_kind,
                    "resolved": r.resolved,
                    "rule": r.rule,
                    "weight": r.weight,
                    "evidence": r.evidence,
                }
                for r in rows
            ]
        )
        return
    if not rows:
        typer.echo("(no outgoing links)")
        return
    for r in rows:
        if r.resolved:
            assert r.dst_document_id is not None  # resolved => fields set
            assert r.dst_title is not None
            assert r.dst_kind is not None
            # Wiki rows are unannotated. Derived rows get a
            # `[derived: <rule>]` prefix per spec §10 Q3 — rule name only
            # (the weight is noise for a human reader; JSON output above
            # carries `rule` / `weight` / `evidence` for programmatic use).
            prefix = (
                f"[derived: {r.rule}] "
                if r.link_kind == "derived" and r.rule is not None
                else ""
            )
            typer.echo(
                f"{prefix}{r.dst_document_id[:8]}  {r.dst_kind:<8}  {r.dst_title}  "
                f"{r.link_text}"
            )
        else:
            typer.echo(
                f"--------  --        (unresolved)  {r.link_text}"
            )


@app.command(name="orphans")
def orphans_cmd(
    all_tiers: bool = typer.Option(
        False,
        "--all",
        help="Include ingested-tier orphans (default: vault-tier only).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List documents with zero incoming AND zero outgoing links.

    Defaults to vault-tier only — ingested-tier orphans (raw Krisp /
    Slack / Gmail mirrors with no ``[[refs]]``) are usually noise; pass
    ``--all`` to include them.

    Output is one line per orphan: ``<short-id> <title>``. ``--json``
    emits ``{document_id, title, kind}`` rows.

    Empty result is exit-code 0 — "everything is connected" is a valid
    (and ideal) answer.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        rows = _orphans_query(conn, vault_only=not all_tiers)
    if json_output:
        emit_json(
            [
                {"document_id": n.document_id, "title": n.title, "kind": n.kind}
                for n in rows
            ]
        )
        return
    if not rows:
        typer.echo("(no orphans)")
        return
    for n in rows:
        typer.echo(f"{n.document_id[:8]}  {n.title}")


@app.command()
def graph(
    format: str = typer.Option(
        "json",
        "--format",
        help="Output format: json, dot, or mermaid.",
    ),
    root: str | None = typer.Option(
        None,
        "--root",
        help="Document id (or 6+ char prefix) to focus on; BFS outward from here.",
    ),
    depth: int | None = typer.Option(
        None,
        "--depth",
        help="BFS depth from --root (only with --root). Default: unlimited.",
    ),
    include_ingested: bool = typer.Option(
        False,
        "--include-ingested",
        help="Include ingested-tier nodes (default: vault-tier only).",
    ),
    no_derived: bool = typer.Option(
        False,
        "--no-derived",
        help=(
            "Exclude metadata-derived edges (shared_thread / "
            "shared_participant / same_day_participant). Default: include."
        ),
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Write output to PATH instead of stdout.",
    ),
) -> None:
    """Emit the link graph in JSON / Graphviz DOT / Mermaid format.

    Defaults to vault-tier only — pass ``--include-ingested`` to include
    every linked document regardless of tier. With ``--root`` (+ optional
    ``--depth``), the output is restricted to a BFS frontier centered on
    that document — a focused subgraph for visualization.

    Metadata-derived edges (``shared_thread`` / ``shared_participant`` /
    ``same_day_participant``) are included by default per spec §10 Q4;
    pass ``--no-derived`` to drop them and render only authored
    wiki/embed edges.

    Empty graphs emit valid syntax (``{"nodes": [], "edges": []}`` /
    ``digraph G {}`` / ``graph TD\\n``). Exit code is 0 in every
    successful case, including empty results.

    Pipe DOT into ``dot -Tsvg -o graph.svg``; paste Mermaid into any
    Mermaid renderer.
    """
    if format not in _GRAPH_FORMATTERS:
        raise typer.BadParameter(
            f"--format must be one of: {', '.join(sorted(_GRAPH_FORMATTERS))}"
        )
    if depth is not None and root is None:
        raise typer.BadParameter("--depth requires --root")
    if depth is not None and depth < 0:
        raise typer.BadParameter("--depth must be >= 0")

    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        root_id: str | None = None
        if root is not None:
            root_id = _resolve_id(conn, root)
        snapshot = graph_data(
            conn,
            root=root_id,
            depth=depth,
            include_ingested=include_ingested,
            include_derived=not no_derived,
        )

    formatter = _GRAPH_FORMATTERS[format]
    rendered: str = formatter(snapshot)
    if out is not None:
        out.write_text(rendered, encoding="utf-8")
        typer.echo(f"wrote {out} ({len(rendered)} bytes)")
        return
    # Use ``typer.echo`` with ``nl=False`` so the formatters' own trailing
    # newlines (when present) aren't doubled. JSON has no trailing newline;
    # DOT / Mermaid both end with one — the formatters own that contract.
    typer.echo(rendered, nl=False)
    if not rendered.endswith("\n"):
        typer.echo("")


@backfill_app.command("source-rows")
def backfill_source_rows_cmd(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report counts without writing.",
    ),
) -> None:
    """Set ``source_id`` to a manual sources row for legacy markdown docs.

    Targets ``documents`` rows where ``source_id IS NULL``,
    ``content_type = 'markdown'``, and ``source_path IS NOT NULL`` — the
    file-ingested rows that predate the manual-source default in
    ``ingest_document``. Each match upserts a ``sources`` row with
    ``kind="manual"`` and ``external_id = source_path`` (deduped by the
    UNIQUE ``(kind, external_id)``), then points the document at it.

    Idempotent: re-running after a successful pass is a no-op (the
    ``source_id IS NULL`` filter filters everything out). The whole pass
    runs in a single transaction. Pass ``--dry-run`` for a preview that
    only counts candidates without writing.

    After this completes, re-export the vault so the on-disk frontmatter
    picks up the new ``source: manual`` lines:
    ``brain vault export --to <vault-dir> --force``.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        # Autocommit so the explicit ``conn.transaction()`` inside
        # backfill_source_rows owns the write boundary; otherwise the
        # outer implicit txn opened by the pre-write SELECT would roll
        # back on close and silently undo the backfill.
        conn.autocommit = True
        report = backfill_source_rows(conn, commit=not dry_run)

    if report.candidates == 0:
        typer.echo("nothing to backfill (no markdown docs with NULL source_id)")
        return

    if report.dry_run:
        typer.echo(f"would backfill {report.candidates} markdown doc(s)")
        typer.echo("  (re-run without --dry-run to apply)")
        return

    typer.echo(
        f"backfilled {report.documents_updated} document(s); "
        f"created {report.sources_created} new manual source row(s)"
    )
    typer.echo(
        "next: re-export vault so frontmatter picks up `source: manual` — "
        "brain vault export --to <vault-dir> --force"
    )


@backfill_app.command("search")
def backfill_search_cmd() -> None:
    """Backfill ``chunks.title_text`` / ``tags_text`` / ``search_extras`` for migration 009.

    Two stages, both idempotent:

    Stage A — SQL ``UPDATE`` denormalizes ``documents.title`` and
    ``documents.tags`` onto every chunk via the FK. Re-running on a
    converged corpus reports 0 rows updated.

    Stage B — Python loop: recomputes ``extract_sub_tokens(content)`` for
    every chunk and writes back only when the computed value differs from
    the stored ``search_extras``. Restores the canonical value if a row's
    ``search_extras`` was hand-edited to a stale string.

    ``brain init`` calls this automatically right after migration 009 is
    first applied; running it again by hand later is safe and a no-op
    once the corpus has converged.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        # Autocommit so the explicit ``conn.transaction()`` blocks inside
        # backfill_search.run own the write boundary; otherwise the outer
        # implicit txn opened by the pre-write SELECT would roll back on
        # close and silently undo the backfill.
        conn.autocommit = True
        report = backfill_search.run(conn)

    typer.echo(f"Stage A (title/tags denorm): {report.stage_a_rows} rows updated")
    typer.echo(f"Stage B (search_extras):     {report.stage_b_rows} rows updated")
    typer.echo(f"Total chunks:                {report.total_chunks}")


def _load_tag_mapping(path: Path) -> dict[str, str]:
    """Load and validate a ``--mapping`` JSON file for ``backfill normalize-tags``.

    The expected shape is a flat ``{from: to}`` object of strings — values are
    treated as canonical synonyms applied *before* :func:`normalize_tags`. Both
    keys and values are themselves passed through :func:`normalize_tag` so the
    user's mapping JSON can use any casing/separator and still work
    (``{"Recruiters": "Recruiter"}`` collapses to ``recruiters → recruiter``).
    Empty keys after normalization are dropped silently.

    Raises :class:`typer.BadParameter` for an unreadable file, malformed JSON,
    or a non-mapping payload — these are user errors, not crashes.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise typer.BadParameter(f"could not read mapping file: {e}") from e
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError as e:
        raise typer.BadParameter(f"mapping file is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise typer.BadParameter(
            "mapping file must be a JSON object of {from: to} strings"
        )
    cleaned: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise typer.BadParameter(
                "mapping entries must be strings; "
                f"got {type(k).__name__} → {type(v).__name__}"
            )
        canonical_from = normalize_tag(k)
        canonical_to = normalize_tag(v)
        if not canonical_from or not canonical_to:
            continue
        cleaned[canonical_from] = canonical_to
    return cleaned


def _apply_tag_mapping(tags: list[str], mapping: dict[str, str]) -> list[str]:
    """Apply a synonym mapping to ``tags`` *before* :func:`normalize_tags`.

    Each input tag is canonicalized once via :func:`normalize_tag` so the
    mapping lookup is case/separator-insensitive: an input of ``Recruiters``
    matches a mapping key of ``recruiters``. Tags with no entry in the
    mapping are returned canonicalized (the caller still pipes the result
    through :func:`normalize_tags` for dedupe + empty-drop, so passing
    pre-canonical tags here is a no-op).
    """
    out: list[str] = []
    for tag in tags:
        canonical = normalize_tag(tag)
        out.append(mapping.get(canonical, canonical))
    return out


@backfill_app.command("normalize-tags")
def backfill_normalize_tags(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print planned changes without applying.",
    ),
    mapping: Path | None = typer.Option(
        None,
        "--mapping",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Optional JSON {from: to} for manual collapses (synonyms, plurals).",
    ),
) -> None:
    """Lowercase + dedupe every tag in the corpus (DB + vault files).

    Idempotent. Uses :func:`brain.tags.normalize_tags` as the canonical rule:
    casefold, replace whitespace/underscore with hyphen, collapse runs of
    hyphens, dedupe preserving first-seen order. Re-running this command
    after it converges is a no-op.

    The optional ``--mapping`` flag is an escape hatch for non-mechanical
    collapses (synonyms, plurals, abbreviations) like
    ``{"recruiters": "recruiter"}`` or
    ``{"artificial-intelligence": "ai"}``. Mapping keys are matched after
    canonicalizing each input tag, so the JSON works regardless of the
    on-disk casing/separator. Mappings are applied BEFORE the canonical
    normalize step.

    For each doc, the new tag list is written directly to
    ``documents.tags`` (we don't go through :func:`apply_tags`'s add/remove
    diff — this is a full replace). When ``vault_path`` is set and the
    file exists, the file's frontmatter is rewritten via
    :func:`brain.vault.frontmatter.rewrite_tags`. Missing files are
    warned (yellow on stderr) and skipped without erroring — same pattern
    as ``brain tag``.
    """
    cfg = Config.load()
    mapping_dict: dict[str, str] = (
        _load_tag_mapping(mapping) if mapping is not None else {}
    )

    docs_normalized = 0
    files_rewritten = 0
    files_missing = 0
    already_canonical = 0
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        # Only fetch docs with at least one tag; an array_length filter keeps
        # the working set tight even on a large corpus.
        rows = conn.execute(
            "SELECT id::text, title, tags, vault_path "
            "FROM documents "
            "WHERE tags IS NOT NULL AND array_length(tags, 1) > 0 "
            "ORDER BY id"
        ).fetchall()
        for doc_id, title, current_tags, vault_path_rel in rows:
            current = list(current_tags or [])
            mapped = _apply_tag_mapping(current, mapping_dict)
            new_tags = normalize_tags(mapped)
            if new_tags == current:
                already_canonical += 1
                continue
            if dry_run:
                typer.echo(
                    f"{doc_id[:8]}  {title}  {current} → {new_tags}"
                )
                docs_normalized += 1
                continue
            conn.execute(
                "UPDATE documents SET tags = %s WHERE id = %s",
                (new_tags, doc_id),
            )
            # Migration 009 denormalizes documents.tags onto chunks.tags_text
            # so the weighted tsv reflects tag changes. The bulk normalizer
            # bypasses ``apply_tags`` (full replace, not add/remove diff), so
            # the chunk sync has to happen here. The IS DISTINCT FROM guards
            # inside the helper make this a no-op when only ordering changed.
            sync_chunk_search_metadata(conn, doc_id)
            docs_normalized += 1
            if vault_path_rel is None:
                continue
            abs_path = cfg.vault_path / vault_path_rel
            if abs_path.exists():
                if rewrite_tags(abs_path, new_tags):
                    files_rewritten += 1
            else:
                files_missing += 1
                typer.secho(
                    f"file missing on disk for {doc_id[:8]} ({vault_path_rel}); "
                    "DB updated, file skipped.",
                    fg=typer.colors.YELLOW,
                    err=True,
                )

    prefix = "would normalize" if dry_run else "normalized"
    typer.echo(
        f"{prefix} {docs_normalized} doc(s), "
        f"rewrote {files_rewritten} file(s), "
        f"{files_missing} file-missing skipped, "
        f"{already_canonical} already-canonical skipped"
    )
