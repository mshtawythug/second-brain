"""brain — second brain CLI."""
import shutil

import psycopg
import typer

from .config import Config, ConfigError
from .db import connect, run_migrations

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
