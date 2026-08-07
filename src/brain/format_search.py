"""Renderers and JSON projections for search metadata (counts, timings, facets).

Lives outside :mod:`brain.format` because that module is already at 783 of the
800-line ceiling.

The string formatter and the JSON projections are deliberately SEPARATE
functions over the same values, not one function with a ``json=`` flag.
:func:`search_meta_line` targets a terminal; :func:`search_meta_json` and
:func:`search_envelope_json` are the programmatic surface that MCP and
``brain ui`` consume. Collapsing them would make the phase-split timing
reachable only by parsing a human string.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table

from .facets import SearchFacets
from .search import SearchDiagnostics, SearchResult

#: Printed instead of the facet panel when nothing matched.
NO_FACETS_MESSAGE = "no facets (0 documents matched)"


def search_results_json(results: list[SearchResult]) -> list[dict[str, Any]]:
    """Project results into the FROZEN seven-key public shape.

    The single construction site for that shape. ``brain search --json``, the
    ``--meta`` envelope's ``results`` array and MCP ``brain_search`` all call
    this, so the envelope's entries cannot drift from the bare list's.
    ``SearchResult.explain`` is deliberately NOT included — ``brain explain``
    adds it as an eighth key on top of this base.
    """
    return [
        {
            "id": r.document_id,
            "title": r.title,
            "source_kind": r.source_kind,
            "snippet": r.snippet,
            "score": r.score,
            "content_type": r.content_type,
            "tags": r.tags,
        }
        for r in results
    ]


def _is_fts_only(diag: SearchDiagnostics) -> bool:
    """Derive fts-only mode from the absence of an embed phase.

    ``hybrid_search`` writes ``embed_ms`` if and only if it ran the vector
    leg's embed call, so this needs no extra diagnostics field to stay true
    for both ``--fts-only`` and the auto-degrading ``NullEmbedder``.
    """
    return diag.embed_ms is None


def search_meta_line(diag: SearchDiagnostics, *, returned: int) -> str:
    """One-line human footer: match counts and the latency phase split.

    Rendered as ``544 matched · 3 shown · embed 5820ms · sql 214ms · total
    6042ms``. A total of ``None`` prints ``? matched`` — the count was asked
    for and failed, and reporting ``0 matched`` beside a screen of results
    would be a lie. Under fts-only the embed segment is omitted entirely
    rather than printed as ``embed 0ms``, which would falsely imply a free
    embed. A warm LRU hit is marked ``(cached)`` so the user is not left
    thinking a 40x speedup is reproducible from a fresh shell.
    """
    total = "?" if diag.total_documents is None else str(diag.total_documents)
    parts = [f"{total} matched", f"{returned} shown"]
    if diag.embed_ms is not None:
        cached = " (cached)" if diag.embed_cached else ""
        parts.append(f"embed {round(diag.embed_ms)}ms{cached}")
    if diag.sql_ms is not None:
        parts.append(f"sql {round(diag.sql_ms)}ms")
    if diag.facets_ms is not None:
        parts.append(f"facets {round(diag.facets_ms)}ms")
    if diag.total_ms is not None:
        parts.append(f"total {round(diag.total_ms)}ms")
    return " · ".join(parts)


def facets_renderable(facets: SearchFacets) -> Table:
    """Render the three facet groups side by side.

    Counts are right-justified in their own columns so the numbers line up
    across rows of differing label width. The three groups have independent
    lengths; short ones pad with blanks. The ``(+N more)`` remainder lands in
    the tag column when tags were truncated.
    """
    table = Table(box=None, pad_edge=False)
    table.add_column("Source")
    table.add_column("", justify="right")
    table.add_column("Content type")
    table.add_column("", justify="right")
    table.add_column("Tags")
    table.add_column("", justify="right")

    source_cells = [(b.value, str(b.count)) for b in facets.source]
    type_cells = [(b.value, str(b.count)) for b in facets.content_type]
    tag_cells = [(b.value, str(b.count)) for b in facets.tag]
    if facets.tag_truncated > 0:
        tag_cells.append((f"(+{facets.tag_truncated} more)", ""))

    for i in range(max(len(source_cells), len(type_cells), len(tag_cells))):
        src = source_cells[i] if i < len(source_cells) else ("", "")
        ctype = type_cells[i] if i < len(type_cells) else ("", "")
        tag = tag_cells[i] if i < len(tag_cells) else ("", "")
        table.add_row(src[0], src[1], ctype[0], ctype[1], tag[0], tag[1])
    return table


def facets_json(facets: SearchFacets) -> dict[str, Any]:
    """Project facets for the ``--meta`` envelope and the MCP dict.

    ``total_documents`` is omitted deliberately — both consumers already carry
    it as a top-level key, and duplicating it would invite the two copies to
    disagree.
    """
    return {
        "source": [{"value": b.value, "count": b.count} for b in facets.source],
        "content_type": [
            {"value": b.value, "count": b.count} for b in facets.content_type
        ],
        "tag": [{"value": b.value, "count": b.count} for b in facets.tag],
        "tag_truncated": facets.tag_truncated,
    }


def _round_ms(value: float | None) -> float | None:
    """Round a millisecond reading to 0.1 ms, preserving ``None``."""
    return None if value is None else round(value, 1)


def search_meta_json(
    diag: SearchDiagnostics,
    *,
    returned: int,
    facets: SearchFacets | None,
) -> dict[str, Any]:
    """Project search metadata as additive JSON keys.

    The shared payload for BOTH metadata surfaces: MCP ``brain_search`` merges
    it into its existing dict, and :func:`search_envelope_json` wraps it for
    ``brain search --json --meta``. One projection means the two can never
    describe the same search differently.

    ``total_documents`` counts documents that LEXICALLY match; the vector leg
    may surface additional near-neighbours it does not include. It is ``null``
    when not requested or when the count query failed — never silently ``0``.
    ``fts_count`` keeps its long-standing capped semantics (only its zero case
    is exact) and is NOT a total.
    """
    return {
        "total_documents": diag.total_documents,
        "returned": returned,
        "fts_count": diag.fts_count,
        "timing_ms": {
            "embed": _round_ms(diag.embed_ms),
            "sql": _round_ms(diag.sql_ms),
            "facets": _round_ms(diag.facets_ms),
            "total": _round_ms(diag.total_ms),
        },
        "embed_cached": diag.embed_cached,
        "fts_only": _is_fts_only(diag),
        "facets": None if facets is None else facets_json(facets),
    }


def search_envelope_json(
    query: str,
    results: list[SearchResult],
    diag: SearchDiagnostics,
    facets: SearchFacets | None,
) -> dict[str, Any]:
    """Build the opt-in ``brain search --json --meta`` envelope.

    ``results`` is produced by :func:`search_results_json` — the same call the
    default bare-list path makes — so opting into the envelope cannot change a
    single result object.
    """
    return {
        "query": query,
        **search_meta_json(diag, returned=len(results), facets=facets),
        "results": search_results_json(results),
    }
