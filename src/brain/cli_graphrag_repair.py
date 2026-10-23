"""`brain graphrag remove` — remove deleted documents' leftover graph state.

The targeted repair the vault watcher's shutdown messages point at
(``LOST_REMOVES_REPAIR`` in :mod:`brain.vault.graph_lane`) when graph removes
were queued and not applied (PR #54). The logic, and the reasons for its
rulings (full UUIDs only, deleted documents only, one batch, counts only), live
in :mod:`brain.graph_rag.repair`. This module is the CLI wrapper and the
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
    if not removed.had_document_vertex:
        return f"{removed.document_id}: no document vertex, nothing to remove"
    vertex = "vertex" if removed.document_vertices_removed == 1 else "vertices"
    return f"{removed.document_id}: removed {removed.document_vertices_removed} document {vertex}"


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

    For the ids a vault-watcher shutdown message names as graph removes not
    applied. Each id must be a full UUID of a document that no longer exists.
    Removes its AGE ``Document`` vertex and ``MENTIONED_IN`` edges, then, once
    for the batch, GCs now-orphaned entities and refreshes the aggregates (it
    holds the tenant graph lock for that pass). Idempotent: an id whose vertex is
    already gone is a reported no-op. Prints ids and counts only. Much cheaper
    than ``brain graphrag build --force``, which re-extracts every document.
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
            report = remove_deleted_documents(
                conn, canonical, backend=backend, config=config
            )
        except DocumentStillExists as exc:
            typer.secho(f"graphrag remove: {exc}", fg="red", err=True)
            raise typer.Exit(code=1) from exc
    for item in report.documents:
        typer.echo(_describe(item))
    typer.echo(
        f"graphrag remove: {report.with_document_vertex} of {len(report.documents)} "
        f"id(s) had a document vertex, {report.orphans_removed} orphan(s) removed "
        f"(tenant {report.tenant_id!r})"
    )


def register(graphrag_app: typer.Typer) -> None:
    """Attach ``brain graphrag remove`` to the ``graphrag`` sub-app."""
    graphrag_app.command("remove")(graphrag_remove)
