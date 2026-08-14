"""Shared retrieval helpers for the local + themes graph paths (G2 file-size split).

Extracted from :mod:`brain.graph_rag.retrieve` (the G2 wave-boundary file-size
split, mirroring the G1 :mod:`brain.graph_rag.aggregates` / G2-c
:mod:`brain.graph_rag.relational` extractions) so the local-path/dispatch module
(:mod:`brain.graph_rag.retrieve`) and the themes module
(:mod:`brain.graph_rag.themes`) share ONE copy of the entity-row mapping +
ranked-document-to-:class:`~brain.search.SearchResult` snippet shaping. This is a
pure move — no behavior change.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg

from .schema import GraphEntity

if TYPE_CHECKING:
    from ..search import SearchResult


def _fetch_entities(
    conn: psycopg.Connection[Any], tenant_id: str, entity_ids: list[str]
) -> list[GraphEntity]:
    """Load ``graph_entities`` rows for the reached entity ids (tenant-scoped)."""
    if not entity_ids:
        return []
    rows = conn.execute(
        "SELECT id::text, entity_type, name, canonical_key, description, doc_count "
        "FROM graph_entities WHERE tenant_id = %s AND id = ANY(%s)",
        (tenant_id, entity_ids),
    ).fetchall()
    return [_row_to_entity(row, tenant_id) for row in rows]


def _row_to_entity(row: tuple[Any, ...], tenant_id: str) -> GraphEntity:
    """Map a ``(id, entity_type, name, canonical_key, description, doc_count)``
    row to a :class:`GraphEntity` value object."""
    return GraphEntity(
        id=str(row[0]),
        entity_type=str(row[1]),
        name=str(row[2]),
        canonical_key=str(row[3]),
        tenant_id=tenant_id,
        description=row[4],
        doc_count=int(row[5]),
    )


def _build_doc_results(
    conn: psycopg.Connection[Any],
    query: str,
    ranked: list[tuple[str, float]],
) -> list[SearchResult]:
    """Shape ranked documents into ``SearchResult``s, reusing the snippet path.

    Reuses :data:`brain.search.SearchResult` (spec §4 D8 — graph docs may reuse
    the search hit shape), :data:`brain.search.SNIPPET_LENGTH`, and
    :func:`brain.search._build_tsquery` so snippet selection matches hybrid
    search rather than reinventing it: per document, the best chunk by ``ts_rank``
    for the query, falling back to the leading chunk (lowest ``chunk_index``)
    when the query matches nothing. ``score`` carries the *graph* document score
    (not an RRF score). Document order preserves the graph ranking.

    ``summary`` is populated from ``documents.summary`` exactly as the hybrid
    path does, so a graph-retrieved hit and a hybrid-retrieved hit for the SAME
    document are interchangeable to downstream projections (notably
    ``search_results_brief_json``, which chooses between summary and snippet
    per result). Leaving it at its ``None`` default would have made that choice
    depend on which leg retrieved the document under ``--mode fuse``.
    """
    # Late import keeps :mod:`brain.graph_rag` import-cheap and free of any
    # import cycle with the ingest pipeline that :mod:`brain.search` pulls in
    # (mirrors the TYPE_CHECKING-only SearchResult reference in schema.py).
    from ..search import SNIPPET_LENGTH, SearchResult, _build_tsquery

    if not ranked:
        return []
    doc_ids = [doc_id for doc_id, _ in ranked]
    scores = dict(ranked)

    meta_rows = conn.execute(
        # ``d.summary`` is appended LAST (index 5) so the positional indices the
        # construction site below already depends on keep their meaning. It is
        # selected here — rather than left to the ``SearchResult`` default —
        # because ``fuse.py`` picks ONE carrier per document from whichever leg
        # retrieved it; omitting it would make "does this hit have a summary?"
        # depend on the retrieval leg instead of on the document.
        "SELECT d.id::text, d.title, d.content_type, d.tags, s.kind, d.summary "
        "FROM documents d LEFT JOIN sources s ON s.id = d.source_id "
        "WHERE d.id = ANY(%s)",
        (doc_ids,),
    ).fetchall()
    meta = {str(row[0]): row for row in meta_rows}

    tsquery = _build_tsquery(conn, query)
    snippet_rows = conn.execute(
        "SELECT DISTINCT ON (c.document_id) c.document_id::text, c.content "
        "FROM chunks c WHERE c.document_id = ANY(%s) "
        "ORDER BY c.document_id, "
        # %s::tsquery — _build_tsquery returns LEXEMES; re-parsing them
        # with the english config double-stems and stops matching the
        # stored tsv. See brain.search._build_tsquery.
        "ts_rank(c.tsv, %s::tsquery) DESC, c.chunk_index ASC",
        (doc_ids, tsquery),
    ).fetchall()
    snippet_by_doc = {str(row[0]): str(row[1]) for row in snippet_rows}

    results: list[SearchResult] = []
    for doc_id in doc_ids:  # preserve graph ranking order
        row = meta.get(doc_id)
        if row is None:
            # A mention referenced a document with no surviving ``documents`` row.
            # Defensive: ``ON DELETE CASCADE`` normally clears mentions first.
            continue
        snippet = snippet_by_doc.get(doc_id, "")[:SNIPPET_LENGTH]
        results.append(
            SearchResult(
                document_id=doc_id,
                title=row[1],
                content_type=row[2],
                tags=list(row[3] or []),
                source_kind=row[4],
                snippet=snippet,
                score=scores[doc_id],
                summary=row[5],
            )
        )
    return results
