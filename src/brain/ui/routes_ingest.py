"""`/api/ingest` — the tab's config and the paste submit (Ingest spec §6).

Thin: gate → capped read → validate → submit. Upload (RM-13) adds its routes here.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..sensitivity import DEFAULT_SENSITIVITY, VALID_SENSITIVITY_LEVELS
from ._http import context_of, ok, parse_json_object, read_body_capped
from .errors import UiTooManyRequests, UiUnavailable
from .ingest_limits import (
    DEFAULT_PASTE_CONTENT_TYPE,
    MAX_PASTE_BODY_BYTES,
    MAX_PASTE_WIRE_BYTES,
    MAX_QUEUED_JOBS,
    PASTE_CONTENT_TYPES,
)
from .ingest_schemas import parse_paste
from .ingest_service import require_ingest, submit_paste, unavailable_reason
from .jobs import QueueFull, ShuttingDown
from .routes_jobs import snapshot_payload


async def ingest_config(request: Request) -> JSONResponse:
    """What the tab renders from. It hard-codes no limit and no list."""
    reason = unavailable_reason(context_of(request))
    return ok(
        {
            "enabled": reason is None,
            "reason": reason,
            # Upload is RM-13; until it ships no file extension is accepted.
            "extensions": [],
            "limits": {
                "paste_body_bytes": MAX_PASTE_BODY_BYTES,
                "max_queued_jobs": MAX_QUEUED_JOBS,
            },
            "content_types": list(PASTE_CONTENT_TYPES),
            "default_content_type": DEFAULT_PASTE_CONTENT_TYPE,
            "sensitivity_levels": sorted(VALID_SENSITIVITY_LEVELS),
            "default_sensitivity": DEFAULT_SENSITIVITY,
        }
    )


async def paste(request: Request) -> JSONResponse:
    """Queue a paste ingest: 202 with the job's snapshot."""
    ctx = context_of(request)
    jobs, runtime = require_ingest(ctx)
    # Never `request.body()`: the wire cap is enforced on the stream (C7).
    raw = await read_body_capped(request, MAX_PASTE_WIRE_BYTES)
    spec = parse_paste(parse_json_object(raw))
    try:
        snap = submit_paste(ctx, jobs, runtime, spec)
    except QueueFull as exc:
        raise UiTooManyRequests(
            f"at most {MAX_QUEUED_JOBS} jobs may wait; retry when one finishes",
            code="queue_full",
        ) from exc
    except ShuttingDown as exc:
        raise UiUnavailable("the server is shutting down", code="shutting_down") from exc
    return ok(
        snapshot_payload(snap, serve_confidential_titles=ctx.serve_confidential_titles),
        status=202,
    )
