"""Targeted graph repair: remove deleted documents' leftover graph state.

The core of ``brain graphrag remove <document-id>...`` and its MCP twin
``brain_graphrag_remove``. When the vault watcher's shutdown times out behind a
long reconcile (PR #54), the graph removes still queued behind it are logged by
document id and not applied. The ``documents`` row is gone, and its relational
graph rows went with it by ``ON DELETE CASCADE``. Nothing relational records
the lost remove, but the AGE ``Document`` vertex and its ``MENTIONED_IN`` edges
stay. This module calls :func:`~brain.graph_rag.reconcile.remove_document` (W2
in ``docs/plans/2026-10-22-graph-entities-deadlock.md``; it takes the tenant
graph write lock) for exactly those ids. The alternative,
``brain graphrag build --force``, re-extracts every document's concepts.

Rulings:

* **Full UUIDs only.** A deleted document has no row, so a prefix has nothing
  to resolve against. Parsing is strict about the value, and the result is the
  canonical lowercase hyphenated form AGE stores.
* **Deleted documents only.** An id whose ``documents`` row still exists is
  refused, and nothing is removed. Removing a live document's graph state would
  only be re-indexed by its next reconcile; to drop the document itself, use
  ``brain rm``. Every id is checked before any is removed.
* **Idempotent.** An id with no graph state left is a reported no-op.
  ``remove_document`` still recomputes the tenant aggregates and the
  ``CO_OCCURS`` mirror for it, which converges to the same graph.
* **Counts only.** Results carry ids and counts, never a title or an entity
  name.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from ..errors import GraphReconcileError
from .backends.base import GraphBackend
from .reconcile import ReconcileConfig, remove_document

__all__ = [
    "DocumentStillExists",
    "RemovedDocument",
    "parse_document_ids",
    "remove_deleted_documents",
]


class DocumentStillExists(GraphReconcileError):
    """A ``graph remove`` id names a document that has not been deleted."""


@dataclass(frozen=True)
class RemovedDocument:
    """What one ``graph remove`` found and removed for a document id."""

    document_id: str
    document_vertices_removed: int
    graph_rows_removed: int
    orphans_removed: int

    @property
    def had_graph_state(self) -> bool:
        """True iff the document had an AGE vertex or relational graph rows left."""
        return self.document_vertices_removed > 0 or self.graph_rows_removed > 0


def parse_document_ids(raw_ids: Sequence[str]) -> list[str]:
    """Return the canonical form of each id, or raise ``ValueError`` naming the bad ones.

    Each id must be a full document UUID (hex, with or without hyphens). A
    prefix is refused: a deleted document has no row to resolve it against.
    Duplicates are dropped, keeping the first occurrence.
    """
    canonical: list[str] = []
    bad: list[str] = []
    for raw in raw_ids:
        try:
            value = str(uuid.UUID(raw.strip()))
        except ValueError:
            bad.append(raw)
            continue
        if value not in canonical:
            canonical.append(value)
    if bad:
        raise ValueError(
            "graph remove needs a full document UUID for each id; a deleted "
            "document has no row to resolve a prefix against. Not a full UUID: "
            + ", ".join(repr(value) for value in bad)
        )
    if not canonical:
        raise ValueError("graph remove needs at least one full document UUID")
    return canonical


def remove_deleted_documents(
    conn: psycopg.Connection[Any],
    document_ids: Sequence[str],
    *,
    backend: GraphBackend,
    config: ReconcileConfig,
) -> list[RemovedDocument]:
    """Remove each deleted document's leftover graph state, in the given order.

    ``document_ids`` must already be canonical (:func:`parse_document_ids`).
    Raises :class:`DocumentStillExists`, before removing anything, if any id
    still has a ``documents`` row. Each removal is its own ``remove_document``
    transaction, so one failure leaves the earlier ids removed and is safe to
    re-run.
    """
    live = conn.execute(
        "SELECT id::text FROM documents WHERE id = ANY(%s::uuid[]) ORDER BY id",
        (list(document_ids),),
    ).fetchall()
    if live:
        raise DocumentStillExists(
            "graph remove is for deleted documents, and each of these still exists: "
            + ", ".join(str(row[0]) for row in live)
            + ". Delete one with `brain rm`, or rebuild its graph with "
            "`brain graphrag build`."
        )
    removed: list[RemovedDocument] = []
    for document_id in document_ids:
        result = remove_document(conn, document_id, backend=backend, config=config)
        removed.append(
            RemovedDocument(
                document_id=document_id,
                document_vertices_removed=result.document_vertices_removed,
                graph_rows_removed=result.graph_rows_removed,
                orphans_removed=result.orphans_removed,
            )
        )
    return removed
