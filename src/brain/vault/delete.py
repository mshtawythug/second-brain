"""The single document-delete path shared by every surface (F8).

Before this module ``brain rm`` was the only way to delete a document, and
its logic — capture title + ``vault_path``, ``DELETE FROM documents``, drop
the doc from the people graph, unlink the on-disk vault mirror — lived
inline in the CLI command body. The MCP server had no delete tool at all,
and ``brain ui`` needs the same four steps in the same order.

Duplicating that sequence three times is how the "row deleted but the
mirror survives, so the next ``brain vault sync`` resurrects it" bug gets
reintroduced. So it lives here exactly once, in a module that imports no
CLI and no Typer and can therefore be called from a request handler.

Ordering is load-bearing and matches the pre-extraction CLI body:

1. Read ``title`` + ``vault_path`` **before** the delete — the row is gone
   afterwards and both are needed for the report and the unlink.
2. ``DELETE FROM documents`` — chunks, links and the relational graph
   source rows cascade (migration 012 FKs).
3. :meth:`DocumentGraphSyncer.remove` — best-effort, never raises; GCs
   orphaned person vertices + edges left behind by the cascade.
4. Unlink the mirror. A missing file is tolerated (debug log only); the DB
   row is already gone and raising here would strand the caller with a
   half-applied delete it cannot retry.

The connection is expected to be in autocommit mode, matching every
existing caller.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import psycopg

logger = logging.getLogger(__name__)

# The three user-facing suffixes appended to ``brain rm``'s ``removed <id>``
# line. Their exact text is part of the CLI contract and is asserted by
# ``tests/test_cli_rm.py`` — do not reword without updating that test.
_SUFFIX_DB_ONLY = " (db only)"
_SUFFIX_ALREADY_GONE = " (db only, file already gone)"

# ``DeleteReport.mirror_action`` values — the machine-readable counterpart to
# the display suffix, for JSON-shaped consumers (MCP, ``brain ui``).
MIRROR_DB_ONLY = "db_only"
MIRROR_ABSENT = "absent"
MIRROR_UNLINKED = "unlinked"


class DocumentGraphSyncer(Protocol):
    """The one :class:`~brain.graph_rag.sync.GraphSyncer` method used here.

    Narrowed to a Protocol so this module does not import the ``graph_rag``
    package (which pulls networkx) and so tests can pass a trivial fake.
    """

    def remove(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        """Drop ``document_id`` from the people graph. Must not raise."""
        ...


@dataclass(frozen=True)
class DeleteTarget:
    """What a delete *would* affect, read without deleting anything.

    Exists so a confirmation surface — the CLI prompt, and the MCP tools'
    ``confirm=False`` refusal, which must name exactly what it declined to
    destroy — can describe the blast radius using the same SELECT the
    delete itself uses.
    """

    document_id: str
    title: str
    vault_path: str | None


@dataclass(frozen=True)
class DeleteReport:
    """Outcome of :func:`delete_document`."""

    document_id: str
    title: str
    vault_path: str | None
    mirror_action: str
    """One of :data:`MIRROR_DB_ONLY` / :data:`MIRROR_ABSENT` / :data:`MIRROR_UNLINKED`."""
    suffix: str
    """The exact suffix the CLI appends to its ``removed <id>`` line."""


def describe_delete_target(
    conn: psycopg.Connection[Any], *, document_id: str
) -> DeleteTarget | None:
    """Read the title + ``vault_path`` of ``document_id`` without deleting.

    Returns ``None`` when no such row exists, so callers that already
    resolved an id prefix can still fail cleanly on a row deleted between
    resolution and this call.
    """
    row = conn.execute(
        "SELECT title, vault_path FROM documents WHERE id=%s", (document_id,)
    ).fetchone()
    if row is None:
        return None
    return DeleteTarget(document_id=document_id, title=row[0], vault_path=row[1])


def unlink_vault_mirror(
    *, vault_root: Path, vault_path_rel: str | None
) -> tuple[str, str]:
    """Remove the on-disk vault mirror. Return ``(mirror_action, suffix)``.

    Never raises on a missing file — the DB row is already gone by the time
    this runs, and a mirror that vanished (manual ``rm``, a previous partial
    delete) is a no-op the user should be *told* about, not crashed over.

    - ``vault_path_rel`` is ``None`` → ``("db_only", " (db only)")``. Raw
      ``ingest-stdin`` rows that never got a vault export land here.
    - File present → unlinked → ``("unlinked", " (file: <vault_path>)")``.
    - File already absent → ``("absent", " (db only, file already gone)")``.
    """
    if vault_path_rel is None:
        return MIRROR_DB_ONLY, _SUFFIX_DB_ONLY
    abs_path = vault_root / vault_path_rel
    if abs_path.exists():
        abs_path.unlink()
        logger.debug("delete: unlinked vault mirror %s", abs_path)
        return MIRROR_UNLINKED, f" (file: {vault_path_rel})"
    logger.debug(
        "delete: vault mirror already gone at %s (skipping unlink)", abs_path
    )
    return MIRROR_ABSENT, _SUFFIX_ALREADY_GONE


def delete_document(
    conn: psycopg.Connection[Any],
    *,
    document_id: str,
    vault_root: Path,
    graph_syncer: DocumentGraphSyncer | None,
) -> DeleteReport:
    """Delete a document row, its graph presence, and its vault mirror.

    ``document_id`` must be a fully-resolved id (callers resolve prefixes
    first). Raises :class:`ValueError` when the row does not exist, so a
    caller that skipped resolution gets a clean, typed failure rather than
    a silent no-op that reports success.

    ``graph_syncer`` may be ``None`` — matching
    :func:`brain.ingest.update_document`'s contract — which skips the graph
    step. The row delete and the mirror unlink still happen.

    The four steps, and why their order matters, are documented at the top
    of this module.
    """
    target = describe_delete_target(conn, document_id=document_id)
    if target is None:
        raise ValueError(f"document not found: {document_id}")

    conn.execute("DELETE FROM documents WHERE id=%s", (document_id,))
    # Post-DELETE on the same (autocommit) connection: the cascade has
    # already removed the relational graph source rows, and ``remove`` is
    # robust whether they are gone or not — it then GCs orphaned person
    # vertices + edges. Best-effort by contract; it never raises.
    if graph_syncer is not None:
        graph_syncer.remove(conn, document_id)

    mirror_action, suffix = unlink_vault_mirror(
        vault_root=vault_root, vault_path_rel=target.vault_path
    )
    return DeleteReport(
        document_id=document_id,
        title=target.title,
        vault_path=target.vault_path,
        mirror_action=mirror_action,
        suffix=suffix,
    )
