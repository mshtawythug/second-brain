"""`GET /api/notes/{id}/related` — the related-docs panel for one note.

A **separate, lazy** fetch for the same reason as ``/links`` (see
:mod:`brain.ui.routes_links`): the note payload is already large, and the panel
is fetched after the note has painted and renders nothing when it fails.

Thin by construction, like every other ``routes_*`` module: resolve →
:func:`brain.related.compute_related` → serialize. **No SQL** —
``brain.ui.queries`` is the only module in the package allowed to hold any, and
the one read this route needs beyond ``compute_related`` (the root's
sensitivity tier) lives there as ``document_sensitivity``.

Binding rule (spec §6.5): this module imports nothing from
``brain.maintenance``, ``brain.graph_rag`` or ``brain.wiki``. The ranking it
serves is the per-document entry point in ``brain.related``, which the old wiki
emitter also delegates to — so the panel and the retiring precompute rank
identically without the panel depending on the emitter.

THE FLOOR IS THE ONE ``brain search`` READS (ruling R1). ``compute_related``
deliberately has no default for ``vector_sim_floor``: the cosine floor is shared
with runtime search, and a panel that ranked with its own copy would drift from
search the first time someone tuned ``BRAIN_VECTOR_SIM_FLOOR`` — silently,
because both surfaces would still return plausible-looking lists. Every other
answer (a route constant, a UI knob, a fallback here) is a second value that
*can* diverge. Reading ``ctx.cfg.vector_sim_floor`` — the same ``Config`` field
search reads — is the only answer that cannot, so that is the whole of it: no
literal, no default, no new knob. The value used is echoed in the payload so a
reader comparing the panel against ``brain search`` can see they agree.

THIS IS BODY EGRESS, NOT TITLE EGRESS (ruling R6). ``RelatedDoc.snippet`` is a
slice of a candidate's chunk *content*. The links rail gates on
``serve_confidential_titles`` alone because it carries titles and ids only;
this panel carries text out of the document, so a confidential candidate is
served only when BOTH lenses are permissive — the title lens (it names the
document) and the body lens (it quotes it).

THE ROOT IS GATED TOO, AND ``compute_related`` DOES NOT DO IT. Its predicate
filters *candidates*. Opened on a confidential root in a strict session, it
would happily list "documents similar to the withheld one", which is a précis
of exactly what is being withheld — the trap ``notes_service.read_note`` names
for ``summary`` beside a withheld body. So the root's tier is checked first,
with the same strictness, and the request refused with a 403 that names no
title and no candidate. The check runs BEFORE ranking, so a refusal costs
nothing and cannot leak through timing of a ranked result.

THE SNIPPET IS CAPPED, AND A CUT IS FLAGGED (ruling R7). ``snippet`` is trimmed
to ``ctx.cfg.snippet_max_chars`` — the ``BRAIN_SNIPPET_MAX_CHARS`` knob search
already honours — and every row carries ``snippet_truncated`` so a cut is never
silent: refused or trimmed-with-a-flag, per the repo's ceiling rule.
``compute_related`` already slices to ``brain.related.SNIPPET_LENGTH``, which is
below the default cap, so under default config this cap does not fire; it is
the route's contract, not the ranking module's, and it holds whatever either
value is later set to.

An empty ``related`` list is a normal 200, not an error: a lone document, or one
whose legs both drop out (no embedded chunks, no distinctive lexemes), simply
has no neighbours.
"""
from __future__ import annotations

from typing import Any

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..related import DEFAULT_RELATED_LIMIT, RelatedDoc, compute_related
from ..sensitivity import is_confidential
from . import notes_service
from . import queries as ui_queries
from ._http import context_of, db_guard, ok
from .errors import UiForbidden


def _related_payload(doc: RelatedDoc, *, snippet_max_chars: int) -> dict[str, Any]:
    """One panel row. ``snippet_truncated`` is present on every row, so a
    client can tell "short snippet" from "cut snippet" without guessing.
    """
    truncated = len(doc.snippet) > snippet_max_chars
    return {
        "id": doc.id,
        "title": doc.title,
        "vault_path": doc.vault_path,
        "source": doc.source,
        "score": float(doc.score),
        "snippet": doc.snippet[:snippet_max_chars] if truncated else doc.snippet,
        "snippet_truncated": truncated,
    }


async def note_related(request: Request) -> JSONResponse:
    """Documents related to one note, by id prefix or full UUID, best first.

    Fails closed on every axis: an unresolvable prefix is the same typed 400/404
    ``GET /api/notes/{id}`` returns (``notes_service.resolve_id`` owns that
    mapping, so the routes cannot drift); a confidential root in a strict
    session is a 403 ``related_withheld`` carrying no title and no candidate;
    and a database failure anywhere — connect, resolve, sensitivity read, or
    ranking — is a 503 that leaks neither SQL nor connection details.

    ``count`` is taken from the filtered list, so it cannot report how many
    confidential neighbours were withheld.

    The client mirrors the root gate exactly (``relatedRefusedHere`` in
    ``static/js/related.js``, reading both lenses off ``/api/health``) and shows
    a notice instead of asking; the two must change together.
    """
    ctx = context_of(request)
    strict = not (ctx.serve_confidential_titles and ctx.serve_confidential_bodies)
    floor = ctx.cfg.vector_sim_floor
    prefix = request.path_params["id_prefix"]
    try:
        with ctx.connect() as conn:
            document_id = notes_service.resolve_id(conn, prefix)
            if strict and is_confidential(
                ui_queries.document_sensitivity(conn, document_id)
            ):
                raise UiForbidden(
                    "related documents are withheld for a confidential note",
                    code="related_withheld",
                )
            related = compute_related(
                conn,
                document_id,
                limit=DEFAULT_RELATED_LIMIT,
                vector_sim_floor=floor,
                exclude_confidential=strict,
            )
    except psycopg.Error as exc:
        raise db_guard(exc) from exc

    rows = [
        _related_payload(doc, snippet_max_chars=ctx.cfg.snippet_max_chars)
        for doc in related
    ]
    return ok(
        {
            "id": document_id,
            "related": rows,
            "count": len(rows),
            "vector_sim_floor": floor,
        }
    )
