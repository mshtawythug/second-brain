"""`GET /api/notes/{id}/links` — the link neighbourhood of one note.

Deliberately a **separate, lazy** fetch rather than fields on the note payload.
`GET /api/notes/{id}` already ships up to a quarter-megabyte of body and HTML on
the largest documents in this corpus; a People Hub page carries hundreds of
edges, and folding those into the same response would make every note open pay
for a rail most readers never look at. The marginalia fetches this after the
note has painted, and renders nothing when it fails (spec §3.2).

Thin by construction, like every other ``routes_*`` module: resolve →
:mod:`brain.vault.graph` → serialize. **No SQL** — ``brain.ui.queries``'s
docstring makes it the only module allowed to hold any, and the two reads this
route needs already exist as ``backlinks_for`` / ``outgoing_links_for``.

Titles and ids only. Nothing here carries document content, which is what keeps
"lazy" true over time and keeps a confidential body out of a rail that the note
route itself may have withheld.
"""
from __future__ import annotations

from typing import Any

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..vault.graph import BacklinkRow, OutgoingLinkRow, backlinks_for, outgoing_links_for
from . import notes_service
from ._http import context_of, db_guard, ok


def _backlink_payload(row: BacklinkRow) -> dict[str, Any]:
    """One "links to me" row.

    ``rule`` and ``weight`` are populated only for ``link_kind='derived'``
    edges; they are emitted as ``None`` on wiki rows rather than omitted, so
    every element of the list has the same keys and the client needs no
    per-kind branching to read one.
    """
    return {
        "id": row.src_document_id,
        "title": row.src_title,
        "kind": row.src_kind,
        "link_text": row.link_text,
        "link_kind": row.link_kind,
        "rule": row.rule,
        "weight": None if row.weight is None else float(row.weight),
    }


def _outgoing_payload(row: OutgoingLinkRow) -> dict[str, Any]:
    """One "I link to" row. ``id``/``title``/``kind`` are null when unresolved."""
    return {
        "id": row.dst_document_id,
        "title": row.dst_title,
        "kind": row.dst_kind,
        "link_text": row.link_text,
        "link_kind": row.link_kind,
        "resolved": row.resolved,
        "rule": row.rule,
        "weight": None if row.weight is None else float(row.weight),
    }


async def note_links(request: Request) -> JSONResponse:
    """Backlinks and outgoing links for one note, by id prefix or full UUID.

    Fails closed on every axis: an unresolvable prefix is the same typed 400/404
    ``GET /api/notes/{id}`` returns (``notes_service.resolve_id`` owns that
    mapping, so the two routes cannot drift), and a database failure is a 503
    that leaks neither SQL nor connection details.

    ``evidence`` is not projected. It is free-form ``jsonb`` written by the
    derived-link rules and can be arbitrarily large; the rail needs the rule
    name and the weight, and nothing that reads this payload has a use for the
    rest.
    """
    ctx = context_of(request)
    prefix = request.path_params["id_prefix"]
    try:
        with ctx.connect() as conn:
            document_id = notes_service.resolve_id(conn, prefix)
            backlinks = [_backlink_payload(row) for row in backlinks_for(conn, document_id)]
            outgoing = [_outgoing_payload(row) for row in outgoing_links_for(conn, document_id)]
    except psycopg.Error as exc:
        raise db_guard(exc) from exc

    return ok(
        {
            "id": document_id,
            "backlinks": backlinks,
            "outgoing": outgoing,
            "counts": {"backlinks": len(backlinks), "outgoing": len(outgoing)},
        }
    )
