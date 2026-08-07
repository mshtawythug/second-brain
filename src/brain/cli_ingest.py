"""`brain ingest*` / `brain reembed` — corpus write-path commands.

Extracted verbatim from :mod:`brain.cli` (which had grown past the 800-line
ceiling in CLAUDE.md). Behaviour is unchanged — command names, flags, help
text, output and exit codes are identical to the previous in-``cli.py``
definitions.

Shared helpers still owned by ``cli.py`` are resolved through the ``brain.cli``
module object *at call time* (see the delegation block below) rather than bound
at import: ``cli.py`` imports this module to register its commands, so a
module-level import back would be a cycle. Reading the attribute at call time
additionally keeps ``monkeypatch.setattr("brain.cli.<name>", ...)`` — the patch
point the existing test suite uses — effective for these commands. Same pattern
as :mod:`brain._capture_command`.
"""
from __future__ import annotations

import json as _json  # aliased — `json` conflicts with the --json output flag name
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import typer

from .agent import resolve_agent_id
from .config import Config
from .db import connect
from .embeddings import is_hosted_embedder
from .errors import SecretGuardError, SensitivityError
from .ingest import (
    Embedder,
    IngestResult,
    extract_path,
    ingest_document,
    supported_extensions,
)
from .ingest import gmail as gmail_ingest
from .ingest.gmail import GmailError
from .ingest.stdin import make_doc as _stdin_make_doc
from .queries import (
    count_chunks_missing_embedding,
    count_confidential_documents,
    iter_chunks_missing_embedding,
)
from .sensitivity import DEFAULT_SENSITIVITY, normalize_level
from .vault.derived_links import real_gws_runner

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher
    from .graph_rag.sync import GraphSyncer

# ---------------------------------------------------------------------------
# Delegation to `brain.cli`-owned helpers.
#
# These names stay in `cli.py` because commands that did NOT move still call
# them (`_build_enricher` -> enrich/tag/edit/review; `_build_graph_syncer` ->
# edit/vault sync; `_analyze_after_bulk_write` -> vault sync) and because the
# test suite patches them at `brain.cli.<name>`. Each wrapper resolves the
# attribute at call time, so the moved command bodies below are byte-identical
# to their pre-move form.
# ---------------------------------------------------------------------------


def _build_embedder(cfg: Config) -> Embedder:
    """Build the configured embedder via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_embedder(cfg)  # type: ignore[attr-defined]


def _build_enricher(cfg: Config) -> OllamaEnricher:
    """Build the Ollama enricher via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_enricher(cfg)


def _build_graph_syncer(cfg: Config) -> GraphSyncer:
    """Build the people-aspect graph syncer via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_graph_syncer(cfg)


def _analyze_after_bulk_write(conn: psycopg.Connection[Any], *, context: str) -> None:
    """Refresh planner stats via the ``brain.cli`` patch point."""
    from . import cli as _cli

    _cli._analyze_after_bulk_write(conn, context=context)


def finalize_embedding_index(conn: psycopg.Connection[Any], embedder: Embedder) -> None:
    """Apply NOT NULL / HNSW via the ``brain.cli`` patch point."""
    from . import cli as _cli

    _cli.finalize_embedding_index(conn, embedder)


# ---------------------------------------------------------------------------
# Ingest-only helpers (moved with their commands — no other caller).
# ---------------------------------------------------------------------------

# F4 secret guard. One help string shared by all four ingest commands so the
# wording cannot drift between them.
_ALLOW_SECRETS_HELP = (
    "Skip the ingest-time secret guard for THIS invocation "
    "(BRAIN_SECRET_GUARD). Findings are still printed; nothing is redacted "
    "or refused."
)


_SENSITIVITY_HELP = (
    "Sensitivity tier for the ingested document(s): normal|confidential. "
    "'confidential' keeps the body off a hosted embedder (chunks are stored "
    "with NULL embeddings, so the doc is findable by full-text search only), "
    "out of MCP brain_show by default, and off the published wiki."
)


def _emit_secret_notice(notice: str) -> None:
    """Print the guard's finding block to STDERR, or nothing when it is empty.

    Stderr is not a detail — it is the backward-compatibility contract. Every
    ingest command's stdout line stays byte-identical to its pre-F4 form, so
    scripts (and ``tests/test_cli_ingest.py``) that parse stdout are unaffected
    whether or not a document trips the guard.
    """
    if notice:
        typer.secho(notice, fg="yellow", err=True)


def _validate_sensitivity_or_exit(level: str) -> str:
    """Validate a ``--sensitivity`` value, exiting 1 with a clear message if bad.

    Exists so the bulk commands (``ingest-dir``) can validate ONCE up front
    rather than discovering a typo on file 400 of 900, by which point 399
    documents would already be stored at the wrong tier. The single-file
    commands let ``ingest_document`` raise instead, since there is no partial
    state to protect.
    """
    try:
        return normalize_level(level)
    except SensitivityError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(code=1) from e


def _emit_egress_notice(notice: str) -> None:
    """Print the F6 hosted-egress notice to STDERR, or nothing when empty.

    Kept separate from :func:`_emit_secret_notice` despite the identical body:
    these are two independent boundaries that can both fire on one document (a
    note that contains a credential AND is marked confidential), so a caller
    must be able to report either without implying the other. Same stderr
    contract — stdout stays byte-identical.
    """
    if notice:
        typer.secho(notice, fg="yellow", err=True)


def _attribute_to_agent(doc: Any, agent_id: str | None) -> Any:
    """Stamp ambient agent attribution onto an extracted document (F10).

    ``BRAIN_AGENT_ID`` in the environment is an affirmative statement by
    whoever set it — "everything from this process is me". Nobody sets it by
    accident. So a file ingest run under it IS agent-driven work, and
    recording the document as unattributed would be its own fabricated fact,
    the mirror image of the one the spec set out to avoid.

    **This overrides F2-F7-F10 §5.2**, which wired attribution to
    ``ingest-stdin`` only. The spec's rationale holds for the ``--agent``
    FLAG, which stays off ``ingest`` / ``ingest-dir`` — a hand-run ingest
    should not carry an explicit agent. It does not hold for the ambient env
    var, where the alternative was ``brain search`` and ``brain ingest``
    disagreeing about the same variable in the same process.

    Returns the doc unchanged when there is nothing to attribute, and a NEW
    doc otherwise — ``ExtractedDoc`` is a value object and mutating a caller's
    metadata dict in place would leak across ``ingest-dir``'s loop.
    """
    if agent_id is None:
        return doc
    return replace(doc, metadata={**doc.metadata, "agent_id": agent_id})


def _ingest_outcome_verb(
    result: IngestResult, *, force: bool = False,
    already_verb: str = "skipped (already ingested)",
) -> str:
    """Map an :class:`IngestResult` to a human status verb.

    A doc that produced no chunks (whitespace-only file, image-only PDF) comes
    back as ``document_id=None, created=False`` — that is an *empty* document,
    not an unchanged re-ingest, so it gets its own verb (Task 2.12(b)). ``force``
    (single-file / stdin ingest) reports an in-place rewrite as "updated" even
    when the body hash is unchanged. ``already_verb`` lets ``ingest-dir`` keep
    its historical bare "skipped" wording while ``ingest`` / ``ingest-stdin``
    spell out "skipped (already ingested)".

    ``result.mirror_repaired`` (#23) reports that the run gave an existing row
    its missing vault file back. It never *replaces* a louder verb — a created
    or updated document is still reported as such — it only overrides the
    "nothing happened" wording, and annotates "updated" so a repair is never
    silently absorbed by a body change that happened in the same run.
    """
    if result.document_id is None:
        return "skipped (empty document)"
    if result.created:
        return "ingested"
    if result.body_changed or force:
        # A repair alongside a real body change: report BOTH. "updated" alone
        # is true but swallows the mirror fix, and the mirror fix is the part
        # the user is waiting to hear about.
        return "updated (mirror repaired)" if result.mirror_repaired else "updated"
    if result.mirror_repaired:
        # The case this branch exists for. The body was unchanged, so every
        # other rule here says "skipped" — but the run just gave an orphaned
        # document its vault file back, which is precisely the work the user
        # re-ran the command to get. Reporting "skipped" would teach them the
        # opposite of what happened, and it is the same class of failure as
        # the silent success the repair path was added to fix.
        return "repaired mirror"
    return already_verb


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
    allow_secrets: bool = typer.Option(
        False, "--allow-secrets", help=_ALLOW_SECRETS_HELP
    ),
    sensitivity: str = typer.Option(
        DEFAULT_SENSITIVITY, "--sensitivity", help=_SENSITIVITY_HELP
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
        try:
            result = ingest_document(
                conn,
                embedder=embedder,
                doc=_attribute_to_agent(doc, resolve_agent_id(None, cfg)),
                source_kind="manual",
                tags=list(tag),
                force=force,
                vault_root=cfg.vault_path,
                enricher=enricher,
                enrich=not no_enrich,
                enrich_min_tokens=cfg.enrich_min_tokens,
                graph_syncer=graph_syncer,
                secret_guard=cfg.secret_guard,
                allow_secrets=allow_secrets,
                sensitivity=sensitivity,
            )
        except SecretGuardError as e:
            # Single-file ingest: a refusal is the whole command's outcome, so
            # exit non-zero. Nothing was written — the guard raises before the
            # content hash and before the write transaction opens.
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e
        except SensitivityError as e:
            # An invalid --sensitivity is a usage error, and it is raised before
            # any write for exactly that reason: a user who typo'd the level must
            # not end up with a document they believe is protected.
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e
    _emit_secret_notice(result.secret_notice)
    _emit_egress_notice(result.egress_notice)
    verb = _ingest_outcome_verb(result, force=force)
    typer.echo(f"{verb}: {path.name} → {result.document_id}")


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
    allow_secrets: bool = typer.Option(
        False, "--allow-secrets", help=_ALLOW_SECRETS_HELP + " Applies to EVERY file in the walk."
    ),
    sensitivity: str = typer.Option(
        DEFAULT_SENSITIVITY,
        "--sensitivity",
        help=_SENSITIVITY_HELP + " Applies to EVERY file in the walk.",
    ),
) -> None:
    """Recursively ingest a directory of files."""
    cfg = Config.load()
    # Validate ONCE, before the walk — not per file. A typo'd level must fail
    # before the first write, not on file 400 of 900 with 399 documents already
    # stored at the wrong tier.
    sensitivity = _validate_sensitivity_or_exit(sensitivity)
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
    wrote = 0
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for f in files:
            try:
                doc = extract_path(f)
                result = ingest_document(
                    conn,
                    embedder=embedder,
                    doc=_attribute_to_agent(doc, resolve_agent_id(None, cfg)),
                    source_kind="manual",
                    tags=list(tag),
                    vault_root=cfg.vault_path,
                    enricher=enricher,
                    enrich=not no_enrich,
                    enrich_min_tokens=cfg.enrich_min_tokens,
                    graph_syncer=graph_syncer,
                    secret_guard=cfg.secret_guard,
                    allow_secrets=allow_secrets,
                    sensitivity=sensitivity,
                )
                _emit_secret_notice(result.secret_notice)
                _emit_egress_notice(result.egress_notice)
                verb = _ingest_outcome_verb(result, already_verb="skipped")
                if result.created or result.body_changed:
                    wrote += 1
                typer.echo(f"  {verb}: {f.name}")
            except SecretGuardError as e:
                # Per-FILE refusal, not a run abort. Under `reject`, one
                # false-positive file at #400 of 900 must not kill the walk —
                # that scenario is precisely why `warn` is the default. The
                # file is skipped, the finding block is printed, and the walk
                # continues, matching how every other per-file failure below
                # already behaves.
                typer.secho(f"  refused: {f.name}", fg="red", err=True)
                typer.secho(str(e), fg="red", err=True)
            except (ValueError, OSError, psycopg.Error) as e:
                typer.secho(f"  failed: {f.name} — {e}", fg="red")
        # Refresh planner stats once, after the batch, only if anything landed.
        if wrote:
            _analyze_after_bulk_write(conn, context="ingest-dir")


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
    allow_secrets: bool = typer.Option(
        False, "--allow-secrets", help=_ALLOW_SECRETS_HELP
    ),
    sensitivity: str = typer.Option(
        DEFAULT_SENSITIVITY, "--sensitivity", help=_SENSITIVITY_HELP
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        help=(
            "Attribute the ingested document to an agent id. Overrides "
            "BRAIN_AGENT_ID and any agent_id in --metadata. Unset means "
            "unattributed."
        ),
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
    cfg = Config.load()
    # F10 — the flag wins over a same-named --metadata key by ASSIGNING rather
    # than setdefault()-ing. An explicit --agent is the more specific, more
    # recent statement of intent; letting a stale agent_id inside a hand-built
    # metadata blob silently outrank it would misattribute the document.
    # Resolved before the doc is built so a malformed id fails fast, and so
    # BRAIN_AGENT_ID applies when the flag is absent.
    resolved_agent = resolve_agent_id(agent, cfg)
    if resolved_agent is not None:
        meta["agent_id"] = resolved_agent

    doc = _stdin_make_doc(
        content=content,
        title=title,
        content_type=content_type,
        metadata=meta,
    )

    embedder = _build_embedder(cfg)
    enricher = None if no_enrich else _build_enricher(cfg)
    graph_syncer = _build_graph_syncer(cfg)
    # Krisp ingest triggers Calendar/Contacts directory refresh via the gws
    # CLI; other sources don't need a runner. Refresh failures are warnings,
    # not errors — the ingest itself still succeeds.
    gws_runner = real_gws_runner if source == "krisp" else None
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        try:
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
                secret_guard=cfg.secret_guard,
                allow_secrets=allow_secrets,
                sensitivity=sensitivity,
            )
        except SecretGuardError as e:
            # Same shape as the `stdin was empty` refusal above: red on stderr,
            # exit 1, nothing written.
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e
        except SensitivityError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(code=1) from e
    _emit_secret_notice(result.secret_notice)
    _emit_egress_notice(result.egress_notice)
    verb = _ingest_outcome_verb(result, force=force)
    typer.echo(f"{verb}: {title} → {result.document_id}")


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
    allow_secrets: bool = typer.Option(
        False, "--allow-secrets", help=_ALLOW_SECRETS_HELP + " Applies to EVERY thread in the pull."
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
                    secret_guard=cfg.secret_guard,
                    allow_secrets=allow_secrets,
                )
                _emit_secret_notice(result.secret_notice)
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
            except SecretGuardError as e:
                # Per-THREAD refusal. A bulk pull is exactly where a `reject`
                # false positive would otherwise abort a long-running job
                # (spec Q4), so the thread is counted as failed and the pull
                # continues. `--allow-secrets` is offered on this command for
                # the same reason.
                typer.secho(
                    f"  refused thread {tid} ({len(ts)} messages)",
                    fg="red",
                    err=True,
                )
                typer.secho(str(e), fg="red", err=True)
                failed += 1
                continue
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

    # FTS-only backend: there are no vectors to backfill, and finalize (NOT NULL
    # + HNSW) must NOT run — the column stays nullable so the docs are still
    # ingestable/searchable. Short-circuit BEFORE any DB work (and before the
    # --dry-run branch) so `brain reembed` is a friendly no-op instead of
    # crashing on ``NullEmbedder.embed()``.
    if not getattr(embedder, "produces_embeddings", True):
        typer.echo(
            "FTS-only backend (BRAIN_EMBEDDER=none) — nothing to reembed. "
            "Install Ollama, set BRAIN_EMBEDDER=arctic, then run 'brain init' "
            "and 'brain reembed'."
        )
        return

    # F6: the same hosted-egress veto ingest applies. Without it `brain reembed`
    # would undo the boundary wholesale — every body withheld at ingest would be
    # shipped to the hosted service in one batch.
    hosted = is_hosted_embedder(embedder)

    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        confidential_docs = count_confidential_documents(conn) if hosted else 0
        skip_confidential = hosted and confidential_docs > 0
        target_total = count_chunks_missing_embedding(
            conn,
            include_embedded=all_chunks,
            exclude_confidential=skip_confidential,
        )
        target = min(limit, target_total) if limit is not None else target_total
        scope = "chunk(s) total" if all_chunks else "chunk(s) have NULL embedding"

        if dry_run:
            typer.echo(f"would embed {target} chunk(s)")
            typer.echo(f"  ({target_total} {scope})")
            return

        embedded = 0
        if target_total == 0:
            typer.echo("nothing to embed (all chunks have embeddings)")
        else:
            for batch in iter_chunks_missing_embedding(
                conn,
                batch_size=batch_size,
                include_embedded=all_chunks,
                exclude_confidential=skip_confidential,
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

        # Report the withheld work explicitly. A veto that silently shrinks the
        # denominator reads as "everything is embedded" — the user must be told
        # these chunks are FTS-only on purpose and how to change that.
        if skip_confidential:
            typer.echo(
                f"{confidential_docs} confidential document(s) skipped "
                f"(hosted embedder — bodies not sent off-machine); their chunks "
                f"stay FTS-only. Switch to a local embedder to give them vectors."
            )

        if finalize:
            # Under a hosted embedder with confidential rows present, finalize
            # CANNOT run: it applies NOT NULL to chunks.embedding while those
            # chunks are deliberately NULL. This branch comes FIRST so the
            # message names the real reason, rather than reporting a NULL backlog
            # the user has no way to clear without giving up the boundary.
            if skip_confidential:
                typer.echo(
                    "finalize skipped: confidential document(s) hold NULL "
                    "embeddings by design under a hosted embedder. NOT NULL "
                    "would refuse them. Switch to a local embedder and re-run "
                    "`brain reembed` to finalize."
                )
            else:
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
                        f"finalize skipped: {remaining} chunk(s) still have "
                        f"NULL embedding"
                    )

        # Refresh planner stats once the embeddings actually changed.
        if embedded:
            _analyze_after_bulk_write(conn, context="reembed")


def register(app: typer.Typer) -> None:
    """Attach the ingest / reembed commands to ``app``.

    Called from ``cli.py`` at the point the commands used to be declared —
    Typer lists commands in registration order, so the position of this call
    is what keeps ``brain --help`` byte-identical. Command names are passed
    explicitly wherever the original decorator did so.
    """
    app.command()(ingest)
    app.command(name="ingest-dir")(ingest_dir)
    app.command(name="ingest-stdin")(ingest_stdin)
    app.command(name="ingest-gmail")(ingest_gmail)
    app.command()(reembed)
