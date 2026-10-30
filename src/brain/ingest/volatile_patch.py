"""Metadata-only refreshes for sourced documents: the stored row and its volatile-key patch.

Protocol point 3 of the new-source design: a field that changes without the
body changing (a series' occurrences, a pull request's state, a note's folder)
lives in ``metadata``, and a re-run whose body is unchanged applies it with
``update_document(metadata_patch=…)``, which neither re-chunks nor embeds.
``brain ingest-calendar`` (RM-30), ``ingest-github`` (RM-31) and
``ingest-apple-notes`` (RM-36) each did this with the same two steps, so the
steps live here once.

:func:`settle` is the one place that turns an ``IngestResult`` into an outcome
(BL-34). For a NEW ``(kind, external_id)`` with no row of its own,
``ingest_document`` falls back to its stdin content-hash match and can return
ANOTHER document's id when the bodies are identical (the same meeting on two
calendars, two copies of one template note). That row is not the item's, so it
is never patched and the item is reported ``same_body``; ``refreshed`` and
``unchanged`` only ever describe the item's own row. A result with no document
(nothing to chunk) is ``empty``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import psycopg

from . import IngestResult, update_document

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


#: What :func:`settle` makes of one ``ingest_document`` call.
Settled = Literal["ingested", "updated", "refreshed", "unchanged", "empty", "same_body"]


def settle(
    conn: psycopg.Connection[Any],
    result: IngestResult,
    *,
    stored: tuple[str, Mapping[str, Any]] | None,
    metadata: Mapping[str, Any],
    keys: Iterable[str],
    vault_root: Path | None,
    graph_syncer: GraphSyncer | None,
) -> Settled:
    """The outcome of ``result`` for an item whose own row was ``stored`` before the call.

    ``stored`` is :func:`stored_document` for the item's ``(kind, external_id)``,
    read BEFORE ``ingest_document``. The volatile ``keys`` of ``metadata`` are
    applied only to that row (:func:`refresh_volatile`); a returned id that is not
    that row's is ``same_body``, and nothing is written to it.
    """
    if result.created or result.body_changed:
        return "ingested" if result.created else "updated"
    if result.document_id is None:
        return "empty"
    if stored is None or result.document_id != stored[0]:
        return "same_body"
    return refresh_volatile(
        conn,
        document_id=result.document_id,
        stored=stored[1],
        metadata=metadata,
        keys=keys,
        vault_root=vault_root,
        graph_syncer=graph_syncer,
    )


def rare_outcomes(counts: Mapping[str, int], noun: str) -> str:
    """``", N same body as another <noun>, N empty"``, each part only when non-zero.

    For a CLI's summary line, so a run with neither prints exactly what it printed
    before BL-34 (decision D1).
    """
    same, empty = counts.get("same_body", 0), counts.get("empty", 0)
    return (f", {same} same body as another {noun}" if same else "") + (
        f", {empty} empty" if empty else ""
    )
