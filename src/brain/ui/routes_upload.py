"""`/api/ingest/uploads` and `/api/ingest/file` — the two-request upload (Ingest spec §5.2, §6).

Two requests, so the JSON-only gate stays intact for everything except one route:

1. ``POST /api/ingest/uploads``: a raw ``application/octet-stream`` body, its name in
   ``X-Brain-Filename`` (percent-encoded UTF-8). The ONE non-JSON route
   (``security.UPLOAD_ROUTE``). Streamed to a staging file under every cap, sniffed,
   and answered ``201 {upload_id, size, kind}``. Not a job.
2. ``POST /api/ingest/file``: JSON ``{upload_id, tags?, sensitivity?, enrich?, title?}``,
   which claims the upload and queues its job (202, a ``JobSnapshot``).

``DELETE /api/ingest/uploads/{upload_id}`` drops a staged file the user removed
before submitting; it takes ``{}`` and answers JSON, like cancel.

Thin: the ingest gate (403 off loopback, before a byte is read), then the staging
area and the service do the work. Every refusal of a body happens before it is
written or deletes what was written. Nothing here logs the filename.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._http import context_of, declared_length, json_body, ok, parse_json_object, read_body_capped
from .ingest_limits import MAX_FILE_SUBMIT_WIRE_BYTES
from .ingest_schemas import parse_file
from .ingest_service import require_ingest, require_uploads, submit_file
from .routes_ingest import queue_errors
from .routes_jobs import snapshot_payload
from .upload_checks import sanitize_filename

#: The request header that carries the original filename.
FILENAME_HEADER = "x-brain-filename"


async def stage_upload(request: Request) -> JSONResponse:
    """Stage one file: 201 ``{upload_id, size, kind}``."""
    ctx = context_of(request)
    require_ingest(ctx)
    staging = require_uploads(ctx)
    # From the header alone, before a byte of the body is read: 400 bad_filename,
    # or 415 unsupported_file for an extension outside the allow-list.
    name = sanitize_filename(request.headers.get(FILENAME_HEADER, ""))
    upload = await staging.stage(name, declared_length(request), request.stream())
    return ok({"upload_id": upload.upload_id, "size": upload.size, "kind": upload.kind}, status=201)


async def discard_upload(request: Request) -> JSONResponse:
    """Drop an unclaimed upload: 200 ``{upload_id, deleted: true}``."""
    ctx = context_of(request)
    require_ingest(ctx)
    staging = require_uploads(ctx)
    await json_body(request)  # the middleware requires a JSON body; `{}` is expected
    upload_id = request.path_params["upload_id"]
    staging.discard(upload_id)
    return ok({"upload_id": upload_id, "deleted": True})


async def submit_upload(request: Request) -> JSONResponse:
    """Queue the job for a staged upload: 202 with the job's snapshot."""
    ctx = context_of(request)
    jobs, runtime = require_ingest(ctx)
    staging = require_uploads(ctx)
    raw = await read_body_capped(request, MAX_FILE_SUBMIT_WIRE_BYTES)
    spec = parse_file(parse_json_object(raw))
    with queue_errors():
        snap = submit_file(ctx, jobs, runtime, staging, spec)
    return ok(
        snapshot_payload(snap, serve_confidential_titles=ctx.serve_confidential_titles),
        status=202,
    )
