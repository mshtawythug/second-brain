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

from ..sensitivity import CONFIDENTIAL
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


#: Appended to the metadata predicate when the caller is outside the trust
#: boundary. Frozen into the string rather than bound as a ``%s`` parameter for
#: the reason :mod:`brain.vault.graph` gives: the fragment sits in a statement
#: whose only positional parameter is the id array, and threading a second one
#: through conditionally is how the wrong value gets bound. ``CONFIDENTIAL`` is
#: a module constant, never caller input, so there is nothing to inject.
_NOT_CONFIDENTIAL = f"AND d.sensitivity <> '{CONFIDENTIAL}'"


def _build_doc_results(
    conn: psycopg.Connection[Any],
    query: str,
    ranked: list[tuple[str, float]],
    *,
    exclude_confidential: bool = False,
) -> list[SearchResult]:
    """Shape ranked documents into ``SearchResult``s, reusing the snippet path.

    Reuses :data:`brain.search.SearchResult` (spec §4 D8 — graph docs may reuse
    the search hit shape), :data:`brain.search.SNIPPET_LENGTH`, and
    :func:`brain.search._build_tsquery` so snippet selection matches hybrid
    search rather than reinventing it: per document, the best chunk by ``ts_rank``
    for the query, falling back to the leading chunk (lowest ``chunk_index``)
    when the query matches nothing. ``score`` carries the *graph* document score
    (not an RRF score). Document order preserves the graph ranking.

    ``exclude_confidential`` is the F6 egress gate for the WHOLE graph surface.
    This function is the single funnel through which all three graph modes
    (local / themes / global, and fuse's graph leg) turn ranked ids into
    ``SearchResult``s, and ``snippet`` is raw ``chunks.content`` — so before
    this parameter existed, ``brain_graphrag_search`` handed a hosted model the
    confidential BODY TEXT that ``brain_search`` had been carefully filtering
    for. The gate EXCLUDES the row rather than blanking its snippet: a redacted
    hit still proves the document exists and matched, which reconstructs the
    body a query at a time (the membership oracle ``_confidential_lens``
    describes).

    It DEFAULTS FALSE — include — matching :mod:`brain.vault.graph` rather than
    the MCP layer's ``include_confidential``. The CLI sits inside the trust
    boundary and must keep seeing its own documents; the MCP layer is the
    boundary and passes ``exclude_confidential=not include_confidential``.
    Opposite name AND opposite default: inverting that bridge flips the gate
    while every test still passes, because the permissive direction only ever
    ADDS rows. Both directions are pinned in
    ``tests/test_graph_confidential_egress.py``.
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
        "SELECT d.id::text, d.title, d.content_type, d.tags, s.kind "
        "FROM documents d LEFT JOIN sources s ON s.id = d.source_id "
        "WHERE d.id = ANY(%s) "
        f"{_NOT_CONFIDENTIAL if exclude_confidential else ''}",
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
            # Two ways to land here, and the gate depends on the second:
            #  1. A mention referenced a document with no surviving
            #     ``documents`` row. Defensive — ``ON DELETE CASCADE``
            #     normally clears mentions first.
            #  2. ``exclude_confidential`` filtered the row out of ``meta``
            #     above. Dropping it HERE is what makes the gate an exclusion
            #     rather than a redaction: the document never enters ``results``
            #     at all, so neither its snippet nor the fact that it matched
            #     reaches the caller.
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
            )
        )
    return results
