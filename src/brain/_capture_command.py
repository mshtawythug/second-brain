"""`brain capture` quick-capture inbox sub-app (Plan 09 Phase 1)."""
from __future__ import annotations

import sys
from datetime import date

import typer

from brain import capture as capture_mod
from brain.config import Config
from brain.db import connect
from brain.ingest import ingest_document

capture_app = typer.Typer(
    name="capture",
    help=(
        "Quick-capture inbox: jot a thought into the brain, tagged `inbox`. "
        "Pipe text on stdin or pass --text; review later with the inbox tools."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)


@capture_app.callback(invoke_without_command=True)
def capture(
    ctx: typer.Context,
    title: str | None = typer.Option(
        None, "--title", help="Document title. Defaults to a date-stamped auto-title."
    ),
    text: str | None = typer.Option(
        None, "--text", help="Capture content inline instead of piping stdin."
    ),
    tag: list[str] = typer.Option(
        [], "--tag", "-t", help="Extra tag(s) applied alongside the always-on `inbox` tag."
    ),
    content_type: str | None = typer.Option(
        None,
        "--content-type",
        help="Content type label. Defaults to BRAIN_CAPTURE_CONTENT_TYPE (note).",
    ),
    no_enrich: bool = typer.Option(
        False, "--no-enrich", help="Skip the local-Ollama auto-summary post-ingest hook."
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-capture even if identical content was already captured."
    ),
) -> None:
    """Capture a quick note into the inbox (tagged `inbox`).

    Content comes from ``--text`` when provided, otherwise from stdin. Dedup is
    by content hash (no source id): re-capturing identical text is a no-op
    unless ``--force`` is passed. The auto-summary enrichment hook degrades to a
    warning (summary stays NULL) when Ollama is unavailable.
    """
    # Phase 2 will register `review` / `list` subcommands under this app; when a
    # subcommand is invoked the callback must defer to it instead of capturing.
    if ctx.invoked_subcommand is not None:
        return

    content = text if text is not None else sys.stdin.read()
    if not content.strip():
        typer.secho(
            "capture content is empty (pass --text or pipe content on stdin)",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg = Config.load()
    resolved_content_type = content_type or cfg.capture_content_type
    resolved_title = (
        title
        if title and title.strip()
        else capture_mod.make_capture_title(
            content, today=date.today(), max_words=cfg.capture_title_words
        )
    )
    tags = ["inbox", *tag]
    doc = capture_mod.make_capture_doc(
        content, resolved_title, resolved_content_type, tags
    )

    # Resolve the embedder/enricher/graph-syncer factories through ``brain.cli``
    # at call time: importing them at module load would create an import cycle
    # (cli imports this module), and the test suite monkeypatches
    # ``brain.cli._build_embedder`` — reading the attribute off the module
    # lazily honors that patch.
    from brain import cli as _cli

    # ``_build_embedder`` is re-imported into ``brain.cli`` from
    # ``brain.vault.note_builder`` (not defined there), so mypy's
    # implicit-reexport check flags cross-module access; the patch point is
    # nonetheless ``brain.cli._build_embedder`` (see conftest ``patch_embedder``).
    embedder = _cli._build_embedder(cfg)  # type: ignore[attr-defined]
    enricher = None if no_enrich else _cli._build_enricher(cfg)
    graph_syncer = _cli._build_graph_syncer(cfg)
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        result = ingest_document(
            conn,
            embedder=embedder,
            doc=doc,
            source_kind="manual",
            tags=tags,
            force=force,
            vault_root=cfg.vault_path,
            enricher=enricher,
            enrich=not no_enrich,
            enrich_min_tokens=cfg.enrich_min_tokens,
            graph_syncer=graph_syncer,
        )

    short_id = (result.document_id or "")[:8]
    if result.created:
        typer.echo(f"✓ captured {short_id}  ({resolved_title})  [inbox]")
    else:
        typer.echo(f"⟳ already captured ({short_id})")
