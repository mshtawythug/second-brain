"""MCP near-duplicate tool: ``brain_dedup_list`` (RM-19).

The read-only twin of ``brain dedup list``, over :mod:`brain.near_dup_store`. It never scans and
never labels a pair: scanning is the daemon's nightly ``brain dedup scan``, and labelling is the
owner's (``brain dedup dismiss / confirm``).

**The F6 lens is on both ends.** A pair names two documents, so with ``include_confidential``
false (the default) a pair is returned only when NEITHER end is confidential, in the SQL
predicate, as ``connect.iter_suggestions`` gates both endpoints. Gating one end would still name
every confidential document that is somebody's near-copy, and a near-copy of a named document
discloses its content. No total is returned: a count over both tiers would be the membership
oracle ``mcp_server._confidential_lens`` describes.
"""

from __future__ import annotations

from typing import Any

import psycopg
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from ..mcp_compat import MCPServerProtocol
from ..near_dup_store import STATUSES, list_pairs, tables_present
from ._context import ContextSlot, ToolContext

_CONTEXT = ContextSlot(__name__)

#: The most pairs one call returns.
MAX_LIMIT = 200


def brain_dedup_list(
    status: str = "open", limit: int = 50, include_confidential: bool = False
) -> dict[str, Any]:
    """List recorded near-duplicate document pairs, most similar first. Read-only.

    ``status`` is ``open`` (the default: pairs nobody has labelled), ``dismissed`` (the owner
    said they are not duplicates) or ``confirmed``. ``limit`` is 1 to 200 (default 50). Returns
    ``{pairs: [{doc_a: {id, title}, doc_b: {id, title}, similarity, status, detected_at}]}``;
    ``similarity`` is the estimated Jaccard of the two bodies' word 5-grams (at least 0.85).
    Pairs are found by the nightly ``brain dedup scan``; this tool never scans or labels.

    F6: a pair is listed only when NEITHER document is confidential, unless
    ``include_confidential=true``. A bad ``status`` or ``limit`` is ``INVALID_PARAMS``; a
    database without migration 030 is ``INTERNAL_ERROR`` naming ``brain init``.
    """
    ctx = _CONTEXT.get()
    if status not in STATUSES:
        raise ctx.error(INVALID_PARAMS, f"status must be one of {', '.join(STATUSES)}")
    if not 1 <= limit <= MAX_LIMIT:
        raise ctx.error(INVALID_PARAMS, f"limit must be between 1 and {MAX_LIMIT}")
    exclude_confidential = ctx.confidential_lens(include_confidential) is not None
    state = ctx.get_state()
    try:
        with ctx.connection(state) as conn:
            if not tables_present(conn):
                raise ctx.error(
                    INTERNAL_ERROR, "near-duplicate detection needs migration 030: run `brain init`"
                )
            rows = list_pairs(
                conn, status=status, exclude_confidential=exclude_confidential, limit=limit
            )
    except psycopg.Error as exc:
        raise ctx.wrap_db_error(exc) from exc
    return {"pairs": [row.to_dict() for row in rows]}


def register(app: MCPServerProtocol, ctx: ToolContext) -> None:
    """Bind ``ctx`` (refusing a different one), then register the tool on ``app``."""
    _CONTEXT.bind(ctx)
    app.tool()(brain_dedup_list)
