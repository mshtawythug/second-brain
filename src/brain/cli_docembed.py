"""``brain docembed``: backfill whole-document vectors for the document-leg stage (RM-20).

Thin Typer orchestration over :mod:`brain.doc_embeddings`. ``backfill`` embeds
the documents that have no vector; ``--stale`` also refreshes those whose text
changed or that another embedder wrote. The stage that reads them ships off
(``BRAIN_DOC_LEG``), so nothing here changes a search until the owner turns it on.

Output is counts only, never a title, summary or content; a failure is named by
its class. Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS`
(RM-06), so ``cli.py`` is not edited for it. The embedder is reached through
``brain.cli._build_embedder`` at call time, the patch point every command shares.
"""
from __future__ import annotations

from typing import NoReturn

import typer

from .config import Config, ConfigError
from .db import connect
from .doc_embeddings import (
    DEFAULT_BATCH_SIZE,
    BackfillReport,
    DocEmbeddingError,
    backfill,
    refuse_without_vectors,
)
from .ingest import Embedder
from .queries import analyze_tables

docembed_app = typer.Typer(
    name="docembed",
    help="Whole-document vectors for the document-leg search stage (BRAIN_DOC_LEG).",
    no_args_is_help=True,
)


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=1)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _embedder(cfg: Config) -> Embedder:
    from . import cli as _cli

    embedder: Embedder = _cli._build_embedder(cfg)  # type: ignore[attr-defined]
    return embedder


def _echo_report(report: BackfillReport) -> None:
    typer.echo(f"embedded {_plural(report.embedded, 'document')}")
    if report.skipped_confidential:
        typer.echo(
            f"{_plural(report.skipped_confidential, 'confidential document')} skipped "
            f"(hosted embedder: their text is not sent off-machine)"
        )
    typer.echo(f"{_plural(report.remaining, 'document')} still without a vector")


@docembed_app.command("backfill")
def backfill_cmd(
    stale: bool = typer.Option(
        False,
        "--stale",
        help="Also refresh vectors whose text changed or that another embedder wrote.",
    ),
    limit: int | None = typer.Option(
        None, "--limit", min=1, help="Embed at most this many documents in this run."
    ),
) -> None:
    """Embed each document's title and summary (or the head of its content)."""
    try:
        cfg = Config.load()
    except ConfigError as exc:
        _fail(f"Configuration error: {exc}")
    embedder = _embedder(cfg)
    try:
        refuse_without_vectors(embedder)
        with connect(cfg.database_url) as conn:
            conn.autocommit = True
            report = backfill(
                conn, embedder, stale=stale, limit=limit, batch_size=DEFAULT_BATCH_SIZE
            )
            if report.embedded:
                analyze_tables(conn, ["document_embeddings"])
    except DocEmbeddingError as exc:
        _fail(str(exc))
    _echo_report(report)
    if report.stopped_by is not None:
        _fail(
            f"docembed: the embedder failed ({report.stopped_by}); the run stopped. "
            f"Documents already embedded are kept; re-run to resume."
        )


def register(app: typer.Typer) -> None:
    """Attach the ``docembed`` sub-app to the root app."""
    app.add_typer(docembed_app, name="docembed")
