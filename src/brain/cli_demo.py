"""`brain demo` CLI sub-app — a zero-Ollama taste test with a synthetic corpus.

Thin Typer orchestration over :mod:`brain.demo`: the bare ``brain demo``
provisions an isolated throwaway Postgres (or seeds a caller-supplied
``--database-url``), seeds the 22-doc synthetic Larkspur corpus with the
deterministic :class:`~brain.demo.embedder.DemoEmbedder`, and runs the hero
query inline. Sub-commands ``query`` / ``status`` / ``teardown`` operate the
running sandbox. All provisioning + search primitives live in
:mod:`brain.demo`; this module only maps them to Rich/Typer output.
"""
from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import typer

from . import demo as demo_mod
from .errors import BrainError
from .format import console, emit_json, search_table
from .search import SearchResult

# The headline query a first-time visitor sees ranked results for.
HERO_QUERY = "compliance horror stories"

# Follow-up query prompts printed after the hero query so the visitor keeps
# exploring (the show + teardown lines are appended dynamically).
_NEXT_STEPS: tuple[str, ...] = (
    'brain demo query "SOC 2 evidence request"',
    'brain demo query "PCI scope creep"',
    'brain demo query "vendor risk" --source slack',
    'brain demo query "GDPR deletion request"',
)

# The exact guidance when Docker is missing and no --database-url was supplied.
_DOCKER_MISSING_MSG = (
    "Docker not found — brain demo needs Docker, or pass --database-url "
    "<postgres-url> to seed an existing empty database."
)

demo_app = typer.Typer(
    name="demo",
    help=(
        "Zero-Ollama taste test: spin up a throwaway Postgres, seed a synthetic "
        "compliance corpus, and search it — see ranked results in under two "
        "minutes with no personal data and no model downloads."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)


def _fail(message: str) -> None:
    """Print an error and exit non-zero (mirrors the cli_connect idiom)."""
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=1)


@contextmanager
def _quiet_internal_logs() -> Iterator[None]:
    """Silence brain's INFO/WARNING logs for a clean demo transcript.

    The seed path emits operational warnings (e.g. the Krisp hook noting no
    ``gws`` runner) that are irrelevant to a first-time visitor and make the
    marketing demo look noisy. Real errors (ERROR+) still surface. The prior
    level is restored on exit so this never leaks into a longer-lived process
    (e.g. cross-test contamination under pytest).
    """
    logger = logging.getLogger("brain")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


def _render_results(results: list[SearchResult], *, json_output: bool) -> None:
    """Render hero/query results as JSON or a Rich table."""
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
    console.print(search_table(results, title=f"brain demo · {HERO_QUERY!r}"))


def _print_next_steps(results: list[SearchResult]) -> None:
    """Print the "Try these next" block after the inline hero query."""
    console.print("\n[bold]Try these next:[/bold]")
    for suggestion in _NEXT_STEPS:
        console.print(f"  [cyan]{suggestion}[/cyan]")
    if results:
        short_id = results[0].document_id[:8]
        console.print(
            f"  [cyan]brain show {short_id}[/cyan]"
            "   # read the top hit in full (in your own brain)"
        )
    console.print("  [cyan]brain demo teardown[/cyan]   # remove the sandbox when done")


def _resolve_default_database_url(port: int, database_url: str | None) -> str:
    """Resolve the DB URL for the default flow: caller-supplied or provisioned.

    With ``--database-url`` the demo seeds that database and never touches
    Docker (the CI / power-user seam). Otherwise Docker is required: absent, we
    exit with actionable guidance; present, we auto-bump off a busy port and
    provision the isolated sandbox.
    """
    if database_url is not None:
        return database_url
    if shutil.which("docker") is None:
        _fail(_DOCKER_MISSING_MSG)
    resolved_port = demo_mod.resolve_port(port)
    typer.echo(f"Provisioning the demo Postgres on port {resolved_port} …")
    return demo_mod.provision(resolved_port)


@demo_app.callback(invoke_without_command=True)
def demo_default(
    ctx: typer.Context,
    port: int = typer.Option(
        demo_mod.DEFAULT_DEMO_PORT, "--port",
        help="Host port for the demo Postgres (auto-bumps if busy).",
    ),
    with_embeddings: bool = typer.Option(
        False, "--with-embeddings",
        help="Also build vector embeddings + HNSW index (default: FTS-only).",
    ),
    database_url: str | None = typer.Option(
        None, "--database-url",
        help="Seed this existing empty Postgres instead of provisioning Docker.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the hero-query results as JSON."
    ),
) -> None:
    """Provision, seed, and run the hero query inline (the default flow)."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        with _quiet_internal_logs():
            resolved_url = _resolve_default_database_url(port, database_url)
            typer.echo("Seeding the synthetic Larkspur corpus (22 docs, no Ollama) …")
            report = demo_mod.seed_demo(resolved_url, with_embeddings=with_embeddings)
            typer.echo(
                f"Seeded {report.ingested} new doc(s) "
                f"({report.skipped} already present).\n"
            )
            results = demo_mod.query_demo(
                resolved_url,
                HERO_QUERY,
                limit=5,
                with_embeddings=with_embeddings,
            )
    except BrainError as exc:
        _fail(str(exc))
    _render_results(results, json_output=json_output)
    if not json_output:
        _print_next_steps(results)


@demo_app.command("query")
def demo_query(
    query: str = typer.Argument(..., help="Search text to run over the demo corpus."),
    limit: int = typer.Option(5, "--limit", "-n", min=1),
    source: str | None = typer.Option(
        None, "--source", help="Filter by source kind (manual/krisp/slack/gmail)."
    ),
    tag: str | None = typer.Option(None, "--tag", help="Filter by tag."),
    person: str | None = typer.Option(
        None, "--person", help="Filter by a participant name (e.g. 'Priya Okafor')."
    ),
    after: datetime | None = typer.Option(
        None, "--after", formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs dated on or after this ISO date.",
    ),
    with_embeddings: bool = typer.Option(
        False, "--with-embeddings", help="Use the vector leg too (default: FTS-only)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead."),
    port: int = typer.Option(
        demo_mod.DEFAULT_DEMO_PORT, "--port", help="Host port of the demo Postgres."
    ),
    database_url: str | None = typer.Option(
        None, "--database-url", help="Query this Postgres instead of the demo container."
    ),
) -> None:
    """Search the seeded demo corpus (the running sandbox, or --database-url)."""
    resolved_url = database_url or demo_mod.demo_database_url(port)
    try:
        results = demo_mod.query_demo(
            resolved_url,
            query,
            limit=limit,
            source=source,
            tag=tag,
            person=person,
            after=after,
            with_embeddings=with_embeddings,
        )
    except BrainError as exc:
        _fail(str(exc))
    if json_output:
        _render_results(results, json_output=True)
        return
    if not results:
        typer.echo("(no results)")
        return
    console.print(search_table(results, title=f"brain demo · {query!r}"))


@demo_app.command("status")
def demo_status(
    port: int = typer.Option(
        demo_mod.DEFAULT_DEMO_PORT, "--port", help="Host port of the demo Postgres."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead."),
) -> None:
    """Report whether the demo sandbox is running and how many docs it holds."""
    snapshot = demo_mod.status(port)
    if json_output:
        emit_json(
            {
                "running": snapshot.running,
                "container": snapshot.container,
                "database_url": snapshot.database_url,
                "doc_count": snapshot.doc_count,
            }
        )
        return
    if not snapshot.running:
        typer.echo("demo: not running (run `brain demo` to start it)")
        return
    docs = "unknown" if snapshot.doc_count is None else str(snapshot.doc_count)
    typer.echo(f"demo: running · container {snapshot.container} · {docs} doc(s)")


@demo_app.command("teardown")
def demo_teardown() -> None:
    """Destroy the demo sandbox and its data (`docker compose down -v`)."""
    demo_mod.teardown()
    typer.echo("demo: torn down (container + volume removed)")
