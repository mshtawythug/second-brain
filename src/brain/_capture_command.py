"""`brain capture` quick-capture inbox sub-app (Plan 09 Phases 1-2)."""
from __future__ import annotations

import sys
from datetime import date
from typing import Any

import psycopg
import typer

from brain import capture as capture_mod
from brain.config import Config
from brain.db import connect
from brain.errors import EnrichmentError, OllamaUnavailable, VaultNoteSyncError
from brain.format import emit_json
from brain.ingest import apply_tags
from brain.queries import (
    DocumentRow,
    count_documents_with_tag,
    list_documents,
    list_existing_tags,
)
from brain.tags import normalize_tags
from brain.vault.note_builder import create_vault_note

# The always-on tag every capture carries until it is reviewed out of the inbox.
_INBOX_TAG = "inbox"
# Default number of inbox items surfaced by ``brain capture review``.
_REVIEW_DEFAULT_LIMIT = 10
# Upper bound on items listed by ``brain capture list`` (the inbox is small by
# design; this caps a runaway listing without paginating).
_INBOX_LIST_LIMIT = 100
# Cap on brand-new (non-vocabulary) tags applied per item in ``--auto`` mode.
_AUTO_MAX_NEW = 1

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
) -> None:
    """Capture a quick note into the inbox (tagged `inbox`).

    Content comes from ``--text`` when provided, otherwise from stdin. Each
    capture authors a vault-tier note under ``capture/`` (visible in the
    Quartz UI without the "Show ingested" toggle). No inline graph sync is
    performed — captures are picked up by the normal ``brain-rebuild`` cycle.

    No inline enrichment is performed — ``documents.summary`` stays ``NULL``
    until ``brain enrich --backfill`` (or a full ``brain-rebuild``) runs, so
    ``brain capture review --auto`` will defer freshly-captured items until
    a summary is present.
    """
    # Phase 2 registers `review` / `list` subcommands under this app; when a
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
    if cfg.vault_path is None:
        typer.secho(
            "vault path is not configured — set BRAIN_VAULT_PATH or run "
            "`brain vault init`",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)

    resolved_title = (
        title.strip()
        if title and title.strip()
        else capture_mod.make_capture_title(
            content, today=date.today(), max_words=cfg.capture_title_words
        )
    )
    # Normalize at the capture boundary so the vault frontmatter tags are
    # queryable by exact tag filters. "inbox" is already canonical; prepending
    # it never collides.
    tags = normalize_tags(["inbox", *tag])

    # Resolve the embedder factory through ``brain.cli`` at call time: importing
    # at module load would create an import cycle (cli imports this module), and
    # the test suite monkeypatches ``brain.cli._build_embedder`` — reading the
    # attribute off the module lazily honors that patch.
    from brain import cli as _cli

    embedder = _cli._build_embedder(cfg)  # type: ignore[attr-defined]
    try:
        with connect(cfg.database_url) as conn:
            conn.autocommit = True
            doc_id = create_vault_note(
                conn,
                cfg=cfg,
                vault_path=cfg.vault_path,
                title=resolved_title,
                body=content,
                tags=tags,
                folder="capture",
                embedder=embedder,
            )
    except VaultNoteSyncError as exc:
        for path, reason in exc.errors:
            typer.secho(f"sync error: {path}: {reason}", fg="red", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✓ captured {doc_id[:8]}  ({resolved_title})  [inbox]")


# ---------------------------------------------------------------------------
# Phase 2 — inbox review + list subcommands.
# ---------------------------------------------------------------------------


def _fetch_summaries(
    conn: psycopg.Connection[Any], ids: list[str]
) -> dict[str, str | None]:
    """Return ``{document_id: summary}`` for ``ids`` in one round-trip.

    ``list_documents`` returns the light projection (``summary`` is always
    ``None``), so the review views fetch summaries separately. A single
    ``= ANY`` query avoids an N+1 over the inbox.
    """
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT id::text, summary FROM documents WHERE id = ANY(%s::uuid[])",
        (ids,),
    ).fetchall()
    return {str(r[0]): r[1] for r in rows}


def _count_inbox(conn: psycopg.Connection[Any]) -> int:
    """Return the number of documents still carrying the ``inbox`` tag.

    Delegates to :func:`brain.queries.count_documents_with_tag` — the same
    shared helper the ``brain doctor`` inbox WARN uses — so the inbox-size
    query lives in exactly one place.
    """
    return count_documents_with_tag(conn, _INBOX_TAG)


def _print_item(
    *, index: int, total: int, row: DocumentRow, summary: str | None
) -> None:
    """Render one inbox item (id + title, optional summary, current tags).

    Never logs the document body — only the title, summary, and tags, per the
    "no document content at INFO" boundary.
    """
    typer.echo(f"[{index}/{total}] {row.id[:8]}  {row.title}")
    if summary:
        typer.echo(f"    summary: {summary}")
    typer.echo(f"    tags: {', '.join(row.tags)}")


def _discard_item(
    conn: psycopg.Connection[Any],
    *,
    cfg: Config,
    row: DocumentRow,
    graph_syncer: Any,
) -> None:
    """Confirm + delete one inbox document (row, chunks, vault mirror).

    Prints the destructive confirmation and only proceeds on an exact ``y`` /
    ``Y``. The ``DELETE FROM documents`` cascades to chunks and to the relational
    graph source-of-truth tables (``graph_entity_mentions``,
    ``graph_edge_contributions``, ``graph_index_state``) via ``ON DELETE CASCADE``
    FKs, so no inline relational cleanup is needed.

    However, *derived* aggregate state (``graph_relationships`` and the AGE mirror)
    is NOT cascade-covered — it requires :meth:`~brain.graph_rag.sync.GraphSyncer.remove`
    to recompute aggregates and GC orphaned vertices/edges. This is only needed
    when the document was previously graph-indexed, so we check
    ``graph_index_state`` BEFORE the DELETE (the watermark row cascade-deletes
    with the document).  Fresh captures (no ``graph_index_state`` row) skip the
    AGE work entirely, paying zero cost on discard. This mirrors the behaviour
    of ``brain rm``, which always calls ``graph_syncer.remove``.

    ``brain.cli._rm_unlink_vault_mirror`` removes the on-disk mirror with the
    same guards/contract as ``brain rm``.
    """
    answer = typer.prompt(
        f'Discard "{row.title}"? This cannot be undone. [y/N]',
        default="n",
        show_default=False,
        prompt_suffix=" ",
    ).strip()
    if answer not in ("y", "Y"):
        typer.echo(f"  kept {row.id[:8]}")
        return
    # Read the graph watermark BEFORE the DELETE — it cascade-deletes with the doc.
    _gis_row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM graph_index_state WHERE document_id = %s)",
        (row.id,),
    ).fetchone()
    had_graph_state: bool = bool(_gis_row[0]) if _gis_row is not None else False
    # vault_path is not in the list projection — fetch it for the unlink.
    vrow = conn.execute(
        "SELECT vault_path FROM documents WHERE id = %s", (row.id,)
    ).fetchone()
    vault_path_rel: str | None = vrow[0] if vrow else None
    # DELETE cascades to chunks and relational graph source-of-truth tables via FK.
    conn.execute("DELETE FROM documents WHERE id = %s", (row.id,))
    # For docs that were graph-indexed, clean up derived aggregate/AGE state.
    # Fresh captures (no graph_index_state row) skip this — zero AGE cost.
    if had_graph_state:
        graph_syncer.remove(conn, row.id)
    # Reuse the shared rm helper for the file-side contract (suffix strings +
    # missing-file tolerance) rather than re-implementing the unlink.
    from brain import cli as _cli

    suffix = _cli._rm_unlink_vault_mirror(cfg=cfg, vault_path_rel=vault_path_rel)
    typer.echo(f"  discarded {row.id[:8]}{suffix}")


def _writeback_routed_tags(
    conn: psycopg.Connection[Any],
    *,
    cfg: Config,
    doc_id: str,
    new_tags: list[str],
) -> None:
    """Sync a routed item's vault frontmatter after ``apply_tags``.

    Captures write a ``capture/<slug>.md`` vault note, so a promote /
    tag / ``--auto`` route that drops ``inbox`` (and adds routing tags) in the
    DB must also rewrite that file's YAML ``tags`` — otherwise the file keeps
    a stale ``inbox`` tag that a later ``brain vault sync`` could reintroduce.
    Reuses :func:`brain.cli._tag_file_writeback` (the same post-``apply_tags``
    writeback ``brain tag`` uses) with ``regenerate_file=False``: a missing
    file degrades to DB-only + a warning rather than being force-regenerated.
    """
    row = conn.execute(
        "SELECT vault_path, kind FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - apply_tags above proved the row exists
        return
    vault_path_rel: str | None = row[0]
    kind: str = row[1]
    from brain import cli as _cli

    _cli._tag_file_writeback(
        conn,
        cfg=cfg,
        vault_path_rel=vault_path_rel,
        kind=kind,
        new_tags=new_tags,
        doc_id=doc_id,
        regenerate_file=False,
    )


def _review_interactive(
    conn: psycopg.Connection[Any],
    rows: list[DocumentRow],
    *,
    cfg: Config,
    graph_syncer: Any,
) -> None:
    """Drive the per-item ``[p]romote [t]ag [d]iscard [s]kip [q]uit`` loop."""
    summaries = _fetch_summaries(conn, [r.id for r in rows])
    for index, row in enumerate(rows, start=1):
        _print_item(
            index=index, total=len(rows), row=row, summary=summaries.get(row.id)
        )
        raw = typer.prompt(
            "[p]romote [t]ag [d]iscard [s]kip [q]uit >",
            default="s",
            show_default=False,
            prompt_suffix=" ",
        ).strip()
        parts = raw.split()
        command = parts[0].lower() if parts else "s"
        args = parts[1:]
        if command == "q":
            break
        if command == "p":
            new_tags = apply_tags(conn, row.id, remove=[_INBOX_TAG])
            _writeback_routed_tags(conn, cfg=cfg, doc_id=row.id, new_tags=new_tags)
            typer.echo(f"  promoted {row.id[:8]}")
        elif command == "t":
            if not args:
                typer.echo("  no tags given; left in inbox")
                continue
            new_tags = apply_tags(conn, row.id, add=args, remove=[_INBOX_TAG])
            _writeback_routed_tags(conn, cfg=cfg, doc_id=row.id, new_tags=new_tags)
            applied = " +".join(normalize_tags(args))
            typer.echo(f"  tagged {row.id[:8]}: +{applied}")
        elif command == "d":
            _discard_item(conn, cfg=cfg, row=row, graph_syncer=graph_syncer)
        elif command == "s":
            typer.echo(f"  skipped {row.id[:8]}")
        else:
            typer.echo(f"  unknown command {command!r}; left in inbox")
    typer.echo(f"{_count_inbox(conn)} item(s) remaining in inbox")


def _review_auto(
    conn: psycopg.Connection[Any],
    rows: list[DocumentRow],
    *,
    cfg: Config,
    enricher: Any,
) -> None:
    """Non-interactive routing: LLM-propose tags per item, remove ``inbox``.

    Items without a summary (the LLM is unreliable on raw bodies) or for which
    enrichment fails are reported and left in the inbox — a single bad item
    never aborts the batch.
    """
    summaries = _fetch_summaries(conn, [r.id for r in rows])
    # Hoist the vocabulary query outside the loop — one round-trip for N items.
    existing_vocab = list_existing_tags(conn)
    for row in rows:
        summary = summaries.get(row.id)
        if not summary:
            typer.echo(
                f"{row.id[:8]}  left in inbox (no summary; run "
                "`brain enrich --backfill`)"
            )
            continue
        try:
            proposal = enricher.propose_tags(
                title=row.title,
                summary=summary,
                existing_vocab=existing_vocab,
                current_tags=[_INBOX_TAG],
                max_new=_AUTO_MAX_NEW,
            )
        except (OllamaUnavailable, EnrichmentError) as exc:
            typer.echo(
                f"{row.id[:8]}  left in inbox (enrichment failed: "
                f"{type(exc).__name__})"
            )
            continue
        accepted = list(proposal.existing) + list(proposal.new[:_AUTO_MAX_NEW])
        if not accepted:
            typer.echo(f"{row.id[:8]}  no tags proposed; left in inbox")
            continue
        new_tags = apply_tags(conn, row.id, add=accepted, remove=[_INBOX_TAG])
        _writeback_routed_tags(conn, cfg=cfg, doc_id=row.id, new_tags=new_tags)
        typer.echo(f"{row.id[:8]}  routed: +{' +'.join(accepted)} (inbox removed)")


@capture_app.command("review")
def capture_review(
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Non-interactive: LLM-route each item out of the inbox via tags.",
    ),
    limit: int = typer.Option(
        _REVIEW_DEFAULT_LIMIT,
        "--limit",
        "-n",
        help="Max inbox items to review this pass.",
    ),
) -> None:
    """Review the quick-capture inbox: promote, tag, or discard each item.

    Interactive by default — per item choose ``[p]romote`` (drop the ``inbox``
    tag), ``[t]ag TAG ...`` (add tags + drop ``inbox``), ``[d]iscard`` (delete
    after an explicit ``y``/``Y`` confirmation), ``[s]kip``, or ``[q]uit``.
    ``--auto`` routes every item non-interactively via the local-Ollama tag
    proposer (items without a summary are left untouched).
    """
    cfg = Config.load()
    from brain import cli as _cli

    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        rows = list_documents(conn, tag=_INBOX_TAG, limit=limit)
        if not rows:
            typer.echo("inbox is empty")
            return
        if auto:
            enricher = _cli._build_enricher(cfg)
            _review_auto(conn, rows, cfg=cfg, enricher=enricher)
            return
        graph_syncer = _cli._build_graph_syncer(cfg)
        _review_interactive(conn, rows, cfg=cfg, graph_syncer=graph_syncer)


@capture_app.command("list")
def capture_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead."),
) -> None:
    """List the documents currently sitting in the quick-capture inbox.

    Mirrors ``brain list`` formatting: a compact ``<id> <kind> <type> <title>``
    table by default, or a JSON array (each item carrying its ``tags``, which
    always include ``inbox``) under ``--json``.
    """
    cfg = Config.load()
    with connect(cfg.database_url) as conn:
        rows = list_documents(conn, tag=_INBOX_TAG, limit=_INBOX_LIST_LIMIT)
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
    if not rows:
        typer.echo("inbox is empty")
        return
    for r in rows:
        kind = r.source_kind or "manual"
        typer.echo(f"{r.id[:8]}  {kind:<8}  {r.content_type:<10}  {r.title}")
