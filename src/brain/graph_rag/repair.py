"""Targeted graph repair: remove deleted documents' leftover graph state.

The core of ``brain graphrag remove <document-id>...`` and its MCP twin
``brain_graphrag_remove``. When the vault watcher's shutdown times out behind a
long reconcile (PR #54), the graph removes still queued behind it are logged by
document id and not applied. The ``documents`` row is gone, and its relational
graph rows went with it by ``ON DELETE CASCADE``. Nothing relational records
the lost remove, but the AGE ``Document`` vertex and its ``MENTIONED_IN`` edges
stay. This module calls :func:`~brain.graph_rag.reconcile.remove_documents`
(W2 in ``docs/plans/2026-10-22-graph-entities-deadlock.md``; it takes the
tenant graph write lock) for exactly those ids. The alternative,
``brain graphrag build --force``, re-extracts every document's concepts.

Rulings (``docs/plans/2026-10-22-graphrag-remove-command.md``):

* **Full UUIDs only.** A deleted document has no row, so a prefix has nothing
  to resolve against. Parsing is strict about the value, and the result is the
  canonical lowercase hyphenated form AGE stores.
* **Deleted documents only.** An id whose ``documents`` row still exists is
  refused, and nothing is removed. Removing a live document's graph state would
  only be re-indexed by its next reconcile; to drop the document itself, use
  ``brain rm``. Every id is checked twice. A pre-check fails fast. Then, under
  the tenant graph write lock and in the same transaction as the removal, a
  re-check catches a document restored in between (a vault file put back with
  its frontmatter id). Either check refuses the whole batch.
* **One batch, one transaction.** The tenant-wide steps (aggregate recompute,
  orphan GC, the ``CO_OCCURS`` rebuild) run once for all the ids, not once per
  id. A failure rolls the whole batch back, and a re-run is safe.
* **The AGE vertex is the only state reported.** A deleted document cannot keep
  relational graph rows: every per-document graph table cascades on the
  ``documents`` delete, by FK. So the report is the ``Document`` vertex count per
  id, and an id whose vertex is already gone is a reported no-op.
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
from .reconcile import ReconcileConfig, remove_documents
from .write_lock import acquire_graph_write_lock

__all__ = [
    "DocumentStillExists",
    "RemoveReport",
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

    @property
    def had_document_vertex(self) -> bool:
        """True iff the deleted document still had its AGE ``Document`` vertex."""
        return self.document_vertices_removed > 0


@dataclass(frozen=True)
class RemoveReport:
    """The whole batch: per-id results plus the batch-wide orphan GC."""

    tenant_id: str
    documents: tuple[RemovedDocument, ...]
    orphans_removed: int

    @property
    def with_document_vertex(self) -> int:
        return sum(1 for item in self.documents if item.had_document_vertex)


def parse_document_ids(raw_ids: Sequence[str]) -> list[str]:
    """Return the canonical form of each id, or raise ``ValueError`` naming the bad ones.

    Each id must be a full document UUID (hex, with or without hyphens). A
    prefix is refused: a deleted document has no row to resolve it against.
    Duplicates (including the same UUID spelled differently) are dropped,
    keeping the first occurrence.
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


def _refuse_live(conn: psycopg.Connection[Any], document_ids: Sequence[str]) -> None:
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


def remove_deleted_documents(
    conn: psycopg.Connection[Any],
    document_ids: Sequence[str],
    *,
    backend: GraphBackend,
    config: ReconcileConfig,
) -> RemoveReport:
    """Remove each deleted document's leftover graph state, in one transaction.

    ``document_ids`` must already be canonical (:func:`parse_document_ids`).
    Raises :class:`DocumentStillExists` if any id still has a ``documents`` row,
    at the pre-check or at the re-check under the lock; nothing is removed then.
    """
    _refuse_live(conn, document_ids)
    with conn.transaction():
        acquire_graph_write_lock(conn, config.tenant_id)
        _refuse_live(conn, document_ids)
        result = remove_documents(conn, document_ids, backend=backend, config=config)
    return RemoveReport(
        tenant_id=result.tenant_id,
        documents=tuple(
            RemovedDocument(document_id=document_id, document_vertices_removed=count)
            for document_id, count in zip(
                document_ids, result.document_vertices_removed, strict=True
            )
        ),
        orphans_removed=result.orphans_removed,
    )
