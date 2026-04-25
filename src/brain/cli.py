"""brain — second brain CLI."""
import json as _json  # aliased — `json` conflicts with the --json output flag name
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import psycopg
import typer

from .config import Config, ConfigError
from .db import connect, run_migrations
from .embeddings import VoyageEmbedder
from .format import console, emit_json, search_table
from .ingest import (
    extract_path,
    ingest_document,
    supported_extensions,
)
from .ingest import gmail as gmail_ingest
from .ingest.stdin import make_doc as _stdin_make_doc
from .search import hybrid_search

# UUID prefixes consist solely of hex digits and hyphens; anything else is rejected
# before reaching SQL so user-supplied `_` / `%` cannot act as LIKE wildcards.
_UUID_PREFIX_RE = re.compile(r"[0-9a-f-]+")

app = typer.Typer(
    name="brain",
    help="Local personal knowledge base. Hybrid search over your career corpus.",
    no_args_is_help=True,
)


@app.callback()
def _main() -> None:
    """brain — second brain CLI root."""


@app.command()
def init() -> None:
    """Apply database migrations."""
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        applied = run_migrations(conn)
    if applied:
        for name in applied:
            typer.echo(f"applied {name}")
    else:
        typer.echo("no migrations to apply")


@app.command()
def doctor() -> None:
    """Check environment, database connection, and external dependencies."""
    failures: list[str] = []

    try:
        cfg = Config.load()
        typer.echo("env             OK")
    except ConfigError as e:
        typer.secho(f"env             FAIL — {e}", fg="red", err=True)
        raise typer.Exit(code=1) from e

    try:
        with connect(cfg.database_url) as conn:
            conn.execute("SELECT 1")
            ext = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            ).fetchone()
        if not ext:
            failures.append("pgvector extension not installed (run brain init)")
            typer.echo("postgres        FAIL — pgvector not installed")
        else:
            typer.echo(f"postgres        OK (pgvector {ext[0]})")
    except psycopg.Error as e:
        failures.append(f"database: {e}")
        typer.secho(f"postgres        FAIL — {e}", fg="red", err=True)

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
        doc_row = conn.execute("SELECT count(*) FROM documents").fetchone()
        chunk_row = conn.execute("SELECT count(*) FROM chunks").fetchone()
        source_row = conn.execute("SELECT count(*) FROM sources").fetchone()
        last_row = conn.execute("SELECT max(ingested_at) FROM documents").fetchone()
        by_kind = conn.execute(
            "SELECT coalesce(s.kind, 'manual') AS kind, count(*) "
            "FROM documents d LEFT JOIN sources s ON s.id = d.source_id "
            "GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()

    doc_count = doc_row[0] if doc_row else 0
    chunk_count = chunk_row[0] if chunk_row else 0
    source_count = source_row[0] if source_row else 0
    last = last_row[0] if last_row else None

    typer.echo(f"documents       {doc_count}")
    typer.echo(f"chunks          {chunk_count}")
    typer.echo(f"sources         {source_count}")
    typer.echo(f"last ingest     {last or 'never'}")
    typer.echo("\nby source:")
    for kind, count in by_kind:
        typer.echo(f"  {kind:<12} {count}")


def _build_embedder(cfg: Config) -> VoyageEmbedder:
    """Build a VoyageEmbedder from config. Indirected so tests can substitute a fake."""
    # pragma: no cover - exercised only against the real Voyage service
    return VoyageEmbedder(api_key=cfg.voyage_api_key)  # pragma: no cover


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
        for m in messages:
            typer.echo(f"  would ingest: [{m['id']}] {m.get('subject', '(no subject)')}")
        return

    embedder = _build_embedder(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for stub in messages:
            full = gmail_ingest.read_message(stub["id"])
            doc = gmail_ingest.to_extracted_doc(full)
            result = ingest_document(
                conn,
                embedder=embedder,
                doc=doc,
                source_kind="gmail",
                source_external_id=full["id"],
                source_metadata={"from": full.get("from"), "date": full.get("date")},
                tags=list(tag),
            )
            verb = "ingested" if result.created else "skipped"
            subject = full.get("subject", "(no subject)")
            typer.echo(f"  {verb}: {subject[:60]}")


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
    """Resolve a UUID prefix (min 6 chars) to a full document id."""
    if len(prefix) < 6:
        raise typer.BadParameter("id prefix must be at least 6 characters")
    if not _UUID_PREFIX_RE.fullmatch(prefix):
        raise typer.BadParameter("id prefix must contain only hex digits and hyphens")
    rows = conn.execute(
        "SELECT id::text FROM documents WHERE id::text LIKE %s",
        (prefix + "%",),
    ).fetchall()
    if not rows:
        typer.secho(f"document not found: {prefix}", fg="red", err=True)
        raise typer.Exit(code=1)
    if len(rows) > 1:
        typer.secho(f"id prefix ambiguous: {prefix}", fg="red", err=True)
        raise typer.Exit(code=1)
    return str(rows[0][0])


@app.command()
def show(
    id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print a document by id (or 6+ char prefix)."""
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        doc_id = _resolve_id(conn, id)
        row = conn.execute(
            """
            SELECT d.id::text, d.title, d.content, d.content_type, d.tags,
                   d.source_path, d.ingested_at, s.kind
            FROM documents d
            LEFT JOIN sources s ON s.id = d.source_id
            WHERE d.id = %s
            """,
            (doc_id,),
        ).fetchone()
    assert row is not None  # _resolve_id confirmed the doc exists
    payload = {
        "id": row[0],
        "title": row[1],
        "content": row[2],
        "content_type": row[3],
        "tags": list(row[4] or []),
        "source_path": row[5],
        "ingested_at": row[6],
        "source_kind": row[7],
    }
    if json_output:
        emit_json(payload)
        return
    typer.echo(f"# {payload['title']}")
    typer.echo(f"id:           {payload['id']}")
    typer.echo(f"source:       {payload['source_kind'] or 'manual'} ({payload['content_type']})")
    typer.echo(f"tags:         {', '.join(payload['tags']) or '(none)'}")
    typer.echo(f"ingested:     {payload['ingested_at']}")
    typer.echo("")
    typer.echo(payload["content"])


@app.command(name="list")
def list_docs(
    source: str | None = typer.Option(None, "--source"),
    tag: str | None = typer.Option(None, "--tag"),
    limit: int = typer.Option(20, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List documents in the brain."""
    cfg = Config.load()
    where = ["TRUE"]
    params: list[Any] = []
    if source:
        where.append("s.kind = %s")
        params.append(source)
    if tag:
        where.append("%s = ANY(d.tags)")
        params.append(tag)
    sql = f"""
        SELECT d.id::text, d.title, d.content_type, d.tags, s.kind, d.ingested_at
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE {" AND ".join(where)}
        ORDER BY d.ingested_at DESC
        LIMIT %s
    """
    params.append(limit)
    with connect(cfg.database_url) as conn:
        rows = conn.execute(sql, params).fetchall()
    if json_output:
        emit_json(
            [
                {
                    "id": r[0],
                    "title": r[1],
                    "content_type": r[2],
                    "tags": list(r[3] or []),
                    "source_kind": r[4],
                    "ingested_at": r[5],
                }
                for r in rows
            ]
        )
        return
    for r in rows:
        kind = r[4] or "manual"
        typer.echo(f"{r[0][:8]}  {kind:<8}  {r[2]:<10}  {r[1]}")


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
        if add:
            conn.execute(
                "UPDATE documents SET tags = ARRAY(SELECT DISTINCT unnest(tags || %s::text[])) "
                "WHERE id = %s",
                (add, doc_id),
            )
        if remove:
            conn.execute(
                "UPDATE documents SET tags = ARRAY(SELECT t FROM unnest(tags) AS t "
                "WHERE t <> ALL(%s::text[])) WHERE id = %s",
                (remove, doc_id),
            )
    typer.echo(f"updated tags on {doc_id[:8]}")


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
