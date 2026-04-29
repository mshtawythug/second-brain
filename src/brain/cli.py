"""brain — second brain CLI."""
import json as _json  # aliased — `json` conflicts with the --json output flag name
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx
import psycopg
import typer

from .config import Config, ConfigError
from .db import connect, ensure_embedding_column, run_migrations
from .edit_session import (
    EditorAbortedError,
    EditorError,
    EditorParseFailedError,
    EditorUnchangedError,
    build_payload,
    run_editor_session,
)
from .embeddings import make_embedder
from .errors import (
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
    count_chunks_missing_embedding,
    embedding_column_state,
    fetch_document,
    finalize_embedding_index,
    iter_chunks_missing_embedding,
    list_documents,
    resolve_document_prefix,
    summary_counts,
)
from .search import hybrid_search
from .vault import init_vault
from .vault.export import export_vault

app = typer.Typer(
    name="brain",
    help="Local personal knowledge base. Hybrid search over your career corpus.",
    no_args_is_help=True,
)

vault_app = typer.Typer(
    name="vault",
    help="Vault management.",
    no_args_is_help=True,
)
app.add_typer(vault_app, name="vault")


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
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        applied = run_migrations(conn)
        ensure_embedding_column(conn, embedder)
    if applied:
        for name in applied:
            typer.echo(f"applied {name}")
    else:
        typer.echo("no migrations to apply")
    typer.echo(f"embedder        {cfg.embedder} (dim={embedder.dim})")


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

    if failures:
        raise typer.Exit(code=1)


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
    """Ingest Gmail messages via the `gws` CLI. At least one scope flag is required."""
    if not any([query, label, from_addr, since, until]):
        typer.secho(
            "ingest-gmail requires at least one scope flag: "
            "--query, --label, --from, --since, --until",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)

    cfg = Config.load()
    messages = gmail_ingest.list_messages(
        query=query,
        label=label,
        since=since,
        until=until,
        from_addr=from_addr,
        max_results=max_results,
    )
    typer.echo(f"found {len(messages)} message(s)")
    if dry_run:
        # Stubs returned by users.messages.list only have id + threadId — no
        # subject is available without a per-message read_message() call, which
        # we skip on --dry-run to keep it cheap.
        for m in messages:
            typer.echo(f"  would ingest: [{m['id']}]")
        return

    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for stub in messages:
            try:
                full = gmail_ingest.read_message(stub["id"])
                doc = gmail_ingest.to_extracted_doc(full)
                result = ingest_document(
                    conn,
                    embedder=embedder,
                    doc=doc,
                    source_kind="gmail",
                    source_external_id=stub["id"],
                    source_metadata={
                        "from": doc.metadata.get("from"),
                        "date": doc.metadata.get("date"),
                    },
                    tags=list(tag),
                )
                verb = "ingested" if result.created else "skipped"
                typer.echo(f"  {verb}: {doc.title[:60]}")
            except (GmailError, psycopg.Error, ValueError) as e:
                typer.secho(
                    f"  failed: {stub.get('id', '?')} — {e}", fg="red"
                )
                continue


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
) -> None:
    """Add (+name) or remove (-name) tags. Example: brain tag abc1234 +interview -draft"""
    add = [m[1:] for m in mods if m.startswith("+") and len(m) > 1]
    remove = [m[1:] for m in mods if m.startswith("-") and len(m) > 1]
    if not (add or remove):
        raise typer.BadParameter("expected +tag or -tag arguments")
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id)
        apply_tags(conn, doc_id, add=add, remove=remove)
    typer.echo(f"updated tags on {doc_id[:8]}")


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
            )
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            return 1
    _print_update_result(result, doc_id)
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

    With no flags, opens an editor on a JSON-header + body payload that lets
    you edit every field at once. With flags, applies a targeted update —
    body changes re-chunk + re-embed; metadata/title/type changes are a
    single SQL UPDATE.
    """
    # Reject `--replace-metadata` without `--metadata` regardless of which
    # mode we're about to enter — silently ignoring it lets a user think the
    # full-replace happened when nothing was passed for it to swap.
    if replace_metadata and metadata is None:
        raise typer.BadParameter("--replace-metadata requires --metadata")

    if not _has_mutating_edit_flag(
        title=title,
        content_type=content_type,
        metadata=metadata,
        content_file=content_file,
        content_stdin=content_stdin,
    ):
        cfg = Config.load()
        with connect(cfg.database_url) as conn:
            doc_id = _resolve_id(conn, id)
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

    cfg = Config.load()
    embedder: Any = _build_embedder(cfg) if new_content is not None else None
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id)
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
    """Delete a document (and its chunks) from the brain."""
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        doc_id = _resolve_id(conn, id)
        row = conn.execute(
            "SELECT title FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
        assert row is not None  # _resolve_id confirmed the doc exists
        title = row[0]
        if not yes:
            typer.confirm(f"Delete '{title}' ({doc_id[:8]})?", abort=True)
        conn.execute("DELETE FROM documents WHERE id=%s", (doc_id,))
    typer.echo(f"deleted {doc_id[:8]}")


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
