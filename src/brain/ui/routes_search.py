"""`GET /api/search` — delegates to the same ``hybrid_search`` the CLI runs.

**Search is never reimplemented here**, and that is the single largest
correctness risk in this feature. Every ranking decision lives inside
``hybrid_search``: RRF at k=60, the per-document chunk cap, the compact-form
tsquery expansion, the empirically-tuned cosine floor, the 180-day recency
half-life, snippet-context expansion, and the FTS-only auto-degrade when the
embedder produces no vectors. All of it is regression-tested and **eval-gated**
— ``brain eval --fail-below`` exits 3 on an nDCG@5 / MRR / Recall@20 regression,
enforced in CI. A parallel SQL path in the UI would be invisible to that gate
and would drift silently.
"""
from __future__ import annotations

import uuid
from time import perf_counter
from typing import Any

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..errors import EmbedError, PersonAmbiguous, PersonNotFound
from ..facets import compute_facets
from ..format_search import search_meta_json
from ..search import SearchDiagnostics, build_tsquery
from ..search_predicate import build_predicate
from ..sensitivity import DEFAULT_SENSITIVITY
from . import telemetry
from ._http import context_of, db_guard, ok
from .errors import UiBadRequest, UiUnavailable
from .schemas import parse_search_params, search_result_payload


async def search(request: Request) -> JSONResponse:
    """Run one hybrid search and return results, facets, and the phase timing."""
    ctx = context_of(request)
    spec = parse_search_params(request.query_params)
    session_id = uuid.uuid4()
    diagnostics = SearchDiagnostics()

    # THE MATCH-MEMBERSHIP ORACLE.
    #
    # Redacting a confidential document's snippet is not sufficient on its own.
    # If the row still appears, an attacker who can reach this server can
    # reconstruct the withheld body a word at a time simply by issuing queries
    # and watching which ones make it appear — membership in a result set is
    # itself derived from the content. Snippet redaction hides the passage;
    # only excluding the row closes the oracle.
    #
    # Nothing is lost by excluding it: the left rail already lists every note by
    # title, so "you can see it exists" (the ruled property) is preserved there,
    # and the document is unreadable on this bind anyway.
    #
    # The snippet redaction below is KEPT as defence in depth — if this filter
    # is ever bypassed or a future caller forgets it, the passage still does not
    # ship.
    exclude_confidential = not ctx.serve_confidential_bodies
    sensitivity_filter = DEFAULT_SENSITIVITY if exclude_confidential else None

    started = perf_counter()
    try:
        with ctx.connect() as conn:
            results = ctx.search_fn(
                conn,
                embedder=ctx.embedder,
                diagnostics=diagnostics,
                total_count=True,
                sensitivity=sensitivity_filter,
                # Config-sourced tuning, passed exactly as the CLI and MCP do.
                # Reading them from cfg rather than hardcoding defaults is what
                # keeps the UI's ranking identical to `brain search`.
                vector_sim_floor=ctx.cfg.vector_sim_floor,
                recency_halflife_days=ctx.cfg.recency_halflife_days,
                snippet_context_tokens=ctx.cfg.snippet_context_tokens,
                snippet_max_chars=ctx.cfg.snippet_max_chars,
                **spec.filter_kwargs(),
            )
            redacted = _confidential_hits(ctx, conn, results)
            facets = _facets_for(conn, spec, sensitivity=sensitivity_filter)
            telemetry.record_ui_search(
                conn,
                enabled=ctx.logging_enabled,
                query=spec.query,
                result_count=len(results),
                session_id=session_id,
                fts_count=diagnostics.fts_count,
                duration_ms=int((perf_counter() - started) * 1000),
            )
    except EmbedError as exc:
        raise UiUnavailable(
            "the embedding backend is unavailable; retry with FTS-only search",
            code="embedding_unavailable",
        ) from exc
    except (PersonNotFound, PersonAmbiguous) as exc:
        raise UiBadRequest(str(exc), code="person_unresolved") from exc
    except psycopg.Error as exc:
        raise db_guard(exc) from exc

    meta = search_meta_json(diagnostics, returned=len(results), facets=facets)
    return ok(
        {
            "session_id": str(session_id),
            "query": spec.query,
            **meta,
            "results": [
                _redact(search_result_payload(r), redacted) for r in results
            ],
        }
    )


def _confidential_hits(
    ctx: Any, conn: psycopg.Connection[Any], results: list[Any]
) -> set[str]:
    """Ids in ``results`` whose snippets must not be returned.

    Empty when the server is allowed to serve confidential bodies, so the
    common loopback case pays nothing.
    """
    if ctx.serve_confidential_bodies:
        return set()
    from . import queries as ui_queries

    return ui_queries.confidential_document_ids(
        conn, [r.document_id for r in results]
    )


def _redact(payload: dict[str, Any], redacted: set[str]) -> dict[str, Any]:
    """Blank a confidential hit's snippet, keeping the row itself visible.

    A search snippet is body text by another name — it is lifted straight out
    of ``chunks`` — so withholding a body while returning its matching snippet
    would defeat the whole control. The document still appears in the results
    with its title and metadata: the user should know it matched, just not read
    the matching passage here.
    """
    if payload["id"] not in redacted:
        return payload
    payload["snippet"] = ""
    payload["withheld"] = True
    return payload


def _facets_for(
    conn: psycopg.Connection[Any], spec: Any, *, sensitivity: str | None
) -> Any:
    """Match-scoped facet counts, sharing the search's own predicate.

    ``compute_facets`` is handed the **same** predicate shape the ranked legs
    used, so the buckets can never describe a different match set than the
    results they annotate. Facets are best-effort: a failure here degrades the
    dropdowns to uncounted values rather than failing the search that produced
    real results.
    """
    try:
        predicate = build_predicate(
            source_kind=spec.source_kind,
            tag=spec.tag,
            content_type=spec.content_type,
            after=spec.after,
            before=spec.before,
            # Same sensitivity filter as the ranked legs. A facet count that
            # includes documents the results exclude would report the exact
            # number of confidential matches — the oracle by another route.
            sensitivity=sensitivity,
        )
        return compute_facets(
            conn, predicate=predicate, tsquery=build_tsquery(conn, spec.query)
        )
    except psycopg.Error:
        return None
