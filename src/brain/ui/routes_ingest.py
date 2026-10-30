"""`/api/ingest` — the tab's config and the paste submit (Ingest spec §6).

Thin: gate → capped read → validate → submit. The upload routes (RM-13) are in
``routes_upload.py``; they share :func:`queue_errors` from here.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

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
    MAX_STAGED_BYTES,
    PASTE_CONTENT_TYPES,
)
from .ingest_schemas import parse_paste
from .ingest_service import require_ingest, submit_paste, unavailable_reason
from .jobs import QueueFull, ShuttingDown
from .routes_jobs import snapshot_payload
from .upload_checks import upload_cap, uploadable_extensions


async def ingest_config(request: Request) -> JSONResponse:
    """What the tab renders from. It hard-codes no limit and no list."""
    ctx = context_of(request)
    reason = unavailable_reason(ctx)
    # The allow-list ∩ the extractor registry, never a list typed here; none at
    # all when this server has no staging area to put an upload in.
    extensions = uploadable_extensions() if ctx.uploads is not None else []
    return ok(
        {
            "enabled": reason is None,
            "reason": reason,
            "extensions": extensions,
            "limits": {
                "paste_body_bytes": MAX_PASTE_BODY_BYTES,
                "max_queued_jobs": MAX_QUEUED_JOBS,
                "upload_bytes": {ext: upload_cap(ext) for ext in extensions},
                "staged_bytes": MAX_STAGED_BYTES,
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
    with queue_errors():
        snap = submit_paste(ctx, jobs, runtime, spec)
    return ok(
        snapshot_payload(snap, serve_confidential_titles=ctx.serve_confidential_titles),
        status=202,
    )


@contextmanager
def queue_errors() -> Iterator[None]:
    """Map the registry's refusals onto 429 ``queue_full`` / 503 ``shutting_down``."""
    try:
        yield
    except QueueFull as exc:
        raise UiTooManyRequests(
            f"at most {MAX_QUEUED_JOBS} jobs may wait; retry when one finishes",
            code="queue_full",
        ) from exc
    except ShuttingDown as exc:
        raise UiUnavailable("the server is shutting down", code="shutting_down") from exc
