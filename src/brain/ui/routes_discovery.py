"""`/api/recent`, `/api/tags`, `/api/tags/{tag}` — browse without searching.

The wiki's home rail and its tag index, ported as live reads: a static site had
to bake "12 most recent" at build time and recompute the relative date in the
browser so it never decayed, while a server can simply answer with fresh rows
(spec §3.2). `brain ui` has had a tag *filter* on search since phase 0 but no
tag *index* — this is that index.

Thin by construction: parse → :mod:`brain.ui.queries` → serialize. **No SQL** —
``queries.recent_documents`` / ``documents_for_tag`` / ``tag_counts`` own the
predicates, including which documents count as browseable at all.

Each row is an id, a title, a path and a date. No bodies, no snippets: these
rails are fetched on load, and a browse surface that grew a body per row would
turn one page open into a corpus download.

**Confidentiality is gated on ``serve_confidential_titles``**, not on
``serve_confidential_bodies``, and identically across all three routes. These
rails are *unprompted* — "fetched on load", above — so what they may name is
the title question, not the body question. The vault tree
(``routes_tree.tree``) is gated on the same flag, which is the point: the three
unprompted listing surfaces now agree, where previously the tree filtered
nothing while these two filtered on a flag named for something else.
"""
from __future__ import annotations

from typing import Any

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..tags import normalize_tag
from . import queries as ui_queries
from ._http import context_of, db_guard, ok
from .errors import UiBadRequest

#: How many rows the home rail returns. Matches the wiki's
#: ``build_homepage`` rail, which is the affordance being ported.
RECENT_LIMIT = 12

#: How many documents one tag page returns. A cap rather than pagination:
#: nothing in phase 2 pages this surface, and an uncapped tag page on a
#: 1,392-note corpus is a rail that never finishes rendering.
TAG_PAGE_LIMIT = 50


async def recent(request: Request) -> JSONResponse:
    """The most recently *dated* browseable documents, newest first.

    Ranked on event time (``coalesce(sent_at, ingested_at)``), so a meeting held
    last month but ingested this morning appears where the reader expects it
    rather than at the top.
    """
    ctx = context_of(request)
    strict = not ctx.serve_confidential_titles
    try:
        with ctx.connect() as conn:
            documents = ui_queries.recent_documents(
                conn, limit=RECENT_LIMIT, exclude_confidential=strict
            )
    except psycopg.Error as exc:
        raise db_guard(exc) from exc
    return ok({"documents": documents, "count": len(documents)})


async def tags(request: Request) -> JSONResponse:
    """Every BROWSEABLE tag with the number of documents carrying it.

    ``/api/facets`` answers the *dropdown's* question and ships ``count: null``;
    this answers the index's, where the count is the whole point — it is what
    tells the reader which tags are load-bearing and which were used once.

    ``browseable_tag_counts``, NOT ``tag_counts``, and the difference is a
    confidentiality boundary rather than a preference. This route feeds the idle
    rail, which paints before the reader has asked for anything; the corpus-wide
    count would name a tag carried only by confidential documents, and the tag
    page below would then return nothing for it. ``/api/facets`` keeps the
    corpus-wide count because it annotates SEARCH, which legitimately returns
    those documents — two consumers, two correct answers. See
    ``queries.browseable_tag_counts``.
    """
    ctx = context_of(request)
    strict = not ctx.serve_confidential_titles
    try:
        with ctx.connect() as conn:
            buckets = ui_queries.browseable_tag_counts(
                conn, exclude_confidential=strict
            )
    except psycopg.Error as exc:
        raise db_guard(exc) from exc
    return ok({"tags": buckets, "count": len(buckets)})


async def tag_documents(request: Request) -> JSONResponse:
    """Browseable documents carrying one tag, newest first.

    The tag is canonicalized before use, so ``/api/tags/Vendors`` and
    ``/api/tags/vendors`` are one page rather than two, and the response echoes
    the canonical form the rows were actually matched on.

    A value that canonicalizes to nothing is a 400, not an empty list: an empty
    list is the answer to "which documents carry this tag", and returning it for
    a string that is not a tag at all asserts something untrue about the corpus.
    """
    ctx = context_of(request)
    strict = not ctx.serve_confidential_titles
    raw: Any = request.path_params["tag"]
    tag = normalize_tag(str(raw))
    if not tag:
        raise UiBadRequest(
            "that is not a usable tag once normalized", code="invalid_tag"
        )
    try:
        with ctx.connect() as conn:
            documents = ui_queries.documents_for_tag(
                conn, tag, limit=TAG_PAGE_LIMIT, exclude_confidential=strict
            )
    except psycopg.Error as exc:
        raise db_guard(exc) from exc
    return ok({"tag": tag, "documents": documents, "count": len(documents)})
