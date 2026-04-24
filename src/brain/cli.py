"""brain — second brain CLI."""
import typer

app = typer.Typer(
    name="brain",
    help="Local personal knowledge base. Hybrid search over your career corpus.",
    no_args_is_help=True,
)


@app.command()
def hello() -> None:
    """Sanity-check command — remove once real commands land."""
    typer.echo("brain is alive")
