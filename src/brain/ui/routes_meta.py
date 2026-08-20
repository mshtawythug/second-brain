"""`GET /api/health`, `/api/status`, `/api/facets`."""
from __future__ import annotations

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..queries import summary_counts
from . import queries as ui_queries
from ._http import context_of, db_guard, ok
from .schemas import SOURCE_NONE, VALID_SOURCE_KINDS


async def health(request: Request) -> JSONResponse:
    """Liveness. Touches no database, so it answers even when Postgres is down.

    That is the point: the shell uses it to tell "the server died" apart from
    "the database died", which are different problems with different fixes.
    """
    ctx = context_of(request)
    return ok(
        {
            "status": "ok",
            "read_only": ctx.read_only,
            "vault": str(ctx.cfg.vault_path),
            "logging_enabled": ctx.logging_enabled,
            "serve_confidential_bodies": ctx.serve_confidential_bodies,
            # Both gates, because they now differ. ``bodies`` answers "may
            # this session read a confidential note it opened"; ``titles``
            # answers "may an unprompted rail name one". Reporting only the
            # first would let a client conclude the wrong thing about the tree.
            "serve_confidential_titles": ctx.serve_confidential_titles,
            # T18. The owner's own address, so the email-thread rail can offer
            # "only my replies". `None` when BRAIN_USER_EMAIL is unset, and the
            # client treats that as "no filter available" rather than as a
            # filter matching nothing.
            #
            # HERE RATHER THAN ON THE NOTE PAYLOAD, and that is the whole point
            # of the feature: this endpoint is answered PER REQUEST, so changing
            # BRAIN_USER_EMAIL takes effect on the next page load. The wiki bakes
            # the same value in at build time and needs a full rebuild to change
            # it. Putting it on the note payload instead would repeat one
            # constant on every note fetch for no gain.
            #
            # Not a disclosure: this is the operator's own address, returned to
            # the operator, on a surface that already reports the vault path.
            "user_email": ctx.cfg.user_email,
            "notices": list(ctx.notices),
        }
    )


async def status(request: Request) -> JSONResponse:
    """Corpus counts, via the single-CTE ``queries.summary_counts``."""
    ctx = context_of(request)
    try:
        with ctx.connect() as conn:
            counts = summary_counts(conn)
    except psycopg.Error as exc:
        raise db_guard(exc) from exc

    return ok(
        {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "sources": counts.sources,
            "last_ingest": (
                counts.last_ingest.isoformat() if counts.last_ingest else None
            ),
            "by_kind": [list(pair) for pair in counts.by_kind],
        }
    )


def _source_values(
    buckets: list[dict[str, object]], *, sourceless: int
) -> list[dict[str, object]]:
    """The Source dropdown's values: the four real kinds plus ``none`` (T7).

    ``none`` selects documents with no ``sources`` row at all. Without it those
    documents are unreachable from this filter in **every** setting, because
    ``d.source_id IN (SELECT id FROM sources WHERE kind=%s)`` is false for a
    NULL ``source_id`` whatever ``kind`` is.

    ITS COUNT IS NOW A REAL NUMBER. It shipped ``null`` because no statement in
    ``ui/queries.py`` — the only module in this package allowed to contain SQL
    — could produce it, and ``queries.source_kind_buckets`` structurally cannot:
    it starts ``FROM sources``, so a source-less document is unreachable there,
    not merely uncounted. That reasoning was sound about
    ``source_kind_buckets`` and wrong about the package: the fix was to add the
    one statement, which :func:`brain.ui.queries.sourceless_document_count` now
    is. ``null`` was the honest answer only while the number was unobtainable.

    The count is corpus-wide, matching the ``sources.kind`` counts it sits
    beside — one differently-scoped number in a row of comparable ones is the
    same defect in a smaller font.
    """
    return [*buckets, {"value": SOURCE_NONE, "count": sourceless}]


async def facets(request: Request) -> JSONResponse:
    """Corpus-wide values for the three dropdowns.

    These are the *unfiltered* vocabularies used to populate the controls before
    a query runs. Once a search has run, F5's ``compute_facets`` supplies
    match-scoped counts on the search response itself, and the UI prefers those
    — a dropdown showing corpus totals next to a filtered result set would be
    actively misleading.
    """
    ctx = context_of(request)
    try:
        with ctx.connect() as conn:
            sources = _source_values(
                ui_queries.source_kind_buckets(conn, known=VALID_SOURCE_KINDS),
                sourceless=ui_queries.sourceless_document_count(conn),
            )
            content_types = ui_queries.content_type_buckets(conn)
            tags = ui_queries.tag_counts(conn, min_doc_count=1)
    except psycopg.Error as exc:
        raise db_guard(exc) from exc

    return ok(
        {
            "sources": sources,
            "content_types": content_types,
            # Real counts since T4: ``queries.list_existing_tags`` already
            # computed them and threw them away, so this route shipped
            # ``count: null`` for tags while every other facet carried a number.
            #
            # DELIBERATE INCONSISTENCY, recorded so nobody "fixes" it blind:
            # ``tag_counts`` counts the WHOLE corpus — drafts and ``people/``
            # pages included — mirroring the ``list_existing_tags`` behaviour it
            # replaces, whereas T4's ``documents_for_tag`` applies browse
            # predicates. A facet count can therefore legitimately exceed the
            # rows a tag click surfaces. That is correct HERE: ``/api/facets``
            # annotates *search*, which returns drafts and hub pages, so a
            # browse-filtered count would understate its own result set. The tag
            # INDEX surface needs browse-consistent counts; that is T17's, not
            # this route's.
            #
            # Note the direction of travel: T4 removed a ``count: null`` here,
            # T7 added one back for the ``none`` source value, and the
            # 2026-08-14 ruling removed that one too — see ``_source_values``.
            # No facet on this route ships a null count any more.
            "tags": tags,
        }
    )
