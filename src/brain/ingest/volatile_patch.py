"""Metadata-only refreshes for sourced documents: the stored row and its volatile-key patch.

Protocol point 3 of the new-source design: a field that changes without the
body changing (a series' occurrences, a pull request's state, a note's folder)
lives in ``metadata``, and a re-run whose body is unchanged applies it with
``update_document(metadata_patch=…)``, which neither re-chunks nor embeds.
``brain ingest-calendar`` (RM-30), ``ingest-github`` (RM-31) and
``ingest-apple-notes`` (RM-36) each did this with the same two steps, so the
steps live here once.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import psycopg

from . import update_document

if TYPE_CHECKING:
    from ..graph_rag.sync import GraphSyncer


def stored_document(
    conn: psycopg.Connection[Any], kind: str, external_id: str
) -> tuple[str, dict[str, Any]] | None:
    """``(document id, metadata)`` of the document stored for ``(kind, external_id)``."""
    row = conn.execute(
        "SELECT d.id::text, d.metadata FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE s.kind = %s AND s.external_id = %s",
        (kind, external_id),
    ).fetchone()
    return None if row is None else (str(row[0]), row[1] or {})


def refresh_volatile(
    conn: psycopg.Connection[Any],
    *,
    document_id: str,
    stored: Mapping[str, Any],
    metadata: Mapping[str, Any],
    keys: Iterable[str],
    vault_root: Path | None,
    graph_syncer: GraphSyncer | None,
) -> Literal["refreshed", "unchanged"]:
    """Apply the ``keys`` of ``metadata`` that differ from ``stored``.

    A key absent from ``metadata`` is left alone. Metadata only: no new content
    and no embedder, so no re-chunk and no embed. The graph syncer goes along,
    so a change the graph indexes (a series' participants) re-indexes it.
    """
    patch = {
        key: metadata[key]
        for key in keys
        if key in metadata and stored.get(key) != metadata[key]
    }
    if not patch:
        return "unchanged"
    update_document(
        conn,
        document_id=document_id,
        metadata_patch=patch,
        vault_root=vault_root,
        graph_syncer=graph_syncer,
    )
    return "refreshed"
