"""`brain graphrag remove` — remove deleted documents' leftover graph state.

The targeted repair the vault watcher's shutdown WARNING points at when graph
removes were queued and not applied (PR #54). The logic, and the reasons for
its rulings (full UUIDs only, deleted documents only, idempotent, counts only),
live in :mod:`brain.graph_rag.repair`. This module is the CLI wrapper and the
exit-code mapping, which matches the other ``brain graphrag`` admin commands:
``2`` for a bad argument (Typer ``BadParameter``), ``1`` for a refused or
failed run, ``0`` otherwise.

It is its own module because ``cli.py`` is over the 800-line ceiling. ``cli.py``
only calls :func:`register` where the command belongs in ``brain graphrag
--help``. Like every extracted command module, it never imports ``brain.cli`` at
module scope.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from .config import Config
from .db import connect_age

if TYPE_CHECKING:
    from .graph_rag.repair import RemovedDocument


def _describe(removed: RemovedDocument) -> str:
    if not removed.had_graph_state:
        return f"{removed.document_id}: no graph state, nothing to remove"
    vertex = "vertex" if removed.document_vertices_removed == 1 else "vertices"
    return (
        f"{removed.document_id}: removed {removed.document_vertices_removed} document "
        f"{vertex}, {removed.graph_rows_removed} graph row(s)"
    )


def graphrag_remove(
    document_ids: list[str] = typer.Argument(  # noqa: B008 - Typer's declaration idiom
        ...,
        metavar="DOCUMENT_ID...",
        help="Full UUIDs of DELETED documents (a prefix cannot resolve: the row is gone).",
    ),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant to repair (default: BRAIN_GRAPH_TENANT)."
    ),
) -> None:
    """Remove deleted documents' leftover graph state (AGE vertex + edges).

    For the ids a vault-watcher shutdown WARNING names as graph removes not
    applied. Each id must be a full UUID of a document that no longer exists.
    Removes its AGE ``Document`` vertex and ``MENTIONED_IN`` edges, and any graph
    rows left, then GCs now-orphaned entities and refreshes the aggregates.
    Idempotent: an id with no graph state is a reported no-op. Prints ids and
    counts only. Much cheaper than ``brain graphrag build --force``, which
    re-extracts every document.
    """
    # Lazy: the graph package pulls networkx, which every `brain` start would
    # otherwise pay for (tests/test_import_hygiene.py).
    from . import cli as _cli
    from .graph_rag.backends import AgeBackend
    from .graph_rag.repair import (
        DocumentStillExists,
        parse_document_ids,
        remove_deleted_documents,
    )

    try:
        canonical = parse_document_ids(document_ids)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="DOCUMENT_ID") from exc
    cfg = Config.load()
    config = _cli._graphrag_config(cfg, tenant)
    with connect_age(cfg.database_url) as conn:
        conn.autocommit = True
        _cli._require_age_or_exit(conn)
        backend = AgeBackend()
        backend.bootstrap(conn)
        try:
            removed = remove_deleted_documents(
                conn, canonical, backend=backend, config=config
            )
        except DocumentStillExists as exc:
            typer.secho(f"graphrag remove: {exc}", fg="red", err=True)
            raise typer.Exit(code=1) from exc
    for item in removed:
        typer.echo(_describe(item))
    with_state = sum(1 for item in removed if item.had_graph_state)
    orphans = sum(item.orphans_removed for item in removed)
    typer.echo(
        f"graphrag remove: {with_state} of {len(removed)} id(s) had graph state, "
        f"{orphans} orphan(s) removed (tenant {config.tenant_id!r})"
    )


def register(graphrag_app: typer.Typer) -> None:
    """Attach ``brain graphrag remove`` to the ``graphrag`` sub-app."""
    graphrag_app.command("remove")(graphrag_remove)
