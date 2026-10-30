"""`/api/ingest` — the tab's config, the paste submit and the inbox (Ingest spec §6, RM-40).

Thin: gate → capped read → validate → submit. The upload routes (RM-13) are in
``routes_upload.py``; they share :func:`queue_errors` from here. The inbox routes
take and return file NAMES only, never a path; the guard is :mod:`brain.ui.inbox`.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import ConfigError
from ..sensitivity import DEFAULT_SENSITIVITY, VALID_SENSITIVITY_LEVELS
from ._http import context_of, ok, parse_json_object, read_body_capped
from .errors import UiTooManyRequests, UiUnavailable
from .inbox import (
    find_inbox,
    inbox_extensions,
    list_inbox,
    require_inbox,
    resolve_inbox_file,
)
from .ingest_limits import (
    DEFAULT_PASTE_CONTENT_TYPE,
    MAX_FILE_SUBMIT_WIRE_BYTES,
    MAX_PASTE_BODY_BYTES,
    MAX_PASTE_WIRE_BYTES,
    MAX_QUEUED_JOBS,
    MAX_STAGED_BYTES,
    PASTE_CONTENT_TYPES,
)
from .ingest_schemas import parse_inbox, parse_paste
from .ingest_service import require_ingest, submit_inbox, submit_paste, unavailable_reason
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


async def inbox_list(request: Request) -> JSONResponse:
    """The inbox's pickable files: names and sizes, never a path (RM-40.D6).

    Always 200, so the tab renders from it. ``files`` is empty unless this server may
    ingest (loopback, writable): the names are someone's private file names, and a
    LAN-bound or read-only server could not ingest them anyway.
    """
    ctx = context_of(request)
    reason = unavailable_reason(ctx)
    if reason is None:
        reason, body = _inbox_listing(ctx.cfg.brain_home)
    else:
        body = {}
    return ok(
        {"available": reason is None, "reason": reason, "extensions": [], "files": [],
         "truncated": False, "default_sensitivity": None, **body}
    )


def _inbox_listing(brain_home: Path) -> tuple[str | None, dict[str, Any]]:
    """``(reason, fields)`` for :func:`inbox_list`, once ingest itself is allowed."""
    try:
        folder = find_inbox(brain_home)
    except ConfigError:
        return "inbox_misconfigured", {}
    if folder is None:
        return "inbox_unconfigured", {}
    try:
        listing = list_inbox(folder, now=time.time())
    except OSError:  # removed since the config was read, or no permission (TCC)
        return "inbox_unreadable", {}
    return None, {
        "extensions": sorted(inbox_extensions(folder)),
        "files": [{"name": f.name, "size": f.size} for f in listing.files],
        "truncated": listing.truncated,
        # The tab preselects it: a submit without one gets it too (RM-40.D5).
        "default_sensitivity": folder.sensitivity,
    }


async def inbox_submit(request: Request) -> JSONResponse:
    """Queue the ingest of one inbox file, by name: 202 with the job's snapshot."""
    ctx = context_of(request)
    jobs, runtime = require_ingest(ctx)
    raw = await read_body_capped(request, MAX_FILE_SUBMIT_WIRE_BYTES)
    folder = require_inbox(ctx.cfg.brain_home)
    spec = parse_inbox(
        parse_json_object(raw),
        extensions=inbox_extensions(folder),
        default_sensitivity=folder.sensitivity,
    )
    # Resolve-then-compare and lstat now, for a clear 4xx; the job checks again (AC2).
    resolve_inbox_file(folder, spec.name)
    with queue_errors():
        snap = submit_inbox(ctx, jobs, runtime, folder, spec)
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
