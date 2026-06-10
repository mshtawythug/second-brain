"""brain — second brain CLI."""
from __future__ import annotations

import dataclasses
import enum
import json as _json  # aliased — `json` conflicts with the --json output flag name
import logging
import os
import shutil
import subprocess
import sys
import time as _time
import uuid
from datetime import UTC, datetime
from datetime import date as date_cls
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import psycopg
import typer
from rich.table import Table

from . import config as _config_module
from .backfill import backfill_search, backfill_source_rows
from .config import Config, ConfigError
from .db import (
    DEFAULT_GRAPH_NAME,
    age_extension_available,
    bootstrap_age,
    connect,
    connect_age,
    connect_raw,
    ensure_embedding_column,
    load_age,
    run_migrations,
)
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
    AgeBootstrapError,
    ElicitError,
    EnrichmentError,
    GraphBackendError,
    GraphTenantError,
    IdPrefixAmbiguous,
    IdPrefixNotFound,
    IdPrefixNotHex,
    IdPrefixTooShort,
    InteractionError,
    OllamaUnavailable,
    PersonAmbiguous,
    PersonNotFound,
    VaultNoteSyncError,
)

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher
    from .graph_rag.reconcile import ReconcileConfig
    from .graph_rag.schema import GraphContext
    from .graph_rag.sync import GraphSyncer
from ._capture_command import _INBOX_TAG as _CAPTURE_INBOX_TAG
from ._capture_command import capture_app
from .cli_claude import SkillInstallError
from .cli_claude import install_skill as _install_skill
from .eval import (
    EvalBaselineError,
    EvalCorpusError,
    diff_reports,
    load_baseline,
    load_corpus,
    run_eval,
    save_baseline,
)
from .eval.baseline import _assert_baseline_name
from .eval.corpus import _DEFAULT_CORPUS_PATH, _VALID_CATEGORIES
from .format import (
    alias_result_json,
    alias_result_summary,
    community_record_json,
    community_records_table,
    console,
    emit_json,
    entity_summaries_json,
    entity_summaries_table,
    eval_diff_table,
    eval_report_table,
    explain_table,
    graph_context_json,
    graph_context_renderable,
    graph_stats_json,
    graph_stats_table,
    search_table,
)
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
from .interactions import record_interaction
from .queries import (
    MirrorDriftSummary,
    PersonMatch,
    analyze_tables,
    count_chunks_missing_embedding,
    count_documents,
    count_documents_with_tag,
    count_unenriched_documents,
    embedding_column_state,
    fetch_document,
    finalize_embedding_index,
    iter_all_document_ids,
    iter_chunks_missing_embedding,
    iter_orphan_mirror_files,
    iter_stale_mirror_files,
    iter_unenriched_documents,
    list_documents,
    list_existing_tags,
    list_public_tables,
    mirror_drift_summary,
    resolve_document_prefix,
    resolve_person_to_keys,
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
    refresh_people_yml,
    rescan_gmail_directory,
)
from .vault.derived_links.fence import rewrite_derived_fences
from .vault.export import export_vault, regenerate_vault_file
from .vault.frontmatter import rewrite_tags
from .vault.graph import (
    backlinks_for,
    graph_data,
    outgoing_links_for,
)
from .vault.graph import orphans as _orphans_query
from .vault.graph_format import to_dot, to_json, to_mermaid
from .vault.note_builder import (
    _build_embedder,
    _build_note_text,
    create_vault_note,
)
from .vault.quartz_overlay import OverlayError, apply_overlay, plan_overlay
from .vault.rename import RenameError, RenameOp, apply_rename, plan_rename
from .vault.slug import slugify
from .vault.sync import SyncReport, sync_one_file, sync_vault
from .vault.sync_summaries import sync_summaries
from .vault.templates import list_template_names
from .vault.watch import WatchConfig, run_watcher
from .wiki.build_people import (
    PersonRecord,
    aggregate_people,
    emit_people_pages,
    humanize_display_name,
)
from .wiki.install import WikiInstallError
from .wiki.install import wiki_install as _wiki_install

logger = logging.getLogger(__name__)

# Baseline JSON files live next to the golden corpus: tests/eval/baselines/.
_BASELINES_DIR: Path = _DEFAULT_CORPUS_PATH.parent / "baselines"

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

owner_app = typer.Typer(
    name="owner",
    help=(
        "Manage BRAIN_OWNER_PARTICIPANTS — corpus-owner identifiers stripped "
        "from derived-edge participant rules (R2/R3)."
    ),
    no_args_is_help=True,
)
app.add_typer(owner_app, name="owner")

wiki_app = typer.Typer(
    name="wiki",
    help="Wiki workspace management (Quartz install, Caddyfile rendering).",
    no_args_is_help=True,
)
app.add_typer(wiki_app, name="wiki")

claude_app = typer.Typer(
    name="claude",
    help="Claude Code integration.",
    no_args_is_help=True,
)
app.add_typer(claude_app, name="claude")

graphrag_app = typer.Typer(
    name="graphrag",
    help=(
        "GraphRAG admin/index operations (Apache AGE people graph). "
        "Retrieval surfaces arrive in G2."
    ),
    no_args_is_help=True,
)
app.add_typer(graphrag_app, name="graphrag")

elicit_app = typer.Typer(
    name="elicit",
    help="Tacit-knowledge elicitation — surface and manage knowledge gaps.",
)
app.add_typer(elicit_app, name="elicit")

# Plan 09 — quick-capture inbox. Command logic lives in `_capture_command.py`
# (cli.py is intentionally kept thin); review/list subcommands land in Phase 2.
app.add_typer(capture_app, name="capture")

# Plan 10 — `brain review weekly` periodic synthesis. The sub-app is the shared
# home for the unified `brain review` tree; Plan 03 adds scan/list/dismiss here.
review_app = typer.Typer(
    no_args_is_help=True,
    help="Periodic synthesis over the corpus (weekly review; scan/list/dismiss).",
)
app.add_typer(review_app, name="review")


@app.callback()
def _main() -> None:
    """brain — second brain CLI root."""


@app.command("setup")
def setup_cmd(
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Use defaults for every prompt"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print planned actions; touch nothing"
    ),
    brain_home: Path | None = typer.Option(
        None, "--brain-home", help="Override $BRAIN_HOME (default ~/.brain)"
    ),
    vault: Path | None = typer.Option(
        None, "--vault", help="Override $BRAIN_VAULT_PATH"
    ),
    port: int = typer.Option(5433, "--port", help="Postgres host port"),
    wiki_port: int = typer.Option(8080, "--wiki-port", help="Caddy port for the wiki"),
    embedder: str | None = typer.Option(
        None, "--embedder", help="arctic|voyage|qwen3 (non-interactive choice)"
    ),
    skip_wiki: bool = typer.Option(False, "--skip-wiki", help="Don't install the wiki UI"),
    skip_skill: bool = typer.Option(
        False, "--skip-skill", help="Don't install the Claude Code skill"
    ),
    reset: bool = typer.Option(
        False,
        "--reset",
        help="Destructive reset of $BRAIN_HOME (requires typed confirmation)",
    ),
) -> None:
    """One-command installer for the second-brain runtime.

    Sets up $BRAIN_HOME, installs shims, starts Postgres via Docker, and
    optionally configures the wiki (Caddy + Quartz) and the Claude Code skill.

    Use --dry-run to preview every action without touching the filesystem.
    Use --reset to wipe an existing $BRAIN_HOME before re-running (typed
    confirmation required — cannot be bypassed even with --non-interactive).
    """
    from .setup import SetupError, run_setup

    try:
        run_setup(
            non_interactive=non_interactive,
            dry_run=dry_run,
            brain_home_override=brain_home,
            vault_override=vault,
            pg_port=port,
            wiki_port=wiki_port,
            embedder_choice=embedder,
            skip_wiki=skip_wiki,
            skip_skill=skip_skill,
            reset=reset,
        )
    except SetupError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc


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
        # GraphRAG (wave G0): provision the Apache AGE extension + the canonical
        # ``brain_graph`` graph idempotently, after relational migrations — but
        # ONLY when the DB image actually ships AGE. A stock pgvector image (a
        # prod DB before the gated AGE cut-over) cannot ``CREATE EXTENSION age``;
        # bootstrapping unconditionally would crash init AFTER the relational
        # migrations already committed. So probe ``pg_available_extensions``
        # first and SKIP the whole graph stack with a friendly note when AGE is
        # absent, letting init finish cleanly. When AGE *is* available the
        # bootstrap is a safe no-op on re-run (existence-guarded create_graph).
        # Requires the autocommit connection set above — AGE catalog DDL
        # dislikes open txns.
        age_available = age_extension_available(conn)
        graph_created = False
        if age_available:
            graph_created = bootstrap_age(conn)
            # Reconcile graph_entities.embedding's declared dim with the active
            # embedder, exactly as for chunks.embedding above — a non-1024
            # backend (e.g. qwen3=4096) otherwise leaves the migration-012
            # vector(1024) column mismatched. Resizes in place; the column stays
            # NULLABLE (no NOT NULL / HNSW finalize) because no entities are
            # embedded yet in G0 — the graph write path is a no-op this wave.
            # Gated with the bootstrap so a stock-pgvector DB skips it too.
            ensure_embedding_column(conn, embedder, "graph_entities", "embedding")
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
    if age_available:
        typer.echo(
            f"graph           brain_graph "
            f"{'created' if graph_created else 'present'} (age)"
        )
    else:
        typer.echo(
            "graph           skipped — AGE not available in this database "
            "image; cut over to the AGE image then re-run `brain init` "
            f"(see {_AGE_IMAGE_SPEC})"
        )
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
    """Doctor sub-check: ping Ollama and verify the backend's model is loaded.

    Also reports presence of ``cfg.enrich_model`` (Wave Q1-D auto-summary
    backend) on the same ``/api/tags`` payload — one HTTP call, two checks.
    The enrich-model check is informational only (yellow warn when missing);
    the user can disable enrichment with ``--no-enrich`` if the model isn't
    available.
    """
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
        # Wave Q1-D — enrich model check. Soft (never failure).
        if _model_loaded(cfg.enrich_model, loaded_models):
            typer.echo(f"enrich model    OK ({cfg.enrich_model} loaded)")
        else:
            typer.secho(
                f"enrich model    WARN ({cfg.enrich_model} not in /api/tags — "
                f"run `ollama pull {cfg.enrich_model}` to enable auto-summary)",
                fg="yellow",
            )
    except (httpx.HTTPError, ValueError) as e:
        # ValueError covers a non-JSON /api/tags response (json.JSONDecodeError).
        failures.append(f"ollama: {e}")
        typer.secho(f"ollama          FAIL — {e}", fg="red", err=True)


def _ollama_reachable(cfg: Config) -> bool:
    """Cheap Ollama liveness probe: GET ``/api/tags`` (5s), same call as doctor.

    Returns ``True`` iff Ollama answers without a transport/HTTP error. Used to
    decide UPFRONT whether to wire the contradiction detector in ``elicit list``
    so the offline delta/orphan detectors run exactly once — never twice via a
    catch-and-rerun on :class:`OllamaUnavailable`.
    """
    try:
        with httpx.Client(
            base_url=cfg.ollama_host, timeout=httpx.Timeout(5.0)
        ) as client:
            client.get("/api/tags").raise_for_status()
    except httpx.HTTPError:
        return False
    return True


# GraphRAG (wave G0). Path the WARN remediations point operators at when the DB
# image predates the Apache AGE cut-over.
_AGE_IMAGE_SPEC = "docs/specs/2026-05-20-graphrag-age-image.md"


def _rollback_quietly(conn: psycopg.Connection[Any]) -> None:
    """Roll back the implicit read transaction on a non-autocommit connection.

    The doctor probes are read-only ``SELECT``s; under psycopg's default
    ``autocommit=False`` each opens a transaction that must be cleared so the
    connection stays clean for the next probe (and so a later ``LOAD``/flip is
    unobstructed). A no-op under autocommit.
    """
    if not conn.autocommit:
        conn.rollback()


def _installed_extension_versions(
    conn: psycopg.Connection[Any],
) -> dict[str, str]:
    """Return ``{extname: extversion}`` for the GraphRAG-relevant extensions.

    One ``pg_extension`` round-trip covering ``vector``, ``pgcrypto`` and
    ``age``; absent extensions are simply missing keys. Rolls the implicit read
    transaction back so the caller's connection stays clean.
    """
    rows = conn.execute(
        "SELECT extname, extversion FROM pg_extension "
        "WHERE extname IN ('vector', 'pgcrypto', 'age')"
    ).fetchall()
    _rollback_quietly(conn)
    return {str(name): str(version) for name, version in rows}


def _age_graph_present(conn: psycopg.Connection[Any], graph_name: str) -> bool:
    """True iff ``graph_name`` exists in the AGE graph catalog.

    Rolls the implicit read transaction back afterward so the caller's
    connection stays clean. Lets any ``psycopg.Error`` propagate — the caller
    decides how to surface a catalog-probe failure.
    """
    present = (
        conn.execute(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s",
            (graph_name,),
        ).fetchone()
        is not None
    )
    _rollback_quietly(conn)
    return present


def _check_chunks_stats(conn: psycopg.Connection[Any]) -> None:
    """Doctor sub-check: warn when ``chunks`` table stats are stale or absent.

    Soft check — never flips doctor's exit code. Catches the post-restore
    stale-stats root cause: a ``pg_restore`` does NOT run ``ANALYZE``, and
    rows bulk-loaded via ``COPY`` do not trigger autoanalyze until the
    autovacuum daemon makes its first pass (which can be minutes to hours
    after restore). Meanwhile ``EXPLAIN`` falls back to default estimates,
    producing bad query plans.

    Reports:
    - ``chunks stats   OK (analyzed <ago>)`` — last_analyze or last_autoanalyze
      is set (whichever is more recent).
    - ``chunks stats   WARN — never analyzed`` when BOTH are NULL and the
      table is non-empty. Suggests ``brain analyze`` (which runs the SQL).
    - Silently skips the check (no output) when the table is empty (fresh
      install before any ingest).
    """
    try:
        row = conn.execute(
            """
            SELECT last_analyze, last_autoanalyze, n_live_tup
            FROM pg_stat_user_tables
            WHERE relname = 'chunks'
            """
        ).fetchone()
    except psycopg.Error as exc:
        _rollback_quietly(conn)
        typer.secho(
            f"chunks stats    WARN — could not probe pg_stat_user_tables: {exc}",
            fg="yellow",
        )
        return

    if row is None:
        # Table not yet visible in pg_stat_user_tables (no vacuums/analyzes yet).
        return

    last_analyze, last_autoanalyze, n_live_tup = row
    if n_live_tup == 0:
        # Empty table — no stats needed yet; skip so fresh installs stay clean.
        return

    most_recent = None
    if last_analyze is not None:
        most_recent = last_analyze
    if last_autoanalyze is not None and (most_recent is None or last_autoanalyze > most_recent):
        most_recent = last_autoanalyze

    if most_recent is None:
        typer.secho(
            f"chunks stats    WARN — never analyzed ({n_live_tup:,} live rows, "
            "stats NULL). Run: brain analyze  (or SQL: ANALYZE chunks;)  "
            "This can happen after pg_restore — planners use default estimates "
            "until ANALYZE runs.",
            fg="yellow",
        )
    else:
        analyzed_at = most_recent.strftime("%Y-%m-%d %H:%M UTC")
        typer.echo(f"chunks stats    OK (last analyzed {analyzed_at})")


def _check_inbox_size(conn: psycopg.Connection[Any], cfg: Config) -> None:
    """Doctor sub-check: warn when the quick-capture inbox has grown large.

    Soft check — never flips doctor's exit code. Counts documents still
    carrying the ``inbox`` tag (Plan 09 quick-capture). When the count exceeds
    ``cfg.capture_inbox_warn_threshold`` it nudges the user toward
    ``brain capture review``; otherwise it prints a plain OK line. The count is
    sourced from :func:`brain.queries.count_documents_with_tag` so the SQL lives
    in exactly one place (shared with ``brain capture``).
    """
    try:
        count = count_documents_with_tag(conn, _CAPTURE_INBOX_TAG)
    except psycopg.Error as exc:
        _rollback_quietly(conn)
        typer.secho(
            f"inbox           WARN — could not probe inbox size: {exc}",
            fg="yellow",
        )
        return
    if count > cfg.capture_inbox_warn_threshold:
        typer.secho(
            f"inbox           WARN — {count} items "
            f"(> {cfg.capture_inbox_warn_threshold}); run `brain capture review`",
            fg="yellow",
        )
    else:
        typer.echo(f"inbox           OK ({count} items)")


def _check_age(conn: psycopg.Connection[Any]) -> None:
    """Doctor sub-check: verify the Apache AGE GraphRAG backend is provisioned.

    Soft check — never flips doctor's exit code. The AGE rollout (wave G0) can
    land before the prod DB image is cut over, so a database without AGE is a
    yellow ``WARN`` with an actionable remediation rather than a hard failure.

    Asserts, in order, stopping at the first unmet condition:

    1. ``vector``, ``pgcrypto`` and ``age`` extensions are present in
       ``pg_extension``.
    2. Reports the ``age`` extversion — built from the ``PG16/v1.5.0-rc0``
       release candidate (extversion ``1.5.0``), **not** a GA release; the
       wording stays honest and never claims "GA".
    3. ``LOAD 'age'`` succeeds in-session. Reuses :func:`brain.db.load_age` for
       the LOAD + rollback + typed-error contract. ``load_age`` re-checks
       ``pg_extension`` for ``age`` — a negligible extra ``SELECT`` in this
       rarely-run command; we accept that small overlap rather than duplicate
       ``load_age``'s LOAD/rollback/error-wrapping logic inline.
    4. The canonical ``brain_graph`` graph exists in ``ag_catalog.ag_graph``.

    Prints ``age             OK (age <ver>, graph brain_graph present)`` when all
    four hold; otherwise a single WARN line naming the failed condition and the
    next step to fix it.
    """
    try:
        installed = _installed_extension_versions(conn)
    except psycopg.Error as exc:
        _rollback_quietly(conn)
        typer.secho(
            f"age             WARN — extension probe failed: {exc}", fg="yellow"
        )
        return

    if "age" not in installed:
        # Distinguish "the image can't do AGE at all" from "AGE is installable
        # but `brain init` hasn't created the extension yet" — the remediation
        # differs. ``age_extension_available`` probes ``pg_available_extensions``
        # (control file present) vs the ``pg_extension`` (installed) probe above.
        # Guarded: this AGE check is WARN-only, so a probe failure must NOT escape
        # to doctor()'s outer DB handler (which would flip the run to exit 1).
        try:
            available = age_extension_available(conn)
        except psycopg.Error as exc:
            _rollback_quietly(conn)
            typer.secho(
                f"age             WARN — couldn't determine AGE availability: {exc}",
                fg="yellow",
            )
            return
        if available:
            typer.secho(
                "age             WARN — Apache AGE is available but not installed "
                "— run `brain init` to install + bootstrap AGE",
                fg="yellow",
            )
        else:
            typer.secho(
                "age             WARN — the DB image lacks Apache AGE — rebuild/cut "
                f"over to the AGE image; see {_AGE_IMAGE_SPEC}",
                fg="yellow",
            )
        return

    missing_support = [
        name for name in ("vector", "pgcrypto") if name not in installed
    ]
    if missing_support:
        typer.secho(
            "age             WARN — missing extension(s): "
            f"{', '.join(missing_support)} — run `brain init`",
            fg="yellow",
        )
        return

    try:
        loaded = load_age(conn)
    except AgeBootstrapError as exc:
        # Roll back too — defensive parity with the psycopg.Error handlers. The
        # exception may have left an aborted transaction; clearing it keeps the
        # connection usable so this check can never poison a later one.
        _rollback_quietly(conn)
        typer.secho(
            f"age             WARN — `LOAD 'age'` failed ({exc}) — the AGE "
            "extension isn't loadable in this database/image; rebuild or cut "
            f"over to the AGE image (see {_AGE_IMAGE_SPEC})",
            fg="yellow",
        )
        return
    if not loaded:
        # Extension row present but load_age found it absent — defensive guard;
        # treat as the image-missing case.
        typer.secho(
            "age             WARN — the DB image lacks Apache AGE — rebuild/cut "
            f"over to the AGE image; see {_AGE_IMAGE_SPEC}",
            fg="yellow",
        )
        return

    try:
        graph_present = _age_graph_present(conn, DEFAULT_GRAPH_NAME)
    except psycopg.Error as exc:
        _rollback_quietly(conn)
        typer.secho(
            f"age             WARN — graph catalog probe failed: {exc}",
            fg="yellow",
        )
        return

    if not graph_present:
        typer.secho(
            f"age             WARN — graph {DEFAULT_GRAPH_NAME} absent — run "
            "`brain init` to bootstrap the AGE graph",
            fg="yellow",
        )
        return

    typer.echo(
        f"age             OK (age {installed['age']}, "
        f"graph {DEFAULT_GRAPH_NAME} present)"
    )


def _relational_graph_counts(
    conn: psycopg.Connection[Any], tenant_id: str
) -> tuple[int, int]:
    """Return ``(entity_count, cooccur_edge_count)`` from the relational mirror.

    The source-of-truth counts the doctor drift check compares against the AGE
    graph: ``graph_entities`` and the ``graph_relationships`` aggregate (the SQL
    counterpart of the AGE ``CO_OCCURS`` edges; spec §5). Tenant-scoped. Rolls
    the implicit read transaction back so the shared connection stays clean.
    """
    ent_row = conn.execute(
        "SELECT count(*) FROM graph_entities WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()
    rel_row = conn.execute(
        "SELECT count(*) FROM graph_relationships WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()
    _rollback_quietly(conn)
    return (
        int(ent_row[0]) if ent_row is not None else 0,
        int(rel_row[0]) if rel_row is not None else 0,
    )


def _check_graph_drift(conn: psycopg.Connection[Any], cfg: Config) -> None:
    """Doctor sub-check: relational ↔ AGE graph entity/edge parity (spec §7).

    Gated by the caller on ``BRAIN_GRAPH_ENABLED``; here it additionally requires
    Apache AGE to be present + loadable (returns silently otherwise — the
    ``age`` line already warned). Compares the configured tenant's relational
    source-of-truth counts (``graph_entities`` / ``graph_relationships``) against
    the AGE mirror (``Entity`` vertices / ``CO_OCCURS`` edges) and prints a
    counts line; a mismatch is a yellow ``drift detected`` WARN pointing at
    ``brain graphrag build --force`` (the authoritative rebuild). Soft check:
    every failure mode is a WARN that never flips doctor's exit code (it catches
    its own probe errors so a graph hiccup never masquerades as a postgres
    failure).
    """
    try:
        if not age_extension_available(conn):
            _rollback_quietly(conn)
            return
        if not load_age(conn):
            return
    except (psycopg.Error, AgeBootstrapError) as exc:
        _rollback_quietly(conn)
        typer.secho(
            f"graph drift     WARN — AGE availability probe failed: {exc}",
            fg="yellow",
        )
        return

    from .graph_rag.backends import AgeBackend
    from .graph_rag.tenancy import resolve_tenant

    try:
        tenant_id = resolve_tenant(cfg)
    except GraphTenantError as exc:
        typer.secho(f"graph drift     WARN — {exc}", fg="yellow")
        return

    try:
        rel_entities, rel_edges = _relational_graph_counts(conn, tenant_id)
        backend = AgeBackend()
        age_entities = backend.count_entities(conn, tenant_id)
        age_edges = backend.count_cooccur_edges(conn, tenant_id)
    except (psycopg.Error, GraphBackendError) as exc:
        _rollback_quietly(conn)
        typer.secho(
            f"graph drift     WARN — graph count probe failed: {exc}", fg="yellow"
        )
        return
    finally:
        _rollback_quietly(conn)

    counters = (
        f"entities rel={rel_entities} age={age_entities}, "
        f"co_occurs rel={rel_edges} age={age_edges}, tenant {tenant_id!r}"
    )
    if rel_entities == age_entities and rel_edges == age_edges:
        typer.echo(f"graph drift     OK ({counters})")
        return
    typer.secho(f"graph drift     drift detected ({counters})", fg="yellow")
    typer.secho(
        "                — run `brain graphrag build --force` to rebuild the "
        "AGE mirror from the relational source-of-truth",
        fg="yellow",
    )


def _community_counts(
    conn: psycopg.Connection[Any], tenant_id: str
) -> tuple[int, int]:
    """Return ``(community_count, member_count)`` for the tenant (spec §17c).

    The counts the doctor community check reports: ``graph_communities`` and
    ``graph_community_members`` rows for the configured tenant. Tenant-scoped.
    Rolls the implicit read transaction back so the shared connection stays clean
    (mirrors :func:`_relational_graph_counts`).
    """
    comm_row = conn.execute(
        "SELECT count(*) FROM graph_communities WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()
    mem_row = conn.execute(
        "SELECT count(*) FROM graph_community_members WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    _rollback_quietly(conn)
    return (
        int(comm_row[0]) if comm_row is not None else 0,
        int(mem_row[0]) if mem_row is not None else 0,
    )


def _stored_community_fingerprints(
    conn: psycopg.Connection[Any], tenant_id: str
) -> set[str]:
    """Return the DISTINCT ``source_graph_hash`` set across the tenant's communities.

    A clean build stamps every community row with the build's fingerprint, so a
    healthy set is exactly ``{current_hash}``; any divergence (membership change,
    extra hash) means the communities no longer reflect the live graph.
    """
    rows = conn.execute(
        "SELECT DISTINCT source_graph_hash FROM graph_communities "
        "WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _relationship_edges(
    conn: psycopg.Connection[Any], tenant_id: str
) -> list[tuple[str, str, float]]:
    """Read the tenant's ``graph_relationships`` edges as ``(src, dst, weight)``.

    The input to :func:`brain.graph_rag.communities.compute_source_graph_hash`
    (which sorts internally, so read order is irrelevant). Tenant-scoped — the
    same edge set the community build hashes into ``source_graph_hash``.
    """
    rows = conn.execute(
        "SELECT src_id::text, dst_id::text, weight FROM graph_relationships "
        "WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchall()
    return [(str(src), str(dst), float(weight)) for src, dst, weight in rows]


def _check_graph_communities(conn: psycopg.Connection[Any], cfg: Config) -> None:
    """Doctor sub-check: community counts + stale-fingerprint detection (§17c).

    Gated by the caller on ``BRAIN_GRAPH_ENABLED``; here it additionally requires
    Apache AGE to be present + loadable (returns silently otherwise — the ``age``
    line already warned), mirroring :func:`_check_graph_drift`. Reports the
    configured tenant's ``graph_communities`` / ``graph_community_members`` counts
    and compares the communities' stored ``source_graph_hash`` fingerprint against
    the CURRENT tenant graph hash, recomputed from ``graph_relationships`` via the
    same :func:`brain.graph_rag.communities.compute_source_graph_hash` the build
    uses. A mismatch is a yellow ``stale`` WARN pointing at ``brain graphrag
    communities refresh`` (the authoritative rebuild). Soft check: every failure
    mode is a WARN that never flips doctor's exit code (it catches its own probe
    errors so a community hiccup never masquerades as a postgres failure).
    """
    try:
        if not age_extension_available(conn):
            _rollback_quietly(conn)
            return
        if not load_age(conn):
            return
    except (psycopg.Error, AgeBootstrapError) as exc:
        _rollback_quietly(conn)
        typer.secho(
            f"communities     WARN — AGE availability probe failed: {exc}",
            fg="yellow",
        )
        return

    from .graph_rag.communities import compute_source_graph_hash
    from .graph_rag.tenancy import resolve_tenant

    try:
        tenant_id = resolve_tenant(cfg)
    except GraphTenantError as exc:
        typer.secho(f"communities     WARN — {exc}", fg="yellow")
        return

    try:
        community_count, member_count = _community_counts(conn, tenant_id)
        stored_hashes = _stored_community_fingerprints(conn, tenant_id)
        current_hash = compute_source_graph_hash(_relationship_edges(conn, tenant_id))
    except psycopg.Error as exc:
        _rollback_quietly(conn)
        typer.secho(
            f"communities     WARN — community probe failed: {exc}", fg="yellow"
        )
        return
    finally:
        _rollback_quietly(conn)

    counts = (
        f"{community_count} communities, {member_count} members, tenant {tenant_id!r}"
    )
    if community_count == 0:
        typer.echo(
            f"communities     OK ({counts}) — none built; run "
            "`brain graphrag communities build`"
        )
        return
    if stored_hashes == {current_hash}:
        typer.echo(f"communities     OK ({counts}, fingerprint current)")
        return
    typer.secho(f"communities     stale ({counts}, fingerprint stale)", fg="yellow")
    typer.secho(
        "                — communities are stale; run `brain graphrag communities "
        "refresh` to rebuild from the current graph",
        fg="yellow",
    )


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
                # Perf wave T1: warn when chunks stats are stale (e.g. post-restore).
                _check_chunks_stats(conn)
                # Plan 09: warn when the quick-capture inbox has grown large.
                _check_inbox_size(conn, cfg)
            else:
                failures.append("pgvector extension not installed (run brain init)")
                typer.echo("postgres        FAIL — pgvector not installed")
            # GraphRAG (wave G0): soft AGE health line — reuses the open
            # connection. Self-contained (catches its own probe errors) so an
            # AGE hiccup never masquerades as a postgres failure below.
            _check_age(conn)
            # GraphRAG (wave G2-h): relational↔AGE graph drift check, only when
            # the people-aspect graph sync is opted into. Self-contained WARN —
            # never flips doctor's exit code (mirrors _check_age).
            if cfg.graph_enabled:
                _check_graph_drift(conn, cfg)
                # GraphRAG (wave G3-g): community counts + stale-fingerprint
                # check (spec §17c). Same gating + self-contained WARN contract
                # as _check_graph_drift; never flips doctor's exit code.
                _check_graph_communities(conn, cfg)
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


@app.command()
def analyze(
    table: str = typer.Argument(
        "chunks",
        help="Table to ANALYZE. Defaults to 'chunks' (the table brain doctor warns about).",
    ),
    all_tables: bool = typer.Option(
        False,
        "--all",
        help="ANALYZE every table in the database instead of a single one.",
    ),
) -> None:
    """Refresh Postgres planner statistics by running ANALYZE.

    Fixes the ``chunks stats WARN — never analyzed`` that ``brain doctor``
    reports after a ``pg_restore``: a restore bulk-loads rows via ``COPY`` but
    never runs ``ANALYZE``, so the planner falls back to default row estimates
    (and bad query plans) until autovacuum's first pass. This command runs the
    ``ANALYZE`` SQL for you against the configured database.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        # ANALYZE writes transactional catalog stats; autocommit (as in `init`)
        # ensures they persist past connection close.
        conn.autocommit = True
        if all_tables:
            conn.execute("ANALYZE")
            typer.secho("ANALYZE (all tables) — done", fg="green")
            return
        known = list_public_tables(conn)
        if table not in known:
            raise typer.BadParameter(
                f"unknown table {table!r}; known tables: {', '.join(known)}",
                param_hint="TABLE",
            )
        analyze_tables(conn, [table])
    typer.secho(f"ANALYZE {table} — done", fg="green")


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


def _build_enricher(cfg: Config) -> OllamaEnricher:
    """Build the configured Ollama enricher.

    Indirected so tests can monkeypatch this single point to swap in a fake
    enricher (mirrors :func:`_build_embedder`). Production code goes
    through this factory so the wave-Q1-D env-var surface
    (``BRAIN_ENRICH_MODEL`` / ``BRAIN_ENRICH_MAX_INPUT_TOKENS`` /
    ``BRAIN_ENRICH_TIMEOUT_SECONDS``) is honored everywhere.
    """
    from .enrichment import make_enricher

    return make_enricher(cfg)


def _build_graph_syncer(cfg: Config) -> GraphSyncer:
    """Build the per-invocation people-aspect graph syncer (wave G1-c).

    Indirected through this single factory (mirrors :func:`_build_embedder` /
    :func:`_build_enricher`) so tests can monkeypatch one point to swap in a
    syncer wired to a live AGE backend, and so every write/delete command in
    this module shares ONE :class:`~brain.graph_rag.reconcile.ReconcileConfig`
    (the factory caches it per :class:`Config`). The syncer self-gates on
    ``BRAIN_GRAPH_ENABLED`` + AGE availability, so building it unconditionally
    is cheap and a no-op on a stock pgvector DB.
    """
    from .graph_rag.sync import make_graph_syncer

    return make_graph_syncer(cfg)


@app.command()
def ingest(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to the document."),
    force: bool = typer.Option(
        False, "--force", help="Re-ingest even if content already exists."
    ),
    no_enrich: bool = typer.Option(
        False, "--no-enrich",
        help=(
            "Skip the local-Ollama auto-summary post-ingest hook (Q1-D). "
            "Default: enrichment runs on every ingest; Ollama-down never "
            "fails the ingest (logged WARN, row stays unenriched)."
        ),
    ),
) -> None:
    """Ingest a single file (TXT/MD/PDF/DOCX)."""
    cfg = Config.load()
    embedder = _build_embedder(cfg)
    enricher = None if no_enrich else _build_enricher(cfg)
    graph_syncer = _build_graph_syncer(cfg)
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
            enricher=enricher,
            enrich=not no_enrich,
            enrich_min_tokens=cfg.enrich_min_tokens,
            graph_syncer=graph_syncer,
        )
    if result.created:
        verb = "ingested"
    elif result.body_changed or force:
        verb = "updated"
    else:
        verb = "skipped (already ingested)"
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
    no_enrich: bool = typer.Option(
        False, "--no-enrich",
        help="Skip the local-Ollama auto-summary post-ingest hook (Q1-D).",
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
    enricher = None if no_enrich else _build_enricher(cfg)
    graph_syncer = _build_graph_syncer(cfg)
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
                    enricher=enricher,
                    enrich=not no_enrich,
                    enrich_min_tokens=cfg.enrich_min_tokens,
                    graph_syncer=graph_syncer,
                )
                if result.created:
                    verb = "ingested"
                elif result.body_changed:
                    verb = "updated"
                else:
                    verb = "skipped"
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
    no_enrich: bool = typer.Option(
        False, "--no-enrich",
        help="Skip the local-Ollama auto-summary post-ingest hook (Q1-D).",
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
    # Wave Q1-D — action-items docs require a parent_meeting_external_id so
    # the parent transcript is discoverable. Without it `brain todo` loses
    # the link; surfacing a clean BadParameter here is friendlier than
    # letting a malformed row into the DB.
    if content_type == "krisp_action_items" and "parent_meeting_external_id" not in meta:
        raise typer.BadParameter(
            "--content-type krisp_action_items requires "
            '--metadata \'{"parent_meeting_external_id": "<id>"}\''
        )
    doc = _stdin_make_doc(
        content=content,
        title=title,
        content_type=content_type,
        metadata=meta,
    )

    cfg = Config.load()
    embedder = _build_embedder(cfg)
    enricher = None if no_enrich else _build_enricher(cfg)
    graph_syncer = _build_graph_syncer(cfg)
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
            enricher=enricher,
            enrich=not no_enrich,
            enrich_min_tokens=cfg.enrich_min_tokens,
            graph_syncer=graph_syncer,
        )
    if result.created:
        verb = "ingested"
    elif result.body_changed or force:
        verb = "updated"
    else:
        verb = "skipped (already ingested)"
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
    no_enrich: bool = typer.Option(
        False, "--no-enrich",
        help="Skip the local-Ollama auto-summary post-ingest hook (Q1-D).",
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
    enricher = None if no_enrich else _build_enricher(cfg)
    graph_syncer = _build_graph_syncer(cfg)
    ingested = 0
    ingested_draft = 0
    skipped = 0
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
                    draft=bool(doc.metadata.get("_is_draft", False)),
                    enricher=enricher,
                    enrich=not no_enrich,
                    enrich_min_tokens=cfg.enrich_min_tokens,
                    graph_syncer=graph_syncer,
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
                    if result.created and doc.metadata.get("_is_draft"):
                        ingested_draft += 1
                else:
                    typer.echo(f"  skipped thread {tid} (unchanged)")
                    skipped += 1
            except (GmailError, psycopg.Error, ValueError, KeyError) as e:
                typer.secho(
                    f"  failed thread {tid} ({len(ts)} messages): {e}",
                    fg="red",
                )
                failed += 1
                continue
    draft_note = f" ({ingested_draft} draft)" if ingested_draft else ""
    typer.echo(
        f"{ingested} ingested{draft_note}, {skipped} skipped (unchanged), "
        f"{failed} failed"
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


# ---------------------------------------------------------------------------
# Wave G1-d — brain graphrag (admin / index ops; Apache AGE people graph)
# ---------------------------------------------------------------------------


def _graphrag_config(cfg: Config, tenant: str | None) -> ReconcileConfig:
    """Resolve the shared :class:`ReconcileConfig`, applying a ``--tenant`` override.

    Starts from the single cached config (:func:`build_reconcile_config`) so a
    build / refresh uses the SAME co-occurrence window + per-doc cap + generic
    ratio + owner keys as the incremental sync hook. ``--tenant`` overrides only
    the tenant id (via :func:`dataclasses.replace` on the frozen config), leaving
    every other knob identical so a backfill cannot diverge from the incremental
    path on weighting.
    """
    from .graph_rag.sync import build_reconcile_config

    base = build_reconcile_config(cfg)
    if tenant is not None and tenant.strip():
        return dataclasses.replace(base, tenant_id=tenant.strip())
    return base


def _require_age_or_exit(conn: psycopg.Connection[Any]) -> None:
    """Exit non-zero with a clear message when this DB image lacks Apache AGE.

    The ``graphrag`` admin commands exist solely to maintain the AGE graph, so —
    unlike ``brain init`` (which has other work and degrades gracefully) — they
    fail loudly when AGE is unavailable, pointing the operator at the AGE image
    cut-over (consistent with ``brain init``'s probe + ``brain doctor``'s WARN).
    """
    if not age_extension_available(conn):
        typer.secho(
            "graphrag: Apache AGE is not available in this database image — "
            "cut over to the AGE image and run `brain init` first "
            f"(see {_AGE_IMAGE_SPEC})",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)


@graphrag_app.command("build")
def graphrag_build(
    backfill: bool = typer.Option(
        False,
        "--backfill",
        help=(
            "Reconcile the people aspect of EVERY existing document into the "
            "graph. Required in G1 (the only build mode this wave); concept "
            "indexing arrives in G2."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Authoritative full rebuild: bypass the per-aspect watermark and "
            "re-reconcile EVERY document from the relational source-of-truth "
            "(entities + MENTIONED_IN + Document vertices + CO_OCCURS). The "
            "recovery path for a dropped or corrupted AGE mirror when documents "
            "and config are unchanged (a plain build would skip them). Implies "
            "--backfill."
        ),
    ),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to build (default: BRAIN_GRAPH_TENANT)."
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Max documents to reconcile (default: all)."
    ),
    concepts: bool = typer.Option(
        False,
        "--concepts",
        help=(
            "Also (re)build the CONCEPT aspect (extract topics/projects/orgs/"
            "tools via the local Ollama extractor) alongside the always-on people "
            "aspect. Explicit opt-in for this run, independent of "
            "BRAIN_GRAPH_CONCEPTS (which gates the ingest-time auto-sync); when "
            "that env is on, concepts are included even without this flag. "
            "Without it (and env off) the build is person-only — unchanged."
        ),
    ),
) -> None:
    """Bulk-reconcile the graph for all existing documents (backfill).

    The batch equivalent of the per-document graph-sync hook: walks every
    document in id order and reconciles its people aspect (and, when ``--concepts``
    or ``BRAIN_GRAPH_CONCEPTS`` is on, its concept aspect) into the Apache AGE
    graph, sharing the same :class:`ReconcileConfig` as the incremental path so
    the result is identical to having reconciled each doc at ingest time.

    Idempotent + resumable: re-running (or resuming after an interruption) skips
    already-indexed documents via the per-aspect watermark. ``--limit`` caps the
    document count; ``--tenant`` scopes the build to one tenant.

    ``--force`` is the authoritative full rebuild — it bypasses the watermark and
    re-reconciles every document from the relational source-of-truth, the
    recovery path for a dropped or corrupted AGE mirror (where docs + config are
    unchanged, so a plain ``--backfill`` would skip every doc). ``--force``
    implies ``--backfill`` and cannot be combined with ``--limit`` (a
    clear-then-partial-rebuild would permanently lose the un-rebuilt rest).
    """
    if force and limit is not None:
        raise typer.BadParameter(
            "--force rebuilds the full corpus and cannot be combined with --limit"
        )
    cfg = Config.load()
    if not (backfill or force):
        typer.echo(
            "graphrag build: pass --backfill to reconcile all existing "
            "documents into the graph (or --force for an authoritative full "
            "rebuild that ignores the watermark). Add --concepts to also build "
            "the concept aspect."
        )
        return

    # Concepts run when the flag is passed OR the env gate is on. The flag is an
    # explicit per-run opt-in independent of BRAIN_GRAPH_CONCEPTS, so an operator
    # can build the concept graph on demand without flipping the ingest-time gate.
    include_concepts = concepts or cfg.graph_concepts
    config = _graphrag_config(cfg, tenant)
    from .graph_rag.backends import AgeBackend
    from .graph_rag.build import build_graph
    from .graph_rag.extract import EntityExtractor, make_extractor

    extractor: EntityExtractor | None = None
    if include_concepts:
        config = dataclasses.replace(config, concepts_enabled=True)
        extractor = make_extractor(cfg)

    # Curated alias rules — wave C3. Loaded once, before opening the AGE
    # connection, so a malformed YAML fails fast without any DB writes. An
    # empty / missing file (the default) is the opt-out: the build behaves
    # exactly as before. Logged-only details: the path is sensitive (real
    # entity names) so we keep it out of the operator footer.
    from .graph_rag.aliases import load_alias_rules, merge_aliases

    alias_rules = load_alias_rules(cfg.graph_aliases_path)

    with connect_age(cfg.database_url) as conn:
        conn.autocommit = True
        _require_age_or_exit(conn)
        backend = AgeBackend()
        backend.bootstrap(conn)
        total = count_documents(conn)
        mode = " (force: ignoring watermark)" if force else ""
        aspects = "people + concepts" if include_concepts else "people"
        typer.echo(
            f"reconciling {aspects} aspect for {total} document(s) "
            f"(tenant {config.tenant_id!r}){mode}"
        )
        document_ids = (
            doc_id for batch in iter_all_document_ids(conn) for doc_id in batch
        )
        result = build_graph(
            conn,
            document_ids,
            backend=backend,
            config=config,
            limit=limit,
            extractor=extractor,
            force=force,
        )
        # Apply curated alias rules at corpus level AFTER the per-doc loop +
        # build_graph's deferred refresh: merge_aliases re-points sources,
        # provisions any newly-created target AGE vertices, then runs another
        # refresh_aggregates so the GC+detach+CO_OCCURS rebuild reflects the
        # merges. Empty rules = no-op (no transaction opened). Atomic per
        # merge_aliases's contract: a failure rolls the alias work back without
        # disturbing the per-doc reconcile that already committed above.
        alias_result = merge_aliases(
            conn, config.tenant_id, alias_rules, backend, config=config
        )
        # Bug A — automatic cross-document concept type-collapse. Runs AFTER the
        # per-doc loop + curated alias merge (so every document's mentions are
        # committed), collapsing any canonical_key fragmented across concept
        # types (org/project/tool/topic) into the precedence winner via the same
        # merge_aliases machinery. Concept types only — never person. Idempotent
        # + a cheap no-op when nothing is fragmented. Gated on ``include_concepts``
        # so a person-only build stays person-only (matches this command's help
        # + the sync.py ``concepts_enabled`` gate).
        collapse_result = None
        rel_count = result.relationship_count
        orphans = result.orphans_removed
        if include_concepts:
            from .graph_rag.cross_type import collapse_cross_type_concepts

            collapse_result = collapse_cross_type_concepts(
                conn, config.tenant_id, backend, config=config
            )
            if collapse_result.rules_applied > 0:
                # The collapse ran its own refresh_aggregates, so build_graph's
                # relationship_count is now stale — re-read the authoritative
                # count for the footer (same pattern as `graphrag refresh`).
                rel_row = conn.execute(
                    "SELECT count(*) FROM graph_relationships WHERE tenant_id = %s",
                    (config.tenant_id,),
                ).fetchone()
                assert rel_row is not None
                rel_count = int(rel_row[0])
                orphans += collapse_result.sources_orphaned
    typer.echo(
        f"graphrag build: {result.processed} processed "
        f"(reconciled {result.reconciled}, skipped {result.skipped}), "
        f"{rel_count} relationship(s) rebuilt, "
        f"{orphans} orphan(s) removed (tenant {config.tenant_id!r})"
    )
    if alias_result.rules_total > 0:
        typer.echo(alias_result_summary(alias_result))
    if collapse_result is not None and collapse_result.rules_total > 0:
        typer.echo(f"cross-type collapse: {alias_result_summary(collapse_result)}")


@graphrag_app.command("refresh")
def graphrag_refresh(
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to refresh (default: BRAIN_GRAPH_TENANT)."
    ),
) -> None:
    """Recompute a tenant's aggregate edges from the source-of-truth.

    The corpus-wide weight/edge recompute, WITHOUT re-resolving any document's
    persons: rebuilds every ``graph_relationships`` edge from
    ``graph_edge_contributions`` (normalized lift + generic suppression) and
    rematerializes the AGE ``CO_OCCURS`` edges. Use after a corpus-wide
    weighting / suppression change (e.g. a new ``BRAIN_GRAPH_GENERIC_DF``).
    Idempotent. Run ``brain graphrag build --backfill`` first — refresh assumes
    the tenant's entity vertices already exist in the graph. For a dropped or
    corrupted AGE mirror (vertices missing), use ``brain graphrag build --force``
    — the authoritative full rebuild — instead.
    """
    cfg = Config.load()
    config = _graphrag_config(cfg, tenant)
    from .graph_rag.aggregates import RefreshResult
    from .graph_rag.aliases import load_alias_rules, merge_aliases
    from .graph_rag.backends import AgeBackend
    from .graph_rag.reconcile import refresh_aggregates

    # Wave C3: alias rules are corpus-level data — a refresh should also fold
    # them in so a knob change that drives a refresh doesn't leave variant
    # entities stranded. Loaded BEFORE the AGE connection so a malformed file
    # fails fast without any DB writes; empty rules = no-op.
    alias_rules = load_alias_rules(cfg.graph_aliases_path)

    with connect_age(cfg.database_url) as conn:
        conn.autocommit = True
        _require_age_or_exit(conn)
        backend = AgeBackend()
        backend.bootstrap(conn)
        # Apply alias merges BEFORE the corpus refresh so the refresh observes
        # the post-merge graph: merge_aliases internally calls refresh_aggregates
        # too, so we skip the second refresh when alias rules ran AND moved at
        # least one rule (the merge's refresh is authoritative). When no rules
        # apply (or the file is empty / missing), behavior is unchanged — the
        # original refresh_aggregates runs and the footer matches the legacy
        # output.
        alias_result = merge_aliases(
            conn, config.tenant_id, alias_rules, backend, config=config
        )
        if alias_result.rules_applied > 0:
            # merge_aliases already ran refresh_aggregates inside its atomic
            # transaction — duplicating it here is wasted work AND would log a
            # second "rebuilt" line that doesn't match reality. Re-read the
            # tenant's current relationship count + orphans-since-last-call for
            # the footer.
            rel_row = conn.execute(
                "SELECT count(*) FROM graph_relationships WHERE tenant_id = %s",
                (config.tenant_id,),
            ).fetchone()
            assert rel_row is not None
            result = RefreshResult(
                tenant_id=config.tenant_id,
                relationship_count=int(rel_row[0]),
                orphans_removed=alias_result.sources_orphaned,
            )
        else:
            result = refresh_aggregates(conn, backend=backend, config=config)
        # Bug A — automatic cross-document concept type-collapse, AFTER the
        # curated merge + corpus refresh, so a knob-change refresh also repairs
        # any concept fragmented across types. Concept types only; idempotent.
        from .graph_rag.cross_type import collapse_cross_type_concepts

        collapse_result = collapse_cross_type_concepts(
            conn, config.tenant_id, backend, config=config
        )
        if collapse_result.rules_applied > 0:
            # The collapse's own refresh_aggregates is now the authoritative
            # tenant state — re-read the relationship count for the footer.
            rel_row = conn.execute(
                "SELECT count(*) FROM graph_relationships WHERE tenant_id = %s",
                (config.tenant_id,),
            ).fetchone()
            assert rel_row is not None
            result = RefreshResult(
                tenant_id=config.tenant_id,
                relationship_count=int(rel_row[0]),
                orphans_removed=result.orphans_removed + collapse_result.sources_orphaned,
            )
    typer.echo(
        f"graphrag refresh: {result.relationship_count} relationship(s), "
        f"{result.orphans_removed} orphan(s) removed (tenant {config.tenant_id!r})"
    )
    if alias_result.rules_total > 0:
        typer.echo(alias_result_summary(alias_result))
    if collapse_result.rules_total > 0:
        typer.echo(f"cross-type collapse: {alias_result_summary(collapse_result)}")


# ---------------------------------------------------------------------------
# Wave G2-h — brain graphrag retrieval surfaces (search / themes / entity).
# Full CLI↔MCP parity arrives in G2-i; this wave is CLI-only. NEVER expose raw
# Cypher — every command takes structured params and the backend injects
# tenant_id + caps (spec §9).
# ---------------------------------------------------------------------------


def _graphrag_search_or_exit(
    cfg: Config,
    query: str,
    *,
    mode: str,
    tenant: str | None,
    person: str | None,
    depth: int | None,
    limit: int | None,
    synthesize: bool,
) -> GraphContext:
    """Open an AGE connection, run :func:`graph_rag_search`, map core errors.

    The single construction + error-mapping seam shared by ``brain graphrag
    search`` / ``themes`` / ``entity`` (mirrors how ``build`` / ``refresh`` open
    an AGE-capable autocommit connection + bootstrap the backend). Local seed
    resolution + the snippet path are FTS-only, so an embedder is needed ONLY
    for the ``global`` (community) path's vector leg AND the ``fuse`` hybrid
    leg (spec §17c Q9; perf-T4 G5). The embedder is constructed ONCE here
    (eagerly, not via a factory) only when the mode actually needs it — global
    / fuse / auto — and passed through ``graph_rag_search``'s ``embedder``
    param; local / themes / entity skip construction entirely. The enricher is
    built only for the opt-in ``--synthesize`` group-summary path.

    Error → exit-code mapping (spec §17b decision 4 + repo error contract; the
    G3-e flip means explicit ``--mode global`` now EXECUTES, so the former
    ``GraphModeUnavailable`` reject is gone — §17c Q6):

    * :class:`PersonNotFound` / :class:`PersonAmbiguous` (themes resolver) →
      clean red CLI error, exit 1 (no traceback).
    * :class:`GraphTenantError` / :class:`GraphBackendError` → clean red CLI
      error, exit 1 (no traceback).
    * ``ValueError`` (themes mode with no resolvable person, or an unknown mode
      surfaced by the router) → :class:`typer.BadParameter` (exit 2).

    AGE-absent is handled before retrieval by :func:`_require_age_or_exit`
    (exit 1), identical to the ``build`` / ``refresh`` admin commands.
    """
    from .graph_rag import graph_rag_search
    from .graph_rag.backends import AgeBackend

    enricher = _build_enricher(cfg) if synthesize else None
    # Build the embedder ONCE up-front (perf-T4 G5) only when the mode might
    # need it — global, fuse, or auto (which may route to global). Local /
    # themes / entity stay embedder-free, matching the prior lazy-factory
    # contract: never construct an embedder we wouldn't use.
    embedder = (
        _build_embedder(cfg) if mode in {"global", "fuse", "auto"} else None
    )
    try:
        with connect_age(cfg.database_url) as conn:
            conn.autocommit = True
            _require_age_or_exit(conn)
            backend = AgeBackend()
            backend.bootstrap(conn)
            return graph_rag_search(
                conn,
                cfg,
                query,
                backend=backend,
                tenant=tenant,
                depth=depth,
                limit=limit,
                mode=mode,
                person=person,
                synthesize=synthesize,
                enricher=enricher,
                embedder=embedder,
            )
    except (PersonNotFound, PersonAmbiguous) as exc:
        typer.secho(f"graphrag: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    except (GraphTenantError, GraphBackendError) as exc:
        typer.secho(f"graphrag: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        # Router caller-bug surface: themes mode with no resolvable person, or an
        # unrecognized --mode value. Both are usage errors → BadParameter.
        raise typer.BadParameter(str(exc)) from exc


def _emit_graph_context(ctx: GraphContext, *, json_output: bool) -> None:
    """Render a :class:`GraphContext` (full JSON, or the human renderable)."""
    if json_output:
        emit_json(graph_context_json(ctx))
        return
    console.print(graph_context_renderable(ctx))


@graphrag_app.command("search")
def graphrag_search(
    query: str = typer.Argument(..., help="Free-text graph retrieval query."),
    mode: str = typer.Option(
        "auto",
        "--mode",
        help=(
            "Retrieval mode: auto (heuristic router, default) | local "
            "(entity-centric) | themes (scope-first 'themes with X', requires "
            "--person) | global (community-level RRF over detected communities; "
            "run `brain graphrag communities build` first) | fuse (RRF of the "
            "local-graph doc leg with the vector/FTS hybrid leg)."
        ),
    ),
    person: str | None = typer.Option(
        None,
        "--person",
        help="Scope themes to this person (resolved via the directory).",
    ),
    depth: int | None = typer.Option(
        None, "--depth", help="Traversal depth (default: BRAIN_GRAPH_DEPTH)."
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Max documents returned (default: 10)."
    ),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to query (default: BRAIN_GRAPH_TENANT)."
    ),
    json_output: bool = typer.Option(False, "--json"),
    synthesize: bool = typer.Option(
        False,
        "--synthesize",
        help=(
            "Attach a best-effort local-Ollama summary to each theme group "
            "(opt-in; never required for retrieval — a missing/failed Ollama "
            "yields summary=None + a WARN)."
        ),
    ),
) -> None:
    """Graph retrieval over the Apache AGE people/concept graph (spec §6/§9).

    ``--mode auto`` (default) runs the heuristic router: a thematic query with a
    resolvable person → themes; a thematic query with no person → global (the
    community path — the G3-e flip, spec §17c Q6, no longer a global→local
    degradation); otherwise local. An explicit ``--mode global`` executes the
    community-level RRF (spec §6c) — build the communities first via
    ``brain graphrag communities build``. An explicit ``--mode fuse`` (wave G4-c,
    spec §17d Q1) RRF-merges the local-graph doc leg with the vector/FTS hybrid
    leg into one ranked doc list (per-doc leg provenance lands in the ``--json``
    ``explanation.matched_filters.fuse_doc_provenance``); ``fuse`` is
    explicit-only — ``auto`` never routes to it. No raw Cypher is ever accepted
    or shown — the backend injects the tenant + caps.
    """
    cfg = Config.load()
    ctx = _graphrag_search_or_exit(
        cfg,
        query,
        mode=mode,
        tenant=tenant,
        person=person,
        depth=depth,
        limit=limit,
        synthesize=synthesize,
    )
    _emit_graph_context(ctx, json_output=json_output)


@graphrag_app.command("themes")
def graphrag_themes(
    person: str = typer.Option(
        ...,
        "--person",
        help="Required: the person X to scope 'themes in my conversations with X'.",
    ),
    depth: int | None = typer.Option(
        None, "--depth", help="Traversal depth (default: BRAIN_GRAPH_DEPTH)."
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Max documents returned (default: 10)."
    ),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to query (default: BRAIN_GRAPH_TENANT)."
    ),
    json_output: bool = typer.Option(False, "--json"),
    synthesize: bool = typer.Option(
        False,
        "--synthesize",
        help="Attach a best-effort Ollama summary per theme group (opt-in).",
    ),
) -> None:
    """The 'themes in my conversations with X' headline (spec §6b).

    A convenience wrapper for ``graphrag search --mode themes --person X``:
    scopes to X, groups X's co-occurrence subgraph, and returns ranked theme
    groups (key entities + representative X-docs). ``--person`` is required;
    omitting it is a usage error.
    """
    cfg = Config.load()
    ctx = _graphrag_search_or_exit(
        cfg,
        "",
        mode="themes",
        tenant=tenant,
        person=person,
        depth=depth,
        limit=limit,
        synthesize=synthesize,
    )
    _emit_graph_context(ctx, json_output=json_output)


@graphrag_app.command("entity")
def graphrag_entity(
    name: str = typer.Argument(..., help="Entity name or canonical key to inspect."),
    depth: int | None = typer.Option(
        None, "--depth", help="Neighbourhood depth (default: BRAIN_GRAPH_DEPTH)."
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Max documents returned (default: 10)."
    ),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to query (default: BRAIN_GRAPH_TENANT)."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show a single entity's neighbourhood (spec §9).

    A thin wrapper over local (entity-centric) retrieval seeded on ``name``: it
    resolves the entity, traverses its bounded ``CO_OCCURS`` neighbourhood, and
    returns the seed + reached entities and their documents. No new traversal
    logic — it reuses the same path as ``graphrag search --mode local``.
    """
    cfg = Config.load()
    ctx = _graphrag_search_or_exit(
        cfg,
        name,
        mode="local",
        tenant=tenant,
        person=None,
        depth=depth,
        limit=limit,
        synthesize=False,
    )
    _emit_graph_context(ctx, json_output=json_output)


# ---------------------------------------------------------------------------
# Wave G3-f — brain graphrag communities (global-mode admin: build / refresh /
# list). Communities are RELATIONAL-only (spec §17c Q2/Q3): build runs Louvain
# over graph_relationships, persists graph_communities/_members, then EAGERLY
# (best-effort) summarizes + embeds each community. Construction mirrors
# build/refresh (connect_age + AGE guard + resolve_tenant). NEVER raw Cypher.
# ---------------------------------------------------------------------------

communities_app = typer.Typer(
    name="communities",
    help=(
        "Global-mode community admin: build / refresh / list the detected "
        "communities that `brain graphrag search --mode global` ranks."
    ),
    no_args_is_help=False,
)
graphrag_app.add_typer(communities_app, name="communities")


def _run_communities_build(
    cfg: Config,
    *,
    tenant: str | None,
    limit: int | None,
    json_output: bool,
    force: bool,
) -> None:
    """Build (``force=False``) / refresh (``force=True``) + summarize communities.

    The shared core of ``communities build`` / ``communities refresh`` (spec
    §17c Q3): resolves the tenant, runs :func:`build_communities` (the dirty gate
    SKIPS an unchanged graph unless ``force``), then :func:`summarize_communities`
    (best-effort Ollama + embedder — an unreachable enricher leaves summaries
    NULL, while a missing/broken embedder still writes summaries and only skips
    the embeddings; either way the build still succeeds; spec §17c Q10). The
    embedder is constructed defensively (mirroring
    :func:`brain.graph_rag.global_._vector_ranked_keys`' lazy-catch) so a
    ``make_embedder`` failure (misconfig, Ollama unreachable) degrades to
    summaries-without-embeddings rather than crashing the build. Reports both
    tallies (human or ``--json``). ``limit`` caps how many stale/new communities
    are (re)summarized this run (it does NOT cap detection — Louvain always
    partitions the full edge set).
    """
    from .graph_rag.communities import build_communities
    from .graph_rag.communities_summary import summarize_communities
    from .graph_rag.tenancy import resolve_tenant

    tenant_id = resolve_tenant(cfg, tenant)
    enricher = _build_enricher(cfg)
    # Construct the embedder defensively (mirrors global_._vector_ranked_keys'
    # lazy-catch): a missing/broken embedder (e.g. ConfigError from
    # make_embedder, Ollama unreachable) must NOT crash `communities build` nor
    # block summaries. On failure, fall back to embedder=None so
    # summarize_communities still writes summaries and leaves summary_embedding
    # NULL (global retrieval degrades that community to FTS-only; §17c Q10).
    embedder: Embedder | None
    try:
        embedder = _build_embedder(cfg)
    except Exception as exc:  # noqa: BLE001 — best-effort: never crash the build
        logger.warning(
            "graphrag communities build: embedder construction failed (%s); "
            "writing summaries without embeddings (FTS-only)",
            exc,
        )
        embedder = None
    with connect_age(cfg.database_url) as conn:
        conn.autocommit = True
        _require_age_or_exit(conn)
        build_result = build_communities(conn, cfg, tenant=tenant_id, force=force)
        summary_result = summarize_communities(
            conn,
            cfg,
            tenant=tenant_id,
            enricher=enricher,
            embedder=embedder,
            limit=limit,
        )

    if json_output:
        emit_json(
            {
                "tenant_id": tenant_id,
                "build": {
                    "communities_total": build_result.communities_total,
                    "created": build_result.created,
                    "reused": build_result.reused,
                    "deleted": build_result.deleted,
                    "dirty": build_result.dirty,
                    "skipped": build_result.skipped,
                },
                "summary": {
                    "candidates": summary_result.candidates,
                    "summarized": summary_result.summarized,
                    "summary_failures": summary_result.summary_failures,
                    "embedded": summary_result.embedded,
                    "embed_failures": summary_result.embed_failures,
                    "skipped": summary_result.skipped,
                },
            }
        )
        return

    verb = "refresh" if force else "build"
    if build_result.skipped:
        typer.echo(
            f"graphrag communities {verb}: graph unchanged — skipped "
            f"({build_result.communities_total} community/-ies, "
            f"tenant {tenant_id!r})"
        )
    else:
        typer.echo(
            f"graphrag communities {verb}: {build_result.communities_total} "
            f"community/-ies (created {build_result.created}, reused "
            f"{build_result.reused}, deleted {build_result.deleted}, "
            f"tenant {tenant_id!r})"
        )
    summary_tail = (
        " (enricher unavailable — summaries skipped)"
        if summary_result.skipped
        else ""
    )
    typer.echo(
        f"summaries: {summary_result.summarized} written, "
        f"{summary_result.summary_failures} failed, "
        f"{summary_result.embedded} embedded, "
        f"{summary_result.embed_failures} embed failure(s)" + summary_tail
    )


def _run_communities_list(
    cfg: Config,
    *,
    tenant: str | None,
    limit: int | None,
    json_output: bool,
) -> None:
    """List a tenant's materialized communities (the ``communities list`` core).

    Resolves the tenant and reads the stored ``graph_communities`` rows via
    :func:`brain.graph_rag.communities.list_communities` (largest-first,
    ``--limit`` capped), then renders the admin table or the ``--json`` payload
    (``{tenant_id, count, communities:[…]}``). Read-only; no raw Cypher.
    """
    from .graph_rag.communities import list_communities
    from .graph_rag.tenancy import resolve_tenant

    tenant_id = resolve_tenant(cfg, tenant)
    with connect_age(cfg.database_url) as conn:
        conn.autocommit = True
        _require_age_or_exit(conn)
        records = list_communities(conn, tenant_id, limit=limit)

    if json_output:
        emit_json(
            {
                "tenant_id": tenant_id,
                "count": len(records),
                "communities": [community_record_json(r) for r in records],
            }
        )
        return
    console.print(community_records_table(records))


@communities_app.callback(invoke_without_command=True)
def communities_default(ctx: typer.Context) -> None:
    """List communities when ``brain graphrag communities`` is run bare.

    Defaults the bare group invocation to ``list`` (with default tenant + no
    limit). An explicit subcommand (``build`` / ``refresh`` / ``list``) suppresses
    this so only the subcommand runs.
    """
    if ctx.invoked_subcommand is not None:
        return
    _run_communities_list(Config.load(), tenant=None, limit=None, json_output=False)


@communities_app.command("build")
def graphrag_communities_build(
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to build (default: BRAIN_GRAPH_TENANT)."
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help="Max stale/new communities to (re)summarize this run (default: all).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Detect + persist + summarize the tenant's communities (spec §17c Q3).

    Runs Louvain over the tenant's relational ``graph_relationships`` edges and
    persists the partition to ``graph_communities`` / ``graph_community_members``,
    then EAGERLY (best-effort) summarizes + embeds each community. The dirty gate
    SKIPS when the graph is unchanged since the last build — use ``communities
    refresh`` to force a rebuild. A missing/unreachable Ollama leaves summaries
    NULL and the build still succeeds (the global path then ranks FTS-only).
    """
    cfg = Config.load()
    _run_communities_build(
        cfg, tenant=tenant, limit=limit, json_output=json_output, force=False
    )


@communities_app.command("refresh")
def graphrag_communities_refresh(
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to refresh (default: BRAIN_GRAPH_TENANT)."
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help="Max stale/new communities to (re)summarize this run (default: all).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Force a community rebuild regardless of the dirty gate (spec §17c Q3).

    Identical to ``communities build`` except it bypasses the
    ``(build_version, source_graph_hash)`` dirty gate — Louvain + the relational
    replace always run, then the eager (best-effort) summary/embedding pass. Use
    after a corpus-wide weighting / suppression change, or to recover after a
    knob change that should re-partition an unchanged graph.
    """
    cfg = Config.load()
    _run_communities_build(
        cfg, tenant=tenant, limit=limit, json_output=json_output, force=True
    )


@communities_app.command("list")
def graphrag_communities_list(
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to list (default: BRAIN_GRAPH_TENANT)."
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n", help="Max communities to show (default: all)."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List the tenant's materialized communities (admin view; spec §17c Q3).

    Shows each stored community's short key, member/edge counts, total weight,
    and a summary preview (largest-first). ``--json`` emits the structured
    ``{tenant_id, count, communities:[…]}`` payload. Read-only — never raw Cypher.
    """
    cfg = Config.load()
    _run_communities_list(cfg, tenant=tenant, limit=limit, json_output=json_output)


# ---------------------------------------------------------------------------
# Wave C3 — brain graphrag aliases (curated entity-merge admin: apply). Mirrors
# the `communities` nested Typer group so the parity guard maps `aliases apply`
# to the MCP twin `brain_graphrag_aliases_apply` 1:1. Real rules live in a
# gitignored local YAML; an empty / missing file is the opt-out (no-op).
# ---------------------------------------------------------------------------

aliases_app = typer.Typer(
    name="aliases",
    help="Curated entity alias/merge admin (apply rules + refresh aggregates).",
    no_args_is_help=True,
)
graphrag_app.add_typer(aliases_app, name="aliases")


@aliases_app.command("apply")
def graphrag_aliases_apply(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Report would-be re-pointed mentions / contributions without "
            "writing. The full apply+upsert+refresh transaction rolls back."
        ),
    ),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to apply rules to (default: BRAIN_GRAPH_TENANT)."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Apply curated entity alias/merge rules + refresh aggregates (C3).

    Loads rules from ``BRAIN_GRAPH_ALIASES_PATH`` (default
    ``$BRAIN_HOME/graph_aliases.yml`` when present, else nothing) and runs
    :func:`brain.graph_rag.aliases.merge_aliases` atomically — re-points every
    source entity's mentions + contributions onto its rule target, provisions
    any newly-created target AGE vertices, then runs ``refresh_aggregates`` so
    the GC + AGE-detach + ``CO_OCCURS`` rebuild reflects the merges.

    ``--dry-run`` rolls the whole transaction back and reports what WOULD have
    moved. After a non-dry, non-empty apply the community partition may be
    stale — re-run ``brain graphrag communities refresh`` (a hint is printed
    to remind the operator). A missing / empty rules file is a no-op that
    prints a short note and exits 0.
    """
    cfg = Config.load()
    from .graph_rag.aliases import load_alias_rules, merge_aliases
    from .graph_rag.backends import AgeBackend

    config = _graphrag_config(cfg, tenant)
    alias_rules = load_alias_rules(cfg.graph_aliases_path)
    if not alias_rules:
        msg = (
            "graphrag aliases apply: no rules configured — set "
            "BRAIN_GRAPH_ALIASES_PATH to a YAML file with a non-empty `rules:` "
            "list, or copy `aliases/default.yml.example` and adapt."
        )
        if json_output:
            # 7 AliasResult fields + ``communities_refresh_recommended`` to
            # match the MCP empty-rules wire shape (C4 parity — both surfaces
            # MUST emit the same 8-key payload so a consumer can assume one
            # schema regardless of transport). Empty rules can't dirty the
            # community partition, so the hint is always ``False`` here.
            emit_json(
                {
                    "tenant_id": config.tenant_id,
                    "rules_total": 0,
                    "rules_applied": 0,
                    "mentions_repointed": 0,
                    "contributions_repointed": 0,
                    "sources_orphaned": 0,
                    "dry_run": dry_run,
                    "communities_refresh_recommended": False,
                }
            )
        else:
            typer.echo(msg)
        return

    with connect_age(cfg.database_url) as conn:
        conn.autocommit = True
        _require_age_or_exit(conn)
        backend = AgeBackend()
        backend.bootstrap(conn)
        res = merge_aliases(
            conn, config.tenant_id, alias_rules, backend,
            dry_run=dry_run, config=config,
        )

    if json_output:
        emit_json(alias_result_json(res))
        return
    typer.echo(alias_result_summary(res))
    if not res.dry_run and res.rules_applied > 0:
        typer.echo(
            "                — communities may be stale; run "
            "`brain graphrag communities refresh`"
        )


# ---------------------------------------------------------------------------
# brain graphrag entities / stats — entity enumeration (plan 2026-05-23)
# ---------------------------------------------------------------------------


class _EntityType(enum.StrEnum):
    """Allowed entity types for ``brain graphrag entities --type``."""

    org = "org"
    project = "project"
    tool = "tool"
    topic = "topic"
    person = "person"


class _EntitySort(enum.StrEnum):
    """Sort order for ``brain graphrag entities --sort``."""

    docs = "docs"
    name = "name"


def _run_entities_list(
    cfg: Config,
    *,
    tenant: str | None,
    entity_type: str | None,
    sort: str,
    limit: int,
    json_output: bool,
) -> None:
    """List a tenant's entities (the ``graphrag entities`` core).

    Resolves the tenant and reads ``graph_entities`` rows via
    :func:`brain.graph_rag.relational.list_entities` (filtered, sorted,
    limit-capped), then renders the admin table or the ``--json`` payload
    (``{tenant_id, count, entities:[…]}``). Read-only; no raw Cypher.
    """
    from .graph_rag.relational import list_entities
    from .graph_rag.tenancy import resolve_tenant

    tenant_id = resolve_tenant(cfg, tenant)
    with connect_age(cfg.database_url) as conn:
        conn.autocommit = True
        _require_age_or_exit(conn)
        rows = list_entities(conn, tenant_id, entity_type=entity_type, sort=sort, limit=limit)

    if json_output:
        emit_json(
            {
                "tenant_id": tenant_id,
                "count": len(rows),
                "entities": [entity_summaries_json(r) for r in rows],
            }
        )
        return
    console.print(entity_summaries_table(rows))


def _run_graph_stats(
    cfg: Config,
    *,
    tenant: str | None,
    json_output: bool,
) -> None:
    """Show a tenant's graph overview (the ``graphrag stats`` core).

    Resolves the tenant, reads counts from the relational tables via
    :func:`brain.graph_rag.relational.graph_stats`, then renders the stats
    table + top-entities table, or the ``--json`` payload. Read-only.
    """
    from .graph_rag.relational import graph_stats
    from .graph_rag.tenancy import resolve_tenant

    tenant_id = resolve_tenant(cfg, tenant)
    with connect_age(cfg.database_url) as conn:
        conn.autocommit = True
        _require_age_or_exit(conn)
        stats = graph_stats(conn, tenant_id)

    if json_output:
        emit_json({"tenant_id": tenant_id, **graph_stats_json(stats)})
        return
    console.print(graph_stats_table(stats))
    if stats.top_entities:
        console.print(entity_summaries_table(list(stats.top_entities)))


@graphrag_app.command("entities")
def graphrag_entities(
    entity_type: _EntityType | None = typer.Option(
        None,
        "--type",
        help="Filter to one entity type (org / project / tool / topic / person).",
    ),
    sort: _EntitySort = typer.Option(
        _EntitySort.docs,
        "--sort",
        help="Sort order: docs (doc_count DESC) or name (name ASC).",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Max entities to show (default 50; 0 = all).",
    ),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to list (default: BRAIN_GRAPH_TENANT)."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List the tenant's entities (admin view; all types or filtered by --type).

    Shows entity type, name, doc_count, and a description preview, sorted by
    doc_count descending (default) or alphabetically (``--sort name``).
    ``--json`` emits ``{tenant_id, count, entities:[…]}``.
    Read-only — never raw Cypher.
    """
    cfg = Config.load()
    _run_entities_list(
        cfg,
        tenant=tenant,
        entity_type=entity_type.value if entity_type is not None else None,
        sort=sort.value,
        limit=limit,
        json_output=json_output,
    )


@graphrag_app.command("stats")
def graphrag_stats(
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to summarize (default: BRAIN_GRAPH_TENANT)."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show an at-a-glance graph overview for the tenant.

    Reports entity counts by type, total relationships, total communities, and
    the top-10 entities by doc_count. ``--json`` emits ``{tenant_id,
    counts_by_type, total_entities, total_relationships, total_communities,
    top_entities:[…]}``. Read-only — never raw Cypher.
    """
    cfg = Config.load()
    _run_graph_stats(cfg, tenant=tenant, json_output=json_output)


# ---------------------------------------------------------------------------
# Wave Q1-D — brain enrich
# ---------------------------------------------------------------------------


# `brain enrich --krisp-action-items` prints plain-English instructions for
# Claude to execute against the Krisp MCP. The CLI does NOT call MCP itself
# (R4 — MCP tools live in agent context, not the Python process), and it
# does NOT invent MCP parameter names. Why: per
# ``docs/specs/2026-04-24-second-brain-design.md:305-306`` the actual Krisp
# flow uses two tools (``search_meetings`` / ``list_activities`` to
# enumerate, then ``get_multiple_documents`` to fetch transcripts), and the
# Python process has no access to either tool's parameter schema. Anything
# the CLI prints about kwargs is speculation that may not match the live
# MCP signature, which is exactly what Codex's stop-time review caught
# twice (first my fake ``list_action_items`` tool, then my fake
# ``query=`` / ``start_date=`` / ``end_date=`` / ``meeting_id=`` kwargs on
# ``search_meetings``).
#
# Correct contract:
#
# 1. Name the tools by exact ``mcp__claude_ai_Krisp__*`` identifier so the
#    agent knows which surface to call.
# 2. Describe the lookback window in PLAIN ENGLISH (concrete ISO date
#    string from ``--since N``) so the agent can pass it to whichever
#    parameter the real tool exposes. No invented kwarg names.
# 3. Show the LITERAL ``brain ingest-stdin`` invocation, which IS owned
#    by this CLI and whose flag shape we control.
# 4. Let the agent extract the action items from whichever field the
#    transcript payload exposes (Krisp renders them as a Markdown
#    checklist in Notes, or as a structured field — the agent decides).
_KRISP_ACTION_ITEMS_OUTPUT = """\
Krisp action-items pipeline — Claude orchestrates the MCP call.
The brain CLI cannot reach MCP tools and does not know their parameter
schemas; only the agent does. Follow these steps:

  1. Enumerate Krisp meetings{window} using either
       mcp__claude_ai_Krisp__search_meetings
     or
       mcp__claude_ai_Krisp__list_activities
     (whichever your Krisp MCP surface exposes — call with the standard
     date/scope params for that tool).{meeting_filter}

  2. For each meeting, fetch its transcript / Note via
       mcp__claude_ai_Krisp__get_multiple_documents
     (passing the meeting id from step 1's response).

  3. Extract the action-items section from each transcript. Krisp renders
     it as a Markdown checklist (``- [ ] item``) in the Note body, or as
     a structured field in the API response. Either source works.

  4. For each meeting's extracted action items, run:

       echo '<action-items markdown body>' | brain ingest-stdin \\
           --source krisp \\
           --external-id "<meeting_id>--action-items" \\
           --content-type krisp_action_items \\
           --title "Action items: <meeting title>" \\
           --metadata '{{"parent_meeting_external_id": "<meeting_id>"}}'

The ``parent_meeting_external_id`` key is required at the ingest-stdin
CLI boundary (BadParameter otherwise) so ``brain todo`` can trace each
item back to the originating Krisp meeting. The ``--external-id`` suffix
``--action-items`` keeps the action-items child row from colliding with
its parent transcript on the ``sources(kind, external_id)`` UNIQUE
constraint.
"""


def _krisp_action_items_window(since_days: int | None) -> str:
    """Render the plain-English lookback window for the printed handoff.

    Computes a concrete ISO 8601 ``since`` date from ``--since N``
    (now - N days) and embeds it in the output so the agent can pass it
    to whichever date parameter the real MCP tool uses. No invented kwarg
    name — just the date string the agent needs.
    """
    if since_days is None:
        return ""
    from datetime import timedelta

    now = datetime.now(tz=UTC).replace(microsecond=0)
    since = (now - timedelta(days=since_days)).isoformat()
    return f" since {since}"


def _krisp_action_items_meeting_filter(source_id: str | None) -> str:
    """Render the optional meeting-id scope hint.

    When ``--source-id`` is set, the agent should restrict the pipeline to
    a single meeting. The hint is plain English (no fake kwarg) so the
    agent passes the id to whichever parameter the real tool accepts.
    """
    if source_id is None:
        return ""
    return (
        f"\n     Scope to one meeting: meeting_id == {source_id!r} "
        f"(filter the response or pass to whichever parameter your "
        f"Krisp MCP exposes)."
    )


def _enrich_backfill(
    cfg: Config,
    *,
    enricher: OllamaEnricher,
    limit: int | None,
    remodel: bool,
) -> int:
    """Drive the ``brain enrich --backfill`` loop.

    Returns the number of rows for which ``documents.summary`` was written.
    Per-row enrichment failures (malformed JSON, persistent errors) are
    logged at WARN; the loop continues to the next row. Ollama unavailable
    on the very first row exits with code 1 (the user almost certainly
    forgot to start Ollama; bailing out is friendlier than logging the
    same warning for every row).

    Eligibility selector:

    - ``remodel=False`` (default) → only ``summary IS NULL``. Backed by
      partial index ``idx_documents_summary_null``.
    - ``remodel=True`` → also re-enrich rows where ``summary_model`` differs
      from the current ``BRAIN_ENRICH_MODEL`` (propagates a model upgrade
      to the whole corpus; falls through to a sequential scan).
    """
    selector_model = enricher.model if remodel else None
    updated = 0
    failed = 0
    first_row = True
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        total = count_unenriched_documents(conn, current_model=selector_model)
        if total == 0:
            if remodel:
                typer.echo(
                    "nothing to enrich (all documents have a summary from "
                    f"{enricher.model})"
                )
            else:
                typer.echo("nothing to enrich (no documents with NULL summary)")
            return 0
        ceiling = limit if limit is not None else total
        if remodel:
            typer.echo(
                f"enriching up to {ceiling} of {total} doc(s) "
                f"(NULL summary OR summary_model != {enricher.model})"
            )
        else:
            typer.echo(
                f"enriching up to {ceiling} of {total} doc(s) "
                "(NULL summary only — pass --remodel to also re-enrich "
                "rows from a different model)"
            )
        for batch in iter_unenriched_documents(conn, current_model=selector_model):
            for row in batch:
                if limit is not None and updated >= limit:
                    if failed:
                        typer.echo(
                            f"enriched {updated} document(s); {failed} failed "
                            "(see warnings)"
                        )
                    else:
                        typer.echo(f"enriched {updated} document(s)")
                    return updated
                if enricher.count_tokens(row.content) < cfg.enrich_min_tokens:
                    # Same skip rule as the post-ingest hook — short content
                    # never warrants a summary.
                    continue
                try:
                    result = enricher.summarize(row.title, row.content)
                except OllamaUnavailable as exc:
                    if first_row:
                        typer.secho(
                            f"Ollama unavailable: {exc}\n"
                            "Is Ollama running? (`brew services start ollama` "
                            "on macOS)",
                            fg="red",
                            err=True,
                        )
                        raise typer.Exit(code=1) from exc
                    typer.secho(
                        f"  WARN: {row.id[:8]} Ollama unavailable: {exc}",
                        fg="yellow",
                        err=True,
                    )
                    failed += 1
                    continue
                except EnrichmentError as exc:
                    typer.secho(
                        f"  WARN: {row.id[:8]} enrichment failed: {exc}",
                        fg="yellow",
                        err=True,
                    )
                    failed += 1
                    continue
                finally:
                    first_row = False
                conn.execute(
                    "UPDATE documents SET summary=%s, summary_model=%s, "
                    "summary_at=NOW() WHERE id=%s",
                    (result.summary, result.model, row.id),
                )
                updated += 1
                typer.echo(f"  enriched {row.id[:8]} {row.title[:60]}")
    if failed:
        typer.echo(
            f"enriched {updated} document(s); {failed} failed (see warnings)"
        )
    else:
        typer.echo(f"enriched {updated} document(s)")
    return updated


@app.command()
def enrich(
    backfill: bool = typer.Option(
        False, "--backfill",
        help=(
            "Backfill documents whose summary IS NULL. Pass --remodel to "
            "additionally re-enrich rows whose summary_model differs from "
            "the current BRAIN_ENRICH_MODEL."
        ),
    ),
    remodel: bool = typer.Option(
        False, "--remodel",
        help=(
            "With --backfill: also re-enrich rows whose summary_model "
            "differs from the current BRAIN_ENRICH_MODEL. Default is to "
            "leave existing summaries untouched even after a model swap."
        ),
    ),
    krisp_action_items: bool = typer.Option(
        False, "--krisp-action-items",
        help=(
            "Print the Krisp MCP request shape that Claude should execute "
            "to pull action items, then exit. The CLI does NOT call MCP "
            "itself — Claude pipes the action items back via "
            "`brain ingest-stdin --content-type krisp_action_items`."
        ),
    ),
    limit: int | None = typer.Option(
        None, "--limit", "-n",
        help="Max docs to enrich in --backfill mode.",
    ),
    since: int | None = typer.Option(
        None, "--since",
        help="Days lookback (used by --krisp-action-items only).",
    ),
    source_id: str | None = typer.Option(
        None, "--source-id",
        help="Restrict --krisp-action-items to one transcript id.",
    ),
) -> None:
    """Catch-up enrichment runner — auto-summary + Krisp action items.

    Two mutually-exclusive modes (BadParameter if both/neither):

    - ``--backfill`` iterates rows that need enrichment and calls the
      enricher for each. By default only ``summary IS NULL`` rows are
      selected (partial index ``idx_documents_summary_null``); pass
      ``--remodel`` to additionally re-enrich rows whose ``summary_model``
      differs from the current ``BRAIN_ENRICH_MODEL`` (sequential scan,
      acceptable at personal-corpus scale and only material right after a
      model upgrade). Honors ``--limit``; idempotent — re-runs pick up
      only still-stale rows.

    - ``--krisp-action-items`` prints copy-pasteable MCP + ingest-stdin
      commands Claude can run to pull action items from Krisp, then exits
      0 without contacting MCP itself.
    """
    if backfill and krisp_action_items:
        raise typer.BadParameter(
            "--backfill and --krisp-action-items are mutually exclusive"
        )
    # Order matters: check --remodel before the generic missing-mode guard
    # so that `brain enrich --remodel` (no --backfill) yields the actionable
    # message instead of a vague "expected --backfill or --krisp-action-items".
    if remodel and not backfill:
        raise typer.BadParameter("--remodel requires --backfill")
    if not backfill and not krisp_action_items:
        raise typer.BadParameter(
            "expected --backfill or --krisp-action-items"
        )
    if krisp_action_items:
        typer.echo(
            _KRISP_ACTION_ITEMS_OUTPUT.format(
                window=_krisp_action_items_window(since),
                meeting_filter=_krisp_action_items_meeting_filter(source_id),
            )
        )
        return
    cfg = Config.load()
    enricher = _build_enricher(cfg)
    _enrich_backfill(cfg, enricher=enricher, limit=limit, remodel=remodel)


def _reconcile_tag_flags(
    tag: str | None, has_tag: str | None
) -> str | None:
    """Reconcile ``--tag`` and its ``--has-tag`` alias for ``search`` / ``explain``.

    ``--has-tag`` is a strict alias of ``--tag`` per plan D3. Both flags
    add the same ``%s = ANY(d.tags)`` predicate; supplying both with
    different values is a user error and exits with ``BadParameter``.
    Returns the single effective tag value to thread into ``hybrid_search``.
    """
    if tag is not None and has_tag is not None and tag != has_tag:
        raise typer.BadParameter(
            "--tag and --has-tag both given with different values"
        )
    return tag if tag is not None else has_tag


def _resolve_search_person(
    conn: psycopg.Connection[Any], person: str | None
) -> PersonMatch | None:
    """Resolve a ``--person`` argument or return ``None`` for the absent case.

    Maps :class:`brain.errors.PersonNotFound` / :class:`PersonAmbiguous`
    to Typer's :class:`BadParameter` so the CLI surface stays consistent
    with the rest of the flag-validation path. Returns ``None`` when
    ``person`` is itself ``None`` so the caller threads ``person_keys=None``
    / ``person_display_name=None`` into ``hybrid_search`` unchanged.
    """
    if person is None:
        return None
    try:
        return resolve_person_to_keys(conn, person)
    except (PersonNotFound, PersonAmbiguous) as e:
        raise typer.BadParameter(str(e)) from e


@app.command()
def search(
    query: str = typer.Argument(...),
    limit: int = typer.Option(5, "--limit", "-n"),
    source: str | None = typer.Option(None, "--source"),
    tag: str | None = typer.Option(None, "--tag"),
    since_days: int | None = typer.Option(None, "--since", help="Days lookback"),
    json_output: bool = typer.Option(False, "--json"),
    fts_only: bool = typer.Option(False, "--fts-only"),
    # — Q1-C metadata filters — same set on `brain explain` below.
    person: str | None = typer.Option(
        None, "--person",
        help="Match docs where this person participated. "
             "Resolved through the directory (same as `brain people`).",
    ),
    after: datetime | None = typer.Option(
        None, "--after",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs sent/ingested on or after this ISO date "
             "(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).",
    ),
    before: datetime | None = typer.Option(
        None, "--before",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs sent/ingested strictly before this ISO date.",
    ),
    kind: str | None = typer.Option(
        None, "--kind",
        help="Filter by documents.content_type "
             "(transcript, email, email_thread, note, markdown, pdf, ...).",
    ),
    thread: str | None = typer.Option(
        None, "--thread", help="Filter by Gmail thread id.",
    ),
    draft: bool | None = typer.Option(
        None, "--draft/--no-draft",
        help="Include only drafts (--draft) or only published "
             "(--no-draft). Default: both.",
    ),
    has_tag: str | None = typer.Option(
        None, "--has-tag", help="Strict alias for --tag.",
    ),
    without_tag: str | None = typer.Option(
        None, "--without-tag",
        help="Exclude docs carrying this tag (combines with --tag).",
    ),
) -> None:
    """Hybrid search across the brain."""
    effective_tag = _reconcile_tag_flags(tag, has_tag)
    cfg = Config.load()
    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        person_match = _resolve_search_person(conn, person)
        results = hybrid_search(
            conn,
            embedder=embedder,
            query=query,
            limit=limit,
            source_kind=source,
            tag=effective_tag,
            since_days=since_days,
            fts_only=fts_only,
            vector_sim_floor=cfg.vector_sim_floor,
            recency_halflife_days=cfg.recency_halflife_days,
            snippet_context_tokens=cfg.snippet_context_tokens,
            person_keys=person_match.keys if person_match else None,
            person_display_name=(
                person_match.display_name if person_match else None
            ),
            after=after,
            before=before,
            content_type=kind,
            thread_id=thread,
            draft=draft,
            without_tag=without_tag,
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


@app.command()
def explain(
    query: str = typer.Argument(...),
    limit: int = typer.Option(10, "--limit", "-n"),
    source: str | None = typer.Option(None, "--source"),
    tag: str | None = typer.Option(None, "--tag"),
    since_days: int | None = typer.Option(None, "--since", help="Days lookback"),
    json_output: bool = typer.Option(False, "--json"),
    fts_only: bool = typer.Option(False, "--fts-only"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    # — Q1-C metadata filters — same set as `brain search` above.
    person: str | None = typer.Option(
        None, "--person",
        help="Match docs where this person participated.",
    ),
    after: datetime | None = typer.Option(
        None, "--after",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs sent/ingested on or after this ISO date.",
    ),
    before: datetime | None = typer.Option(
        None, "--before",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs sent/ingested strictly before this ISO date.",
    ),
    kind: str | None = typer.Option(
        None, "--kind",
        help="Filter by documents.content_type "
             "(transcript, email, email_thread, note, ...).",
    ),
    thread: str | None = typer.Option(
        None, "--thread", help="Filter by Gmail thread id.",
    ),
    draft: bool | None = typer.Option(
        None, "--draft/--no-draft",
        help="Include only drafts (--draft) or only published (--no-draft).",
    ),
    has_tag: str | None = typer.Option(
        None, "--has-tag", help="Strict alias for --tag.",
    ),
    without_tag: str | None = typer.Option(
        None, "--without-tag",
        help="Exclude docs carrying this tag.",
    ),
) -> None:
    """Show per-result ranking diagnostics for a query.

    Displays FTS rank, vector cosine, RRF contributions, recency boost, and
    the best-matching chunk for each result.  Use ``--verbose`` to also show
    which filter flags were active.  Use ``--json`` for the full machine-readable
    payload including all :class:`~brain.search.SearchExplanation` fields.
    """
    effective_tag = _reconcile_tag_flags(tag, has_tag)
    cfg = Config.load()
    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        person_match = _resolve_search_person(conn, person)
        results = hybrid_search(
            conn,
            embedder=embedder,
            query=query,
            limit=limit,
            source_kind=source,
            tag=effective_tag,
            since_days=since_days,
            fts_only=fts_only,
            vector_sim_floor=cfg.vector_sim_floor,
            recency_halflife_days=cfg.recency_halflife_days,
            snippet_context_tokens=cfg.snippet_context_tokens,
            explain=True,
            person_keys=person_match.keys if person_match else None,
            person_display_name=(
                person_match.display_name if person_match else None
            ),
            after=after,
            before=before,
            content_type=kind,
            thread_id=thread,
            draft=draft,
            without_tag=without_tag,
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
                    "explain": (
                        {
                            "fts_rank": r.explain.fts_rank,
                            "fts_score": r.explain.fts_score,
                            "fts_rrf_contribution": r.explain.fts_rrf_contribution,
                            "vector_rank": r.explain.vector_rank,
                            "vector_cosine": r.explain.vector_cosine,
                            "vector_rrf_contribution": r.explain.vector_rrf_contribution,
                            "rrf_score": r.explain.rrf_score,
                            "recency_age_days": r.explain.recency_age_days,
                            "recency_boost": r.explain.recency_boost,
                            "final_score": r.explain.final_score,
                            "best_chunk_id": r.explain.best_chunk_id,
                            "best_chunk_index": r.explain.best_chunk_index,
                            "matched_filters": r.explain.matched_filters,
                            "reranker_score": r.explain.reranker_score,
                        }
                        if r.explain is not None
                        else None
                    ),
                }
                for r in results
            ]
        )
        return
    if not results:
        typer.echo("(no results)")
        return
    console.print(explain_table(results, verbose=verbose))


@app.command("eval")
def eval_cmd(
    category: list[str] = typer.Option(
        [], "--category", "-c", help="Restrict to one or more categories (repeatable)."
    ),
    limit: int | None = typer.Option(None, "--limit", "-n", min=1, help="Max queries to run."),
    baseline: str | None = typer.Option(
        None, "--baseline", help="Baseline name for --diff comparison."
    ),
    diff: bool = typer.Option(False, "--diff", help="Show delta vs --baseline."),
    record_baseline: str | None = typer.Option(
        None, "--record-baseline", help="Write result as named baseline file."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
    corpus_path: Path | None = typer.Option(
        None, "--corpus", help="Override the default corpus YAML path."
    ),
) -> None:
    """Run the eval harness over the golden corpus and display ranking metrics.

    By default runs all queries in ``tests/eval/golden_corpus.yaml`` and prints
    a Rich table of nDCG@5 / MRR / Recall@20 per query plus aggregate means.
    That file is gitignored and must be authored locally (see
    ``tests/eval/.gitignore``); pass ``--corpus PATH`` to point at a different
    YAML, or expect ``EvalCorpusError`` when the default path is absent.

    Use ``--record-baseline NAME`` to persist the result for future comparison,
    and ``--baseline NAME --diff`` to compare the current run against a saved
    baseline.  Baseline files live in ``tests/eval/baselines/<NAME>.json``.
    """
    import dataclasses

    # Validate mutual-exclusion and dependency constraints.
    if diff and not baseline:
        raise typer.BadParameter("--diff requires --baseline")
    if diff and record_baseline is not None:
        raise typer.BadParameter("--diff and --record-baseline are mutually exclusive")

    # Validate baseline names (prevent path traversal).
    if baseline is not None:
        try:
            _assert_baseline_name(baseline)
        except EvalBaselineError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if record_baseline is not None:
        try:
            _assert_baseline_name(record_baseline)
        except EvalBaselineError as exc:
            raise typer.BadParameter(str(exc)) from exc

    # Load corpus.
    effective_corpus_path = corpus_path or _DEFAULT_CORPUS_PATH
    try:
        queries = load_corpus(effective_corpus_path)
    except EvalCorpusError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc

    # Apply category filter (empty list = no filter).
    if category:
        unknown_cats = set(category) - _VALID_CATEGORIES
        if unknown_cats:
            raise typer.BadParameter(
                f"unknown category/categories: {', '.join(sorted(unknown_cats))}. "
                f"Valid: {', '.join(sorted(_VALID_CATEGORIES))}"
            )
        queries = [q for q in queries if q.category in category]

    # Apply limit.
    if limit is not None:
        queries = queries[:limit]

    # Run eval.
    cfg = Config.load()
    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        report = run_eval(
            conn,
            embedder=embedder,
            queries=queries,
            recency_halflife_days=cfg.recency_halflife_days,
            snippet_context_tokens=cfg.snippet_context_tokens,
            vector_sim_floor=cfg.vector_sim_floor,
            embedder_name=cfg.embedder,
        )

    # Persist baseline if requested.
    if record_baseline is not None:
        baseline_file = _BASELINES_DIR / f"{record_baseline}.json"
        save_baseline(report, path=baseline_file)
        typer.echo(f"baseline saved: {baseline_file}", err=True)

    # Diff mode: compare against a saved baseline.
    if diff and baseline is not None:
        try:
            baseline_report = load_baseline(_BASELINES_DIR / f"{baseline}.json")
        except EvalBaselineError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(code=1) from exc
        diff_result = diff_reports(baseline_report, report)
        if json_output:
            emit_json(dataclasses.asdict(diff_result))
        else:
            console.print(eval_diff_table(diff_result))
        return

    # Default: display report.
    if json_output:
        emit_json(dataclasses.asdict(report))
    else:
        console.print(eval_report_table(report))


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


# Graph-target kinds rateable via ``brain rate --target-type`` (G4-b, spec
# §17d Q2). Mirrors ``brain.interactions._VALID_TARGET_TYPES`` / the SQL
# ``interactions_target_type_chk`` CHECK; kept as a CLI-local tuple so the
# Typer help + boundary validation don't reach into the writer's private set.
_RATE_TARGET_TYPES = ("entity", "community", "theme")


def _record_interaction_best_effort(
    conn: psycopg.Connection[Any], **kwargs: Any
) -> str | None:
    """Write one interaction row, swallowing logging failures (G4-b never-raise).

    Interaction logging is best-effort: a logging failure must NOT break the
    underlying command (mirrors the MCP discipline). Boundary validation
    (verdict / target-type / doc-id resolution) happens at the call site and
    surfaces normally; only the persistence step is guarded here. Returns the
    new row's id, or ``None`` if the write failed (logged at WARNING).
    """
    try:
        return record_interaction(conn, **kwargs)
    except (psycopg.Error, InteractionError) as exc:
        logger.warning("interaction logging failed: %s", type(exc).__name__)
        return None


@app.command()
def rate(
    id: str = typer.Argument(
        ...,
        help=(
            "What you're rating: a document id prefix (6+ hex chars), or — when "
            "--target-type is given — the graph target's id (entity UUID, "
            "community key, or theme key)."
        ),
    ),
    verdict: str = typer.Argument(
        ..., help="One of: useful, irrelevant.",
    ),
    target_type: str | None = typer.Option(
        None,
        "--target-type",
        help=(
            "Rate a graph target instead of a document: one of "
            "entity | community | theme. When set, the positional id is the "
            "graph target's id (not resolved as a document prefix)."
        ),
    ),
    graph_retrieved: bool = typer.Option(
        False,
        "--graph-retrieved",
        help=(
            "Provenance: mark this rating as produced by a graph surface "
            "(graphrag search/themes/entity). Independent of the target shape — "
            "a document rated via a graph path is still a document row."
        ),
    ),
) -> None:
    """Record a thumbs-up / thumbs-down on a document or graph target.

    Persists to the ``interactions`` table (action=``rated_useful`` or
    ``rated_irrelevant``, source='cli', session_id=NULL). Ratings APPEND
    every call — re-rating the same target creates a new row with a fresh
    timestamp; the full history is preserved. A future aggregation query
    will collapse to "most recent rating wins" but that is a read-side
    concern; not in Q1-C.

    Two target shapes (G4-b, spec §17d Q2; mutually exclusive — the XOR is
    enforced by ``record_interaction`` + the SQL CHECK):

    - Document (default): ``brain rate <doc-id> useful`` — the positional id
      resolves to a document.
    - Graph target: ``brain rate <target-id> useful --target-type entity`` —
      entity / community / theme become first-class rateable targets; the
      positional id is the durable graph-target id (no document resolution),
      and ``document_id`` is NULL.

    ``--graph-retrieved`` is an orthogonal provenance flag usable with either
    shape. Graph retrieval surfaces (``brain graphrag …``) never log at
    retrieval time — only this user action does.
    """
    if verdict not in {"useful", "irrelevant"}:
        raise typer.BadParameter("verdict must be 'useful' or 'irrelevant'")
    action: str = "rated_useful" if verdict == "useful" else "rated_irrelevant"
    if target_type is not None and target_type not in _RATE_TARGET_TYPES:
        raise typer.BadParameter(
            "--target-type must be one of: "
            + ", ".join(_RATE_TARGET_TYPES)
        )
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        if target_type is not None:
            # Graph-target rating: the positional id is the target id; it is
            # NOT a document prefix, so skip _resolve_id. document_id stays
            # NULL (the XOR demands exactly the target shape).
            new_id = _record_interaction_best_effort(
                conn,
                action=action,
                source="cli",
                target_type=target_type,
                target_id=id,
                graph_retrieved=graph_retrieved,
            )
            if new_id is None:
                typer.secho(
                    f"warning: failed to record {action} for "
                    f"{target_type} {id}",
                    fg="yellow",
                    err=True,
                )
                return
            typer.echo(
                f"recorded {action} ({new_id[:8]}) for {target_type} {id}"
            )
            return
        # Document rating (the unchanged Q1-C path + optional provenance).
        doc_id = _resolve_id(conn, id)
        new_id = _record_interaction_best_effort(
            conn,
            document_id=doc_id,
            action=action,
            source="cli",
            graph_retrieved=graph_retrieved,
        )
    if new_id is None:
        typer.secho(
            f"warning: failed to record {action} for doc {doc_id[:8]}",
            fg="yellow",
            err=True,
        )
        return
    typer.echo(f"recorded {action} ({new_id[:8]}) for doc {doc_id[:8]}")


@app.command()
def todo(
    source: str = typer.Option(
        "krisp", "--source",
        help="Filter to one source kind. Today only 'krisp' is supported.",
    ),
    since: int | None = typer.Option(
        None, "--since",
        help="Only items from docs ingested in the last N days.",
    ),
    closed: bool = typer.Option(
        False, "--closed",
        help="Include closed [x] items (default: open only).",
    ),
    limit: int = typer.Option(50, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List action items parsed from ``krisp_action_items`` documents.

    Walks every ``content_type='krisp_action_items'`` doc, parses each
    body for ``- [ ]`` / ``- [x]`` lines, and prints one row per parsed
    item. Default: open items only (use ``--closed`` to include done).
    JSON output is a flat list of ``{document_id, document_title,
    ingested_at, state, text}`` dicts.
    """
    from .todo import iter_action_item_docs

    cfg = Config.load()
    rows = []
    with connect(cfg.database_url) as conn:
        for row in iter_action_item_docs(
            conn,
            source_kind=source,
            since_days=since,
            include_closed=closed,
        ):
            rows.append(row)
            if len(rows) >= limit:
                break
    if json_output:
        emit_json(
            [
                {
                    "document_id": r.document_id,
                    "document_title": r.document_title,
                    "ingested_at": (
                        r.ingested_at.isoformat() if r.ingested_at else None
                    ),
                    "state": r.state,
                    "text": r.text,
                }
                for r in rows
            ]
        )
        return
    if not rows:
        typer.echo("(no action items)")
        return
    for r in rows:
        date_str = r.ingested_at.date().isoformat() if r.ingested_at else "(no date)"
        marker = "[done]" if r.state == "done" else "[open]"
        typer.echo(
            f"{marker:<7} {r.document_id[:8]}  {date_str}  {r.text}"
        )


@review_app.command("weekly")
def review_weekly(
    week: str | None = typer.Option(
        None,
        "--week",
        help="Target ISO week (YYYY-Www, e.g. 2026-W23). Default: current week.",
    ),
    no_graph: bool = typer.Option(
        False,
        "--no-graph",
        help="Skip the graph-community leg; fall back to tag-cluster grouping.",
    ),
    no_emit: bool = typer.Option(
        False,
        "--no-emit",
        help="Print to stdout only; do not write the vault review page.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Machine-readable JSON output (implies --no-emit)."
    ),
) -> None:
    """Synthesize the prior week's activity into a dated vault review page.

    Assembles themes (graph communities or tag clusters), activity, open loops,
    new captures, and key people for the target ISO week. Writes
    ``<vault>/reviews/<week>.md`` unless ``--no-emit`` / ``--json`` is given.
    """
    from .activity import current_iso_week
    from .review import (
        build_weekly_report,
        emit_weekly_page,
        render_weekly_json,
        render_weekly_rich,
    )

    cfg = Config.load()
    target_week = week or current_iso_week()
    # Best-effort theme synthesis only matters on the graph path; build the
    # enricher lazily there (Ollama is contacted only if a community lacks a
    # stored summary, and summarize_group never raises if it is down).
    enricher = _build_enricher(cfg) if not no_graph else None
    try:
        with connect(cfg.database_url) as conn:
            report = build_weekly_report(
                conn,
                cfg,
                week=target_week,
                generated_on=date_cls.today(),
                no_graph=no_graph,
                enricher=enricher,
            )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--week") from exc

    if json_output:
        emit_json(render_weekly_json(report))
        return

    is_empty = not (
        report.themes
        or report.activity
        or report.open_loops
        or report.ingested
    )
    if is_empty:
        typer.echo(f"No activity found for {target_week}.")
    else:
        typer.echo(render_weekly_rich(report))
    # Default behaviour emits the page regardless of how sparse the week was
    # (the renderer handles empty sections), matching the MCP tool's emit path.
    if not no_emit:
        path = emit_weekly_page(cfg.vault_path, report)
        typer.echo(f"Wrote {path}")


def _print_brief(data: Any) -> None:
    """Print the daily brief to stdout (titles + todo texts only)."""
    typer.echo(f"━━━ Brain Brief · {data.date.isoformat()} ━━━")
    typer.echo("\n📥  Recent captures")
    if data.captures:
        for doc in data.captures:
            kind = doc.source_kind or "manual"
            typer.echo(f"  • [{kind}] {doc.title}")
    else:
        typer.echo("  (no recent captures)")
    typer.echo("\n✅  Open action items")
    if data.open_todos:
        for row in data.open_todos:
            typer.echo(f"  • [ ] {row.text}")
    else:
        typer.echo("  (no open action items)")
    typer.echo("\n📌  Pinned / follow-up docs")
    if data.pinned:
        for pin in data.pinned:
            typer.echo(f"  • {pin.title}")
    else:
        typer.echo("  (no pinned docs)")
    if data.suggestions:
        typer.echo("\n💡  Suggested next steps")
        for i, suggestion in enumerate(data.suggestions, 1):
            typer.echo(f"  {i}. {suggestion}")


@app.command()
def brief(
    since: int | None = typer.Option(
        None, "--since", help="Capture window in hours (default: config)."
    ),
    todo_since: int | None = typer.Option(
        None, "--todo-since", help="Open-todo window in days (default: config)."
    ),
    date: str | None = typer.Option(
        None, "--date", help="ISO date for the header (YYYY-MM-DD, default: today)."
    ),
    no_enrich: bool = typer.Option(
        False, "--no-enrich", help="Skip LLM next-step suggestions."
    ),
    wiki: bool = typer.Option(
        False,
        "--wiki/--no-wiki",
        help="Write the digest to <vault>/daily/<YYYY>/<date>-brief.md.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Proactive daily digest: recent captures, open todos, pins, and next steps.

    Surfaces titles + todo texts only (never document bodies). LLM next-step
    suggestions are best-effort — skipped silently if Ollama is down or
    ``--no-enrich`` is given.
    """
    from dataclasses import replace

    from .brief import assemble_brief, suggest_next_steps, write_brief_to_vault

    cfg = Config.load()
    since_hours = since if since is not None else cfg.brief_since_hours
    todo_since_days = (
        todo_since if todo_since is not None else cfg.brief_todo_since_days
    )
    if date is not None:
        try:
            on_date = date_cls.fromisoformat(date)
        except ValueError as exc:
            raise typer.BadParameter(
                "date must be YYYY-MM-DD", param_hint="--date"
            ) from exc
    else:
        on_date = date_cls.today()

    with connect(cfg.database_url) as conn:
        data = assemble_brief(
            conn,
            cfg,
            since_hours=since_hours,
            todo_since_days=todo_since_days,
            on_date=on_date,
        )
    if not no_enrich:
        suggestions = suggest_next_steps(data, cfg)
        if suggestions:
            data = replace(data, suggestions=suggestions)

    if json_output:
        emit_json(data.to_dict())
        return

    _print_brief(data)
    if wiki:
        path = write_brief_to_vault(cfg.vault_path, on_date, data)
        typer.echo(f"\nWrote {path}")


@app.command()
def show(
    id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json"),
    query: str | None = typer.Option(
        None,
        "--query",
        "--originating-query",
        help=(
            "The query that led you to this document. When set, records an "
            "'opened' interaction (source='cli'). Mirrors MCP brain_show."
        ),
    ),
    session_id: str | None = typer.Option(
        None,
        "--session-id",
        help=(
            "UUID grouping a search-then-open pair (from a prior search/graphrag "
            "call). Requires --query."
        ),
    ),
    graph_retrieved: bool = typer.Option(
        False,
        "--graph-retrieved",
        help=(
            "Provenance: this open came from a graph surface (graphrag "
            "search/themes/entity). Recorded on the 'opened' row when --query "
            "is given. Default off preserves today's no-log behavior."
        ),
    ),
) -> None:
    """Print a document by id (or 6+ char prefix).

    G4-b (spec §17d Q2): pass ``--query`` to record an ``opened`` interaction
    (source='cli') the same way MCP ``brain_show`` does, and ``--graph-retrieved``
    to mark that open as produced by a graph surface (provenance). With no
    ``--query`` nothing is logged — today's behavior is unchanged. Logging is
    best-effort: a logging failure never blocks the document from printing.
    """
    # Mirror MCP brain_show's D15 guard: a session id without an originating
    # query carries no useful signal.
    if session_id is not None and query is None:
        raise typer.BadParameter("--session-id requires --query")
    session_uuid: uuid.UUID | None = None
    if session_id is not None:
        try:
            session_uuid = uuid.UUID(session_id)
        except ValueError as e:
            raise typer.BadParameter(
                f"--session-id is not a valid UUID: {session_id!r}"
            ) from e
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id)
        doc = fetch_document(conn, doc_id)
        # Log the open AFTER the fetch succeeded, gated on --query (parity with
        # MCP brain_show). graph_retrieved is provenance on the document row.
        # Best-effort: a logging failure must not break `brain show` (G4-b).
        if query is not None:
            _record_interaction_best_effort(
                conn,
                document_id=doc_id,
                action="opened",
                source="cli",
                query=query,
                session_id=session_uuid,
                graph_retrieved=graph_retrieved,
            )
    assert doc is not None  # _resolve_id confirmed the doc exists
    if json_output:
        # Wave Q1-D — emit ``summary`` only when populated (additive,
        # back-compatible; scripts parsing the JSON shape still work when
        # the key is absent).
        payload: dict[str, Any] = {
            "id": doc.id,
            "title": doc.title,
            "content": doc.content,
            "content_type": doc.content_type,
            "tags": doc.tags,
            "source_path": doc.source_path,
            "ingested_at": doc.ingested_at,
            "source_kind": doc.source_kind,
        }
        if doc.summary is not None:
            payload["summary"] = doc.summary
        emit_json(payload)
        return
    typer.echo(f"# {doc.title}")
    typer.echo(f"id:           {doc.id}")
    typer.echo(f"source:       {doc.source_kind or 'manual'} ({doc.content_type})")
    typer.echo(f"tags:         {', '.join(doc.tags) or '(none)'}")
    typer.echo(f"ingested:     {doc.ingested_at}")
    if doc.summary is not None:
        # Wave Q1-D — between ``ingested:`` and the body so existing scripts
        # parsing the labeled-prefix lines stay unaffected (R7).
        typer.echo(f"summary:      {doc.summary}")
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
    mods: list[str] | None = typer.Argument(None),
    regenerate_file: bool = typer.Option(
        False,
        "--regenerate-file",
        help=(
            "If the doc's vault mirror is missing, recreate it from the DB "
            "before applying tags. Errors out for vault-tier authored notes."
        ),
    ),
    auto: bool = typer.Option(
        False, "--auto",
        help=(
            "Auto-propose tags via the local-Ollama enricher (Q1-D) and "
            "prompt accept/reject. Mutually exclusive with +tag/-tag mods. "
            "Requires the doc to have a non-NULL `summary` (run "
            "`brain enrich --backfill` first)."
        ),
    ),
    accept_all: bool = typer.Option(
        False, "--accept-all",
        help="Non-interactive: accept every proposed tag. Requires --auto.",
    ),
) -> None:
    """Add (+name) or remove (-name) tags, or auto-propose via the LLM.

    Modes:
        brain tag <id> +foo -bar            # explicit add/remove (legacy)
        brain tag <id> --auto               # interactive LLM proposal
        brain tag <id> --auto --accept-all  # non-interactive accept-everything

    When the document has a ``vault_path``, every accepted change is also
    written to the file's frontmatter so the next ``brain vault sync`` does
    not re-read stale ``tags: []`` from disk and overwrite the DB. Pass
    ``--regenerate-file`` to recreate a missing ``_ingested/`` mirror from
    the DB row (vault-tier authored notes are refused).
    """
    mods_list = mods or []
    if accept_all and not auto:
        raise typer.BadParameter("--accept-all requires --auto")
    if auto and mods_list:
        raise typer.BadParameter(
            "--auto cannot combine with +tag/-tag arguments"
        )
    if not auto and not mods_list:
        raise typer.BadParameter(
            "expected --auto or +tag/-tag arguments"
        )
    if auto:
        _run_auto_tag(
            id, accept_all=accept_all, regenerate_file=regenerate_file
        )
        return
    add = [m[1:] for m in mods_list if m.startswith("+") and len(m) > 1]
    remove = [m[1:] for m in mods_list if m.startswith("-") and len(m) > 1]
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


def _run_auto_tag(
    id_prefix: str, *, accept_all: bool, regenerate_file: bool
) -> None:
    """Interactive auto-tag proposal flow (Wave Q1-D 3.2).

    Resolves the id; fetches title + summary + current_tags; surfaces an
    error if ``summary IS NULL`` (the LLM is unreliable on raw bodies and
    cheaper on summaries — D5 / R2 mitigation); calls the enricher;
    presents the proposal; applies accepted tags via :func:`apply_tags`
    (which routes through :func:`brain.tags.normalize_tags` and the
    vault writeback helper).
    """
    cfg = Config.load()
    enricher = _build_enricher(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id_prefix)
        row = conn.execute(
            "SELECT title, summary, tags, vault_path, kind "
            "FROM documents WHERE id=%s",
            (doc_id,),
        ).fetchone()
        assert row is not None  # _resolve_id confirmed
        title, summary, current_tags, vault_path_rel, kind = row
        current_tags = list(current_tags or [])
        if summary is None:
            typer.secho(
                "auto-tag requires a non-NULL summary on the document.\n"
                "Run `brain enrich --backfill` first.",
                fg="red",
                err=True,
            )
            raise typer.Exit(code=1)
        vocab = list_existing_tags(conn)
        try:
            proposal = enricher.propose_tags(
                title=title,
                summary=summary,
                existing_vocab=vocab,
                current_tags=current_tags,
                max_new=1,
            )
        except OllamaUnavailable as exc:
            typer.secho(f"Ollama unavailable: {exc}", fg="red", err=True)
            raise typer.Exit(code=1) from exc
        except EnrichmentError as exc:
            typer.secho(f"enrichment failed: {exc}", fg="red", err=True)
            raise typer.Exit(code=1) from exc

        all_proposed = proposal.existing + proposal.new
        if not all_proposed:
            typer.echo("(no tags proposed)")
            return

        typer.echo("Proposed tags:")
        for t in proposal.existing:
            typer.echo(f"  [existing]  + {t}")
        for t in proposal.new:
            typer.echo(f"  [new]       + {t}")
        if not accept_all:
            choice = typer.prompt(
                "Apply tags? [a]ll / [s]ome / [r]eject",
                default="r",
                show_default=True,
            ).strip().lower()
        else:
            choice = "a"
        if choice == "r" or choice == "q":
            typer.echo("rejected; no changes")
            return
        accepted: list[str]
        if choice == "a":
            accepted = list(all_proposed)
        elif choice == "s":
            accepted = []
            for t in all_proposed:
                yn = typer.prompt(
                    f"  accept {t}? [y/n]", default="n"
                ).strip().lower()
                if yn == "y":
                    accepted.append(t)
        else:
            typer.secho(
                f"unknown choice {choice!r}; treating as reject",
                fg="yellow",
                err=True,
            )
            return
        if not accepted:
            typer.echo("nothing accepted; no changes")
            return
        new_tag_list = apply_tags(conn, doc_id, add=accepted)
        suffix = _tag_file_writeback(
            conn,
            cfg=cfg,
            vault_path_rel=vault_path_rel,
            kind=kind,
            new_tags=new_tag_list,
            doc_id=doc_id,
            regenerate_file=regenerate_file,
        )
        typer.echo(
            f"updated tags on {doc_id[:8]}{suffix}: +{' +'.join(accepted)}"
        )


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
    # Wave Q2-SUMMARY-WIKI smoke gap (Codex finding 1 follow-up,
    # 2026-05-11): wire the enricher whenever the body is changing so
    # ``_enrich_post_ingest_hook`` can refresh ``documents.summary``
    # against the new body. Without this the Q2 wiki lede would render
    # the pre-edit summary above a freshly-edited body. Build lazily —
    # only when the body actually changed (the enricher constructor
    # probes Ollama, which we don't want on a title-only / metadata-only
    # edit). Ollama failures inside the hook degrade soft (logged WARN;
    # ``brain enrich --backfill`` recovers the row later).
    enricher: Any = _build_enricher(cfg) if body_changed else None
    graph_syncer = _build_graph_syncer(cfg)
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
                enricher=enricher,
                graph_syncer=graph_syncer,
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
            owner_participants=cfg.owner_participants,
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
    # Wave Q2-SUMMARY-WIKI smoke gap (Codex finding 1 follow-up,
    # 2026-05-11): wire the enricher on body change so the auto-summary
    # refreshes alongside the new body. Lazy build — title-only /
    # content-type-only / metadata-only edits don't need an Ollama
    # probe, and the body-only flag set (``--content-file`` /
    # ``--content-stdin``) is the only path that re-triggers
    # ``_enrich_post_ingest_hook``'s body-changed branch.
    enricher: Any = _build_enricher(cfg) if new_content is not None else None
    graph_syncer = _build_graph_syncer(cfg)
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
                enricher=enricher,
                graph_syncer=graph_syncer,
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
    graph_syncer = _build_graph_syncer(cfg)
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
        # Wave G1-c — drop the doc from the people graph. Runs post-DELETE on
        # the same (autocommit) connection; best-effort / never-raises. The
        # documents-row delete cascades to the relational graph source rows
        # (migration 012 FKs), and remove_document is robust whether those are
        # already gone or not — it then GCs orphaned person vertices + edges.
        graph_syncer.remove(conn, doc_id)
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
    graph_syncer = _build_graph_syncer(cfg)
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
                graph_syncer=graph_syncer,
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


@vault_app.command("sync-summaries")
def vault_sync_summaries(
    vault: Path | None = typer.Option(
        None,
        "--vault",
        help="Override the configured vault path for this invocation.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Inspect every doc + report would-update counts without "
            "touching any files. Reads the DB; writes nothing."
        ),
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help=(
            "Cap the total number of documents inspected. Pairs with "
            "``--dry-run`` for incremental drains on a large corpus."
        ),
    ),
) -> None:
    """Backfill ``summary:`` frontmatter into existing vault mirror files.

    Wave Q2-SUMMARY-WIKI's one-shot reconciliation pass. The Q1-D
    enricher writes ``documents.summary``, and post-Q2 ingests carry
    that summary into the mirror frontmatter on first write — but
    documents enriched BEFORE the writer learned the ``summary`` field
    have stale frontmatter on disk. This command reads each row where
    ``documents.summary IS NOT NULL`` and ``vault_path IS NOT NULL``,
    parses the mirror file's frontmatter, and rewrites it atomically
    when the on-disk ``summary:`` is missing or stale.

    Idempotent: rerunning on a fully-synced vault reports every row as
    ``unchanged`` and writes nothing. Non-destructive: only the
    ``summary:`` key is touched; the file body, tags, aliases, and
    user-authored freeform keys round-trip verbatim.

    Counters printed at the end:

    - ``inspected``  total rows pulled from the DB
    - ``updated``    rewrote the mirror (or, under ``--dry-run``, would)
    - ``unchanged``  on-disk summary already matched
    - ``missing``    DB row's ``vault_path`` resolved to no file on disk
    - ``errored``    parse / write failure (see warnings on stderr)
    """
    cfg = Config.load()
    target = vault.expanduser() if vault is not None else cfg.vault_path
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        report = sync_summaries(
            conn, vault_root=target, dry_run=dry_run, limit=limit
        )

    label = "would update" if dry_run else "updated"
    typer.echo(
        f"inspected {report.inspected}, "
        f"{label} {report.updated}, "
        f"unchanged {report.unchanged}, "
        f"missing {report.missing_file}, "
        f"errored {report.errored}"
    )
    for err in report.errors:
        typer.secho(f"  error: {err}", fg="red", err=True)
    if report.errored:
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
    graph_syncer = _build_graph_syncer(cfg)

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
                owner_participants=cfg.owner_participants,
            ),
            graph_syncer=graph_syncer,
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
            f"fences_written {report.fences_written}, "
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
            owner_participants=cfg.owner_participants,
            graph_syncer=graph_syncer,
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
        f"fences_written {report.fences_written}, "
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
            "Copy the brain package's quartz_overrides/ tree over the Quartz workspace "
            "before building. Use `--no-overlay` to skip and use whatever is "
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

    Before the build, the overlay step copies the brain package's
    ``quartz_overrides/`` tree over the Quartz workspace (custom Graph
    component, contentIndex emitter, etc.). Use `--no-overlay` to skip,
    or `--print-overlay` to see what would be copied without applying.

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
        plan = plan_overlay(workspace)
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
    """Full-corpus directory rebuild + derived-links rebuild + People Hub.

    Five steps, all against a single Postgres connection:

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
    4. **Fence rewrite** across every ``_ingested/`` body whose derived
       edges drifted (so Quartz's ``/graph`` view picks up the fresh edges).
    5. **People Hub emission** — :func:`emit_people_pages` walks the
       freshly-rebuilt directory + the curated ``_people.yml`` set and
       writes one ``<vault>/people/<slug>.md`` per emittable person plus
       an ``index.md`` roster. Pages whose rendered bytes match disk are
       skipped; pages for people no longer in scope are deleted. The
       step runs unconditionally so the index page still renders on a
       fresh corpus with no Gmail/Krisp data yet.

    Idempotent: running twice produces the same final state because
    ``rebuild_derived_for``'s DELETE+INSERT scopes to the touched docs (and
    we touch every linkable doc on this command), and the Krisp backfill
    overwrites ``_participant_keys`` deterministically from the body.
    """
    cfg = Config.load()
    graph_syncer = _build_graph_syncer(cfg)
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

        people_yml_seen = refresh_people_yml(conn, cfg.vault_path)
        typer.echo(f"  - _people.yml: {people_yml_seen} entries")

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
                conn,
                corpus_ids,
                directory=directory,
                owner_participants=cfg.owner_participants,
            )
            typer.echo(f"  - Touched docs: {len(corpus_ids)}")
            typer.echo(f"  - Inserted edges: {inserted}")
            typer.echo(f"  - Affected docs: {len(affected_ids)}")

            # Step 4 (Phase D): regenerate the fenced "Related" section in
            # every affected ``_ingested/`` file so Quartz's ``/graph`` view
            # picks up the edges we just rebuilt. The renderer skips
            # vault-tier rows and missing mirror files silently; the count
            # is the number of files actually written. Per the 2026-05-08
            # idempotency fix, the renderer also skips files whose
            # freshly-rendered text is byte-identical to what's already on
            # disk — so ``Fence files rewritten`` reflects real disk
            # effect, and a relink → sync round-trip is a true no-op for
            # unchanged docs.
            fences_written = rewrite_derived_fences(
                conn, affected_ids, vault_path=cfg.vault_path
            )
            typer.echo(f"  - Fence files rewritten: {fences_written}")

        # Step 5 (Phase C, 2026-05-07 People Hub plan): emit per-person hub
        # pages under ``<vault>/people/`` from the freshly-rebuilt directory.
        # Idempotent — pages whose rendered bytes match disk are no-ops, and
        # pages for people no longer in scope (curation removed, dropped
        # below ``min_docs``) are deleted. Runs unconditionally (not gated
        # on ``corpus_ids``) so the index page + curated-only pages still
        # render on a corpus that has yet to acquire any Krisp/Gmail data.
        typer.echo("Emitting people hub pages...")
        people_report = emit_people_pages(
            conn,
            vault_path=cfg.vault_path,
            owner_keys=cfg.owner_participants,
            min_docs=cfg.people_hub_min_docs,
            sender_denylist=cfg.graph_sender_denylist,
        )
        typer.echo(f"  - Pages written: {people_report.pages_written}")
        typer.echo(f"  - Pages deleted: {people_report.pages_deleted}")

        # Step 5.5 (wave G1-c): reconcile the people graph for every linkable
        # doc. Step 1 (Gmail directory rescan), step 1.5 (Krisp
        # ``_participant_keys`` backfill), and step 2 (Calendar/Contacts
        # refresh) all feed the person resolver, so a doc's graph people roster
        # can go stale after a relink even when its own row was untouched.
        # Reconciling the full linkable set (the same ids the linker iterates)
        # is correct + cheap: the per-aspect ``graph_index_state`` watermark
        # skips any doc whose resolved persons + config didn't actually change.
        # Best-effort / never-raises; a no-op when graph sync is disabled or
        # AGE is absent. Runs post-commit on the autocommit ``conn``.
        if corpus_ids:
            typer.echo("Reconciling graph...")
            for doc_id in sorted(corpus_ids):
                graph_syncer.reconcile(conn, doc_id)
            typer.echo(f"  - Graph docs reconciled: {len(corpus_ids)}")

        # Step 6: Rich summary — directory by source + derived_links by rule.
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

        people_yml_seen = refresh_people_yml(conn, cfg.vault_path)
        typer.echo(f"  - _people.yml: {people_yml_seen} entries")

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
            owner_participants=cfg.owner_participants,
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
    # Validate the template up front so a bad ``--template`` / missing
    # ``_templates/`` surfaces a friendly CLI error (``create_vault_note``
    # re-resolves it internally for the actual render).
    _ensure_template(vault_path, template)

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

    # Build the embedder via the CLI factory (the patch point tests wire onto
    # ``brain.cli._build_embedder``) and hand it to the shared helper so the
    # session loop and ``brain note new`` share one create+sync path.
    embedder = _build_embedder(cfg)
    try:
        with connect(cfg.database_url) as conn:
            conn.autocommit = True
            document_id = create_vault_note(
                conn,
                cfg=cfg,
                vault_path=vault_path,
                title=title,
                tags=list(tag),
                template=template,
                folder=folder,
                embedder=embedder,
            )
    except VaultNoteSyncError as exc:
        for path, reason in exc.errors:
            typer.secho(f"sync error: {path}: {reason}", fg="red", err=True)
        raise typer.Exit(code=1) from exc

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
            owner_participants=cfg.owner_participants,
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
            owner_participants=cfg.owner_participants,
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


# ---------------------------------------------------------------------------
# brain people — terminal view onto the People Hub aggregation.
#
# Phase C of the 2026-05-07 People Hub plan. The hub itself is rendered
# into ``<vault>/people/`` by ``vault relink-derived`` (Step 5); this
# command surfaces the same data without leaving the terminal.
#
# Two forms:
#   * ``brain people``         — alphabetised roster (display_name,
#                                 doc count, primary email, curated badge)
#   * ``brain people <name>``  — single record with full doc list
#
# ``--json`` swaps the Rich table for machine-readable JSON.
# ---------------------------------------------------------------------------


def _person_to_payload(record: PersonRecord) -> dict[str, Any]:
    """Render one :class:`PersonRecord` as a JSON-friendly dict.

    Used by the ``--json`` branch of the ``brain people`` commands and
    extracted so the list and detail views serialize identically (no
    drift between the roster and the per-person view).

    ``DocRef.date`` is dropped through ``isoformat`` for stable output.
    Missing dates serialize as ``null`` (rather than absent) so the
    consumer always sees the same key set per doc.
    """
    return {
        "slug": record.slug,
        "display_name": humanize_display_name(record.display_name),
        "primary_email": record.primary_email,
        "all_emails": list(record.all_emails),
        "doc_count": len(record.docs),
        "in_people_yml": record.in_people_yml,
        "docs": [
            {
                "id": doc.document_id,
                "title": doc.title,
                "source_kind": doc.source_kind,
                "date": doc.date.isoformat() if doc.date is not None else None,
                "vault_target": doc.vault_target,
            }
            for doc in record.docs
        ],
    }


def _people_matches(
    records: list[PersonRecord], name: str
) -> list[PersonRecord]:
    """Every record whose ``display_name`` contains ``name`` (case-insensitive).

    Returned in the alphabetical order ``aggregate_people`` already
    produced. Used by the detail view to surface "did you mean …?"
    when the lookup is ambiguous.
    """
    needle = name.casefold().strip()
    if not needle:
        return []
    return [r for r in records if needle in r.display_name.casefold()]


def _render_people_roster_table(records: list[PersonRecord]) -> Table:
    """Build the Rich table shown by ``brain people`` (no name argument).

    Columns: ``Curated`` (✅ when ``in_people_yml`` is True, blank
    otherwise), ``Display name``, ``Docs``, ``Primary email``. Sorted
    alphabetically by display name (``aggregate_people`` already
    produces that ordering).
    """
    table = Table(title="People Hub")
    table.add_column("Curated", style="green", justify="center")
    table.add_column("Display name", style="cyan")
    table.add_column("Docs", justify="right")
    table.add_column("Primary email")
    for rec in records:
        table.add_row(
            "✅" if rec.in_people_yml else "",
            humanize_display_name(rec.display_name),
            str(len(rec.docs)),
            rec.primary_email,
        )
    return table


def _render_person_detail_table(record: PersonRecord) -> Table:
    """Build the per-doc Rich table shown by ``brain people <name>``.

    Columns: ``Date`` (``YYYY-MM-DD`` or ``undated``), ``Source``,
    ``Title`` (truncated with ellipsis if long), and ``Doc id``
    (8-char prefix — enough for ``brain show <prefix>`` resolution).
    Rows in the order :func:`_sort_docs` produced inside
    ``aggregate_people`` (date desc, then title asc).
    """
    table = Table(
        title=f"{humanize_display_name(record.display_name)} — "
        f"{len(record.docs)} doc(s)"
    )
    table.add_column("Date", style="dim")
    table.add_column("Source", style="cyan")
    table.add_column("Title")
    table.add_column("Doc id", style="dim")
    for doc in record.docs:
        date_str = (
            doc.date.strftime("%Y-%m-%d") if doc.date is not None else "undated"
        )
        table.add_row(
            date_str,
            doc.source_kind,
            doc.title,
            doc.document_id[:8],
        )
    return table


@app.command("people")
def people_cmd(
    name: str | None = typer.Argument(
        None,
        help=(
            "Optional case-insensitive substring of a display name. "
            "Without this argument, the full alphabetised roster is "
            "shown. With it, that person's full doc list is shown."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the aggregation as JSON instead of a Rich table.",
    ),
) -> None:
    """Browse the People Hub aggregation.

    Reuses :func:`brain.wiki.build_people.aggregate_people` so the
    terminal view, the rendered ``<vault>/people/`` pages, and the
    derived-link participant filter all derive from the same canonical
    set. Read-only — no DB writes.

    The threshold (``BRAIN_PEOPLE_HUB_MIN_DOCS``, default 3) and owner
    filter (``BRAIN_OWNER_PARTICIPANTS``) flow through the existing
    :class:`Config`, so flipping either env var changes the visible
    roster on the next invocation without code changes.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        records = aggregate_people(
            conn,
            owner_keys=cfg.owner_participants,
            min_docs=cfg.people_hub_min_docs,
            sender_denylist=cfg.graph_sender_denylist,
        )

    if name is None:
        # Roster view.
        if json_output:
            emit_json([_person_to_payload(r) for r in records])
            return
        if not records:
            typer.echo(
                "(no people in scope — add entries to <vault>/_people.yml or "
                "lower BRAIN_PEOPLE_HUB_MIN_DOCS)"
            )
            return
        console.print(_render_people_roster_table(records))
        return

    # Detail view: case-insensitive substring match on display_name.
    matches = _people_matches(records, name)
    if not matches:
        typer.secho(
            f"no person matched {name!r}.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)
    record = matches[0]
    if len(matches) > 1:
        # Surface the ambiguity; pick the first alphabetically (matches
        # are already sorted) but tell the user the others exist so
        # they can refine. Not an error — the substring rule deliberately
        # tolerates partial input.
        others = ", ".join(
            humanize_display_name(r.display_name) for r in matches[1:]
        )
        typer.secho(
            f"note: {len(matches)} matches; showing "
            f"{humanize_display_name(record.display_name)!r}. "
            f"Other matches: {others}",
            fg="yellow",
            err=True,
        )
    if json_output:
        emit_json(_person_to_payload(record))
        return
    console.print(_render_person_detail_table(record))


# ---------------------------------------------------------------------------
# brain owner — manage BRAIN_OWNER_PARTICIPANTS in `.env` without hand-editing.
# ---------------------------------------------------------------------------

# Hint printed after every mutation. Shipped through stdout (not stderr) so
# CliRunner captures it for the regression test and so users running in a
# pipe still see it.
_OWNER_RELINK_HINT = (
    "Updated BRAIN_OWNER_PARTICIPANTS. Run `brain vault relink-derived` to "
    "rebuild derived links, then `brain vault sync` to refresh body fences."
)


def _owner_dotenv_path() -> Path:
    """Path to the `.env` file the owner subcommands read + mutate.

    Indirects through ``config._project_dotenv`` so tests can patch one
    helper and have it cover both ``Config.load()`` (used by ``show``) and
    the writer used by ``set/add/remove``. Keeps the source of truth in one
    place.
    """
    return _config_module._project_dotenv()


def _owner_normalize_csv(csv: str) -> list[str]:
    """Mirror ``Config.load()`` parsing: trim → lowercase → drop empty → dedupe.

    Returns the canonical list ordering (insertion order, deduped) — the
    on-disk representation. Owner identifiers are stored lowercased to match
    the comparison Phase 1 already performs in
    ``pass_runner._build_doc_snapshot`` (``key.lower() in owner_participants``).

    Rejects entries containing newline / carriage-return characters by
    raising ``typer.Exit(1)`` after a friendly stderr message — a literal
    newline embedded in an identifier would silently corrupt the next
    line of ``.env`` once written. Defensive: real-world emails and
    display names never contain raw line terminators.
    """
    seen: set[str] = set()
    out: list[str] = []
    for piece in csv.split(","):
        norm = piece.strip().lower()
        if not norm:
            continue
        if "\n" in norm or "\r" in norm:
            typer.secho(
                "error: BRAIN_OWNER_PARTICIPANTS entries must not contain "
                "newline characters",
                fg="red",
                err=True,
            )
            raise typer.Exit(code=1)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _owner_read_existing(path: Path) -> list[str]:
    """Parse the current ``BRAIN_OWNER_PARTICIPANTS`` value from ``path``.

    Returns the lowercased, deduped entry list — same shape as
    :func:`_owner_normalize_csv`. Returns ``[]`` for missing file, missing
    line, or blank value. Strips surrounding double-quotes (the writer's
    quoting rule round-trips exactly).
    """
    if not path.is_file():
        return []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.lstrip()
        if not line.startswith("BRAIN_OWNER_PARTICIPANTS="):
            continue
        value = line[len("BRAIN_OWNER_PARTICIPANTS=") :].strip()
        if (
            len(value) >= 2
            and value.startswith('"')
            and value.endswith('"')
        ):
            value = value[1:-1].replace('\\"', '"')
        return _owner_normalize_csv(value)
    return []


def _owner_format_value(entries: list[str]) -> str:
    """Render the RHS of the ``BRAIN_OWNER_PARTICIPANTS=…`` line.

    Quotes the value when it contains spaces or commas (i.e. always for
    2+ entries, or for any single entry containing whitespace). Pure
    ``alphanumeric+@+.`` single entries stay unquoted to match the existing
    style of ``DATABASE_URL=…`` lines.
    """
    raw = ",".join(entries)
    if any(ch in raw for ch in (" ", ",", "\t")):
        escaped = raw.replace('"', '\\"')
        return f'"{escaped}"'
    return raw


def _owner_write_dotenv(path: Path, entries: list[str]) -> None:
    """Atomically replace (or insert) the ``BRAIN_OWNER_PARTICIPANTS`` line.

    Read existing contents, replace the target line in place if found
    (preserves every other line, blank lines, and trailing comments),
    otherwise append at the end with a leading newline if the file
    doesn't already terminate in one. Creates the file if missing. Writes
    via a sibling temp file + ``os.replace`` so a partial write can never
    leave ``.env`` truncated.
    """
    new_value = _owner_format_value(entries)
    new_line = f"BRAIN_OWNER_PARTICIPANTS={new_value}"

    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        lines = existing.splitlines(keepends=True)
        replaced = False
        for idx, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("BRAIN_OWNER_PARTICIPANTS="):
                trailing_nl = "\n" if line.endswith("\n") else ""
                lines[idx] = new_line + trailing_nl
                replaced = True
                break
        if not replaced:
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(new_line + "\n")
        new_text = "".join(lines)
    else:
        new_text = new_line + "\n"

    tmp = path.parent / (path.name + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        # Clean up the half-written sidecar so a retry can't trip over a
        # stale ``.env.tmp``. ``missing_ok=True`` covers the case where the
        # original ``write_text`` failed before the file was created.
        tmp.unlink(missing_ok=True)
        typer.secho(
            f"error: failed to write {path}: {exc}", fg="red", err=True
        )
        raise typer.Exit(code=1) from exc


@owner_app.command("show")
def owner_show() -> None:
    """Print the active ``BRAIN_OWNER_PARTICIPANTS`` list, one entry per line.

    Reads from ``Config.owner_participants`` so shell-env overrides of
    ``BRAIN_OWNER_PARTICIPANTS`` (which beat ``.env``) are reflected. An
    empty list prints a single ``(none — BRAIN_OWNER_PARTICIPANTS unset)``
    placeholder so the output channel is never silent.
    """
    cfg = Config.load()
    if not cfg.owner_participants:
        typer.echo("(none — BRAIN_OWNER_PARTICIPANTS unset)")
        return
    for entry in sorted(cfg.owner_participants):
        typer.echo(entry)


@owner_app.command("set")
def owner_set(
    csv: str = typer.Argument(
        ...,
        help='Comma-separated identifiers, e.g. "Pat Owner,fixture@example.com"',
    ),
) -> None:
    """Replace the entire ``BRAIN_OWNER_PARTICIPANTS`` list in ``.env``.

    Trims, lowercases, dedupes — same normalisation ``Config.load()``
    performs. Quotes the on-disk value when it contains spaces or commas.
    Atomic write; never partial.

    Idempotent: if the new normalised list equals the current on-disk
    list (same entries, same order — ``_owner_normalize_csv`` produces
    deterministic ordering for any input that maps to the same set),
    skip the rewrite + relink hint and emit a "no change" message. This
    matches ``add`` / ``remove`` behaviour and avoids advising the user
    to run an expensive ``relink-derived`` for a no-op.
    """
    entries = _owner_normalize_csv(csv)
    path = _owner_dotenv_path()
    current = _owner_read_existing(path)
    if entries == current:
        typer.echo(
            "BRAIN_OWNER_PARTICIPANTS already matches — no change."
        )
        return
    _owner_write_dotenv(path, entries)
    typer.echo(_OWNER_RELINK_HINT)


@owner_app.command("add")
def owner_add(
    identifier: str = typer.Argument(
        ...,
        help="Identifier to append (email or display name).",
    ),
) -> None:
    """Append one identifier to ``BRAIN_OWNER_PARTICIPANTS``. Idempotent.

    Lookup is case-insensitive (entries are stored lowercased); a duplicate
    add is a silent no-op that does not rewrite ``.env`` and does not print
    the relink hint — there's nothing to relink.
    """
    norm = identifier.strip().lower()
    if not norm:
        typer.secho("error: identifier must not be empty", fg="red", err=True)
        raise typer.Exit(code=1)
    path = _owner_dotenv_path()
    current = _owner_read_existing(path)
    if norm in current:
        typer.echo(f"{norm!r} already present in BRAIN_OWNER_PARTICIPANTS — no change.")
        return
    current.append(norm)
    _owner_write_dotenv(path, current)
    typer.echo(_OWNER_RELINK_HINT)


@owner_app.command("remove")
def owner_remove(
    identifier: str = typer.Argument(
        ...,
        help="Identifier to drop (email or display name).",
    ),
) -> None:
    """Drop one identifier from ``BRAIN_OWNER_PARTICIPANTS``. Idempotent.

    Lookup is case-insensitive. Removing an absent entry is a silent no-op
    that does not rewrite ``.env`` and does not print the relink hint.
    """
    norm = identifier.strip().lower()
    if not norm:
        typer.secho("error: identifier must not be empty", fg="red", err=True)
        raise typer.Exit(code=1)
    path = _owner_dotenv_path()
    current = _owner_read_existing(path)
    if norm not in current:
        typer.echo(f"{norm!r} not present in BRAIN_OWNER_PARTICIPANTS — no change.")
        return
    current = [e for e in current if e != norm]
    _owner_write_dotenv(path, current)
    typer.echo(_OWNER_RELINK_HINT)


# ---------------------------------------------------------------------------
# brain wiki — wiki workspace management
# ---------------------------------------------------------------------------


@wiki_app.command("install")
def wiki_install_cmd(
    vault: Path | None = typer.Option(
        None,
        "--vault",
        help="Vault path; defaults to $BRAIN_VAULT_PATH or ~/brain-vault.",
    ),
    port: int = typer.Option(
        8080,
        "--port",
        help="Caddy listening port written into the Caddyfile template.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-clone even if workspace exists (destructive — rmtrees .quartz/).",
    ),
    no_npm: bool = typer.Option(
        False,
        "--no-npm",
        help="Skip npm install (useful in tests and offline environments).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print planned actions; touch nothing.",
    ),
) -> None:
    """Install Quartz workspace, apply brain overlay, render Caddyfile.

    On a fresh vault this clones jackyzha0/quartz at the pinned commit,
    applies the brain overlay (quartz_overrides/), runs ``npm install``,
    and writes ``$BRAIN_HOME/Caddyfile`` ready for ``caddy run``.

    Re-running on an existing workspace re-applies the overlay and
    re-renders the Caddyfile without re-cloning.  Use ``--force`` to
    wipe and re-clone.  Does NOT auto-install Caddy or npm — prints
    remediation messages if either is missing.
    """
    try:
        _wiki_install(
            vault=vault,
            port=port,
            force=force,
            no_npm=no_npm,
            dry_run=dry_run,
        )
    except WikiInstallError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# brain claude — Claude Code integration
# ---------------------------------------------------------------------------


@claude_app.command("install-skill")
def claude_install_skill_cmd(
    target: Path | None = typer.Option(
        None,
        "--target",
        help=(
            "Override target root (default ~/.claude/skills); "
            "installs to <target>/brain/SKILL.md"
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite differing target without prompting.",
    ),
    uninstall: bool = typer.Option(
        False,
        "--uninstall",
        help="Remove the skill and its directory if empty.",
    ),
) -> None:
    """Install (or uninstall) the brain Claude Code skill.

    Copies the packaged SKILL.md to ``~/.claude/skills/brain/SKILL.md`` so a
    fresh Claude Code conversation knows about the ``brain`` CLI.  The install
    is idempotent: if the target already has identical bytes it prints "skill
    up to date" and exits 0.  When the target exists but differs, the command
    prompts for confirmation unless ``--force`` is given.

    ``--uninstall`` removes the file and the ``brain/`` directory (only if it
    is empty; never ``rm -rf``).
    """
    try:
        _install_skill(target_root=target, force=force, uninstall=uninstall)
    except SkillInstallError as exc:
        typer.secho(f"error: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# brain uninstall
# ---------------------------------------------------------------------------


@app.command("uninstall")
def uninstall_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    remove_db: bool = typer.Option(
        False,
        "--remove-db",
        help="Also remove $BRAIN_HOME/data/postgres/ (typed confirmation required; DESTROYS DATA)",
    ),
    remove_vault: bool = typer.Option(
        False,
        "--remove-vault",
        help="Also remove $BRAIN_VAULT_PATH (YOUR NOTES — be careful)",
    ),
) -> None:
    """Remove brain runtime state and supervised daemons.

    Removes launchd plists (macOS), stops Docker compose, and deletes the
    $BRAIN_HOME runtime files (.env, .shims/, Caddyfile, etc.).

    By default, $BRAIN_HOME/data/postgres/ (your document database) and
    $BRAIN_VAULT_PATH (your notes) are KEPT.  Use --remove-db and/or
    --remove-vault to delete them explicitly.

    The pipx installation itself is NOT removed — a CLI cannot safely
    uninstall its own running process.  After this command completes, run:

        pipx uninstall second-brain
    """
    from .uninstall import run_uninstall

    try:
        run_uninstall(yes=yes, remove_db=remove_db, remove_vault=remove_vault)
    except typer.Abort:
        typer.secho("Aborted.", fg="yellow")
        raise typer.Exit(code=1) from None


# ---------------------------------------------------------------------------
# brain elicit list
# ---------------------------------------------------------------------------

# Entity types a gap may target — mirrors elicit.schema.TargetType. Used to
# validate the repeatable `--type` filter on `brain elicit list` / `brain elicit`.
_ELICIT_TARGET_TYPES = ("person", "org", "project", "topic", "tool", "doc")


def _load_config_or_exit() -> Config:
    """Load config, turning a ``ConfigError`` into a clean message + exit 1.

    A bad elicit knob (e.g. ``BRAIN_ELICIT_MIN_GAP_SCORE=1.5``) must surface as
    a friendly one-line error on stderr — never a raw Rich traceback — so the
    user can fix the typo. Mirrors the inline handling in ``brain doctor``.
    """
    try:
        return Config.load()
    except ConfigError as e:
        typer.secho(f"Configuration error: {e}", fg="red", err=True)
        raise typer.Exit(code=1) from e


def _validate_elicit_types(type_filter: list[str]) -> list[str] | None:
    """Validate repeatable ``--type`` values; return them as the build_queue arg.

    Returns ``None`` when no filter was supplied (so the read-back is unscoped),
    otherwise the validated list. Raises ``BadParameter`` on any unknown value.
    """
    if not type_filter:
        return None
    cleaned = [t.strip().lower() for t in type_filter]
    unknown = [t for t in cleaned if t not in _ELICIT_TARGET_TYPES]
    if unknown:
        raise typer.BadParameter(
            f"unknown entity type(s): {', '.join(unknown)}. "
            f"Choose from: {', '.join(_ELICIT_TARGET_TYPES)}",
            param_hint="--type",
        )
    return cleaned


@elicit_app.command("list")
def elicit_list(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    limit: int = typer.Option(
        0, "--limit", "-n", help="Max gaps to show (0 = use BRAIN_ELICIT_QUEUE_LIMIT)."
    ),
    low_confidence: bool = typer.Option(
        False, "--low-confidence", help="Include gaps below the score floor."
    ),
    type_filter: list[str] = typer.Option(
        [],
        "--type",
        help="Filter to entity type(s): person|org|project|topic|tool|doc (repeatable).",
    ),
) -> None:
    """Refresh the gap queue and list open knowledge gaps sorted by score.

    Runs the active detectors (delta, orphan, contradiction-when-enabled),
    upserts results into elicitation_gaps, then renders the current open queue.
    """
    import json as _json

    from rich.console import Console
    from rich.table import Table

    from .db import connect
    from .elicit.detectors import (
        ContradictionDetector,
        DeltaDetector,
        GapDetector,
        OrphanEntityDetector,
    )
    from .elicit.queue import build_queue
    from .enrichment import make_enricher

    target_types = _validate_elicit_types(type_filter)

    cfg = _load_config_or_exit()
    effective_limit = limit if limit > 0 else cfg.elicit_queue_limit
    # delta + orphan are Ollama-free and always run. Contradiction detection
    # needs an enricher, so probe Ollama UPFRONT (the cheap /api/tags check
    # brain doctor uses) and wire the detector only when reachable. This keeps
    # `elicit list` offline by default (flag OFF → no check, no enricher) and,
    # when the flag is ON but Ollama is down, runs delta/orphan exactly ONCE —
    # no catch-and-rerun that would re-execute the offline detectors.
    detectors: list[GapDetector] = [DeltaDetector(), OrphanEntityDetector()]
    if cfg.elicit_contradiction_enabled:
        if _ollama_reachable(cfg):
            detectors.append(
                ContradictionDetector(
                    enabled=True,
                    enricher=make_enricher(cfg),
                    min_docs=cfg.elicit_contradiction_min_docs,
                )
            )
        else:
            typer.secho(
                "contradiction detection needs Ollama; skipping "
                f"(Ollama unreachable at {cfg.ollama_host}).",
                fg="yellow",
                err=True,
            )

    with connect(cfg.database_url) as conn:
        gaps = build_queue(
            conn,
            cfg=cfg,
            tenant_id=cfg.graph_tenant_id,
            detectors=detectors,
            limit=effective_limit,
            include_low_confidence=low_confidence,
            target_types=target_types,
        )

    if as_json:
        typer.echo(
            _json.dumps(
                [
                    {
                        "gap_id": g.gap_id,
                        "signal_kind": g.signal_kind,
                        "target_type": g.target_type,
                        "target_id": g.target_id,
                        "target_name": g.target_name,
                        "score": g.score,
                        "evidence_ids": g.evidence_ids,
                        "rationale": g.rationale,
                    }
                    for g in gaps
                ]
            )
        )
        return

    if not gaps:
        typer.echo("No open gaps in the elicitation queue.")
        return

    console = Console()
    table = Table(title="Knowledge Gaps", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Signal", style="cyan", min_width=10)
    table.add_column("Type", style="green", min_width=8)
    table.add_column("Target", min_width=16)
    table.add_column("Score", justify="right", width=7)
    table.add_column("Evidence", justify="right", width=8)
    table.add_column("Rationale", overflow="fold")

    _rationale_preview = 60
    for i, g in enumerate(gaps, 1):
        rationale = g.rationale or ""
        if len(rationale) > _rationale_preview:
            rationale = rationale[:_rationale_preview].rstrip() + "…"
        table.add_row(
            str(i),
            g.signal_kind,
            g.target_type,
            g.target_name or g.target_id,
            f"{g.score:.4f}",
            str(len(g.evidence_ids)),
            rationale,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# brain elicit (default — interactive session)
# ---------------------------------------------------------------------------


@elicit_app.callback(invoke_without_command=True)
def elicit(
    ctx: typer.Context,
    target: str | None = typer.Option(
        None,
        "--target",
        help="Elicit knowledge about one specific entity/topic (skips detection).",
    ),
    signal: str | None = typer.Option(
        None,
        "--signal",
        help="Restrict detection to a single signal: delta|orphan|contradiction.",
    ),
    include_low_confidence: bool = typer.Option(
        False,
        "--include-low-confidence",
        "--low-confidence",
        help="Include gaps below the confidence floor.",
    ),
    type_filter: list[str] = typer.Option(
        [],
        "--type",
        help="Filter to entity type(s): person|org|project|topic|tool|doc (repeatable).",
    ),
) -> None:
    """Interactively review and codify open knowledge gaps.

    With no subcommand this drives the elicitation session: build the gap
    queue, draft a confident rule for each gap, then let you edit & save,
    skip, snooze, or quit. Drafting needs Ollama running; the Ollama-free
    queue view is ``brain elicit list``.
    """
    if ctx.invoked_subcommand is not None:
        return

    from .db import connect
    from .elicit.detectors import (
        ContradictionDetector,
        DeltaDetector,
        GapDetector,
        OrphanEntityDetector,
        UserFlaggedDetector,
    )
    from .elicit.drafter import GapDrafter
    from .elicit.queue import build_queue
    from .elicit.session import run_session
    from .enrichment import make_enricher

    cfg = _load_config_or_exit()
    tenant_id = cfg.graph_tenant_id
    vault_path = _resolve_vault(None, cfg)

    def _contradiction() -> ContradictionDetector:
        return ContradictionDetector(
            enabled=cfg.elicit_contradiction_enabled,
            enricher=make_enricher(cfg) if cfg.elicit_contradiction_enabled else None,
            min_docs=cfg.elicit_contradiction_min_docs,
        )

    # Validate --signal / --type up front (no DB needed) so a typo fails fast.
    sig: str | None = None
    if signal is not None:
        sig = signal.strip().lower()
        if sig not in ("delta", "orphan", "contradiction"):
            raise typer.BadParameter(
                "--signal must be one of: delta, orphan, contradiction",
                param_hint="--signal",
            )
    target_types = _validate_elicit_types(type_filter)

    try:
        with connect(cfg.database_url) as conn:
            conn.autocommit = True

            # Pick detectors AND the matching read-back scope so --target /
            # --signal don't surface unrelated pre-existing open gaps.
            detectors: list[GapDetector]
            signal_kinds: list[str] | None = None
            target_ids: list[str] | None = None
            if target is not None:
                flagged = UserFlaggedDetector(target=target)
                detectors = [flagged]
                signal_kinds = ["user_flagged"]
                # Capture the target_id(s) this flag resolves to so the read-back
                # returns only this target's gap (the detector re-runs inside
                # build_queue; running it once here to capture ids is cheap).
                target_ids = [
                    g.target_id
                    for g in flagged.detect(
                        conn, tenant_id=tenant_id, limit=cfg.elicit_queue_limit
                    )
                ]
            elif sig == "delta":
                detectors = [DeltaDetector()]
                signal_kinds = ["delta"]
            elif sig == "orphan":
                detectors = [OrphanEntityDetector()]
                signal_kinds = ["orphan"]
            elif sig == "contradiction":
                detectors = [_contradiction()]
                signal_kinds = ["contradiction"]
            else:
                detectors = [DeltaDetector(), OrphanEntityDetector(), _contradiction()]

            gaps = build_queue(
                conn,
                cfg=cfg,
                tenant_id=tenant_id,
                detectors=detectors,
                limit=cfg.elicit_queue_limit,
                include_low_confidence=include_low_confidence,
                signal_kinds=signal_kinds,
                target_ids=target_ids,
                target_types=target_types,
            )
            if not gaps:
                typer.echo("No open gaps in the elicitation queue.")
                return
            drafter = GapDrafter(make_enricher(cfg))
            outcomes = run_session(
                cfg,
                conn,
                drafter=drafter,
                gaps=gaps,
                tenant_id=tenant_id,
                vault_path=vault_path,
            )
    except OllamaUnavailable as exc:
        typer.secho(
            f"Elicitation needs Ollama running to draft rules ({exc}). "
            "Start Ollama and retry — `brain elicit list` works without it.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except (EnrichmentError, VaultNoteSyncError, ElicitError) as exc:
        # Drafting / enrichment failure, vault-note authoring failure, or a
        # gap-lifecycle guard tripping. OllamaUnavailable is handled above
        # (it's an EnrichmentError subclass), so this catches the rest cleanly
        # rather than letting a raw traceback escape.
        typer.secho(f"Elicitation failed: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc

    accepted = sum(1 for o in outcomes if o.action == "accepted")
    dismissed = sum(1 for o in outcomes if o.action in ("dismissed", "skipped"))
    snoozed = sum(1 for o in outcomes if o.action == "snoozed")
    typer.echo(
        f"\nReviewed {len(outcomes)} gap(s): "
        f"{accepted} saved, {dismissed} skipped, {snoozed} snoozed."
    )
