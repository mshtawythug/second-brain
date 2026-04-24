"""brain — second brain CLI."""
import typer

from .config import Config
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
    else:  # pragma: no cover
        typer.echo("no migrations to apply")
