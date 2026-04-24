"""brain — second brain CLI."""
import shutil
from pathlib import Path

import psycopg
import typer

from .config import Config, ConfigError
from .db import connect, run_migrations
from .embeddings import VoyageEmbedder
from .ingest import (
    extract_path,
    ingest_document,
    supported_extensions,
)

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
