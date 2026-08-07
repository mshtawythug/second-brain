"""`GET /api/health`, `/api/status`, `/api/facets`."""
from __future__ import annotations

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..queries import list_existing_tags, summary_counts
from . import queries as ui_queries
from ._http import context_of, db_guard, ok
from .schemas import VALID_SOURCE_KINDS


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
            sources = ui_queries.source_kind_buckets(conn, known=VALID_SOURCE_KINDS)
            content_types = ui_queries.content_type_buckets(conn)
            tags = list_existing_tags(conn, min_doc_count=1)
    except psycopg.Error as exc:
        raise db_guard(exc) from exc

    return ok(
        {
            "sources": sources,
            "content_types": content_types,
            "tags": [{"value": tag, "count": None} for tag in tags],
        }
    )
