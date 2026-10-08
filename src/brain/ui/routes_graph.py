"""`GET /api/notes/{id}/graph` — the one-ring neighbourhood graph of one note.

A **separate, lazy** fetch for the same reason as ``/links``: the note payload
is already the heaviest response in the UI, and most opens never look at the
graph panel. The client fetches this after the note has painted.

Thin by construction, like every other ``routes_*`` module: resolve →
:func:`brain.vault.graph.graph_data` (depth 1) → :func:`brain.ui.graph_layout.layout`
→ serialize. **No SQL** — ``brain.ui.queries`` is the only UI module allowed to
hold any, and the two reads this route needs beyond ``graph_data``
(``corpus_has_links``, ``document_sensitivity``) live there.

The geometry is computed here, not in the browser (ruling R2), so the payload
is coordinates plus titles. Titles are plain strings — nothing in this payload
is HTML, and the client must render them as text.

NO GRAPH SURFACE MAY TRIGGER A BUILD (spec §6.5). This module reads the
``links`` / ``derived_links`` tables as they are and imports nothing from
``brain.maintenance``, ``brain.graph_rag`` or ``brain.wiki``; an empty corpus is
reported as ``corpus_linked: false`` rather than repaired.

Confidentiality follows ``/links``: gated on ``serve_confidential_titles``.
``graph_data`` withholds a confidential neighbour AND every edge touching it,
and ``truncated`` is counted after that gate, so it cannot disclose how many
neighbours were withheld.
"""
from __future__ import annotations

from typing import Any

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..sensitivity import is_confidential
from ..vault.graph import graph_data
from . import graph_layout, notes_service, queries
from ._http import context_of, db_guard, ok
from .errors import UiForbidden, UiNotFound

#: Neighbours drawn on the ring (ruling R4); the rest are reported as ``truncated``.
#: Passed explicitly at the call site, but defined once, in ``graph_layout``.
GRAPH_CAP = graph_layout.DEFAULT_CAP


def _payload(
    document_id: str, result: graph_layout.LayoutResult, *, linked: bool
) -> dict[str, Any]:
    """The wire shape. Every node and edge has the same keys."""
    return {
        "id": document_id,
        "width": result.width,
        "height": result.height,
        "nodes": [
            {
                "id": n.id,
                "title": n.title,
                "kind": n.kind,
                "x": n.x,
                "y": n.y,
                "r": n.r,
                "root": n.root,
            }
            for n in result.nodes
        ],
        "edges": [{"src": e.src, "dst": e.dst, "kind": e.kind} for e in result.edges],
        "truncated": result.truncated,
        "corpus_linked": linked,
    }


async def note_graph(request: Request) -> JSONResponse:
    """The depth-1 graph around one note, by id prefix or full UUID.

    Fails closed on every axis: an unresolvable prefix is the same typed 400/404
    ``GET /api/notes/{id}`` returns (``notes_service.resolve_id`` owns that
    mapping), and a database failure is a 503 that leaks neither SQL nor
    connection details.

    A CONFIDENTIAL ROOT in a strict session is a typed 403 ``graph_withheld``.
    ``graph_data(exclude_confidential=True)`` withholds the root itself, so
    there is no honest picture to draw. The note route answers that document
    with 200 and a ``withheld`` notice, so the client never asks for its graph;
    a direct caller gets neither a root-only payload (that would carry the
    title) nor a 404 (which would contradict ``/api/notes`` confirming the id
    exists). The check runs BEFORE ``layout``.

    The sensitivity read and ``graph_data`` are separate reads, so a root
    deleted — or sealed under a strict lens — between them comes back from
    ``graph_data`` without its root node. That race is a typed 404
    ``note_not_found`` naming nothing, not ``layout``'s ``ValueError`` as a
    bare 500.
    It reuses :func:`brain.sensitivity.is_confidential`, the predicate
    ``notes_service.read_note`` decides ``withheld`` with.
    """
    ctx = context_of(request)
    strict = not ctx.serve_confidential_titles
    prefix = request.path_params["id_prefix"]
    try:
        with ctx.connect() as conn:
            document_id = notes_service.resolve_id(conn, prefix)
            if strict and is_confidential(queries.document_sensitivity(conn, document_id)):
                raise UiForbidden(
                    "the graph of a confidential note is not served here",
                    code="graph_withheld",
                )
            graph = graph_data(
                conn,
                root=document_id,
                depth=1,
                include_ingested=True,
                include_derived=True,
                exclude_confidential=strict,
            )
            corpus_linked = queries.corpus_has_links(conn)
    except psycopg.Error as exc:
        raise db_guard(exc) from exc

    if all(node.document_id != document_id for node in graph.nodes):
        raise UiNotFound("no such document", code="note_not_found")
    result = graph_layout.layout(graph, root=document_id, cap=GRAPH_CAP)
    return ok(_payload(document_id, result, linked=corpus_linked))
