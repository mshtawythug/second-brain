"""`/api/jobs` — list, poll and cancel jobs (Ingest spec §4.3, §4.4, §6).

Thin: every read is an in-memory registry read with no database call, which is
why polling every second costs microseconds. Cancel is the one mutation, and like
every ingest mutation it is refused off loopback (D8).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._http import context_of, json_body, ok
from .errors import UiBadRequest, UiConflict, UiNotFound
from .ingest_service import require_ingest, require_jobs
from .jobs import JobSnapshot, JobUnknown, NotCancellable


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def snapshot_payload(snap: JobSnapshot, *, listing: bool = False) -> dict[str, Any]:
    """The wire shape of a :class:`JobSnapshot`.

    ``listing`` drops the event log AND the result. A result names its document
    (title, vault path), so the unprompted list must not carry it: the list is a
    listing surface like the tree, and ``serve_confidential_titles`` defaults to
    hiding there. A client reads a result by the id it submitted
    (``GET /api/jobs/{id}``), which is prompted. Pinned by
    ``tests/test_ui_confidential_titles_gate.py``, which discovers this route.
    """
    payload: dict[str, Any] = {
        "id": snap.id,
        "kind": snap.kind,
        "state": snap.state,
        "cancellable": snap.cancellable,
        "cancel_requested": snap.cancel_requested,
        "created_at": _iso(snap.created_at),
        "started_at": _iso(snap.started_at),
        "finished_at": _iso(snap.finished_at),
        "error": (
            {"code": snap.error.code, "message": snap.error.message}
            if snap.error is not None
            else None
        ),
    }
    if listing:
        return payload
    payload["result"] = dict(snap.result) if snap.result is not None else None
    payload["events"] = [
        {
            "seq": e.seq,
            "at": e.at.isoformat(),
            "phase": e.phase,
            "done": e.done,
            "total": e.total,
            "code": e.code,
        }
        for e in snap.events
    ]
    return payload


def _unknown() -> UiNotFound:
    return UiNotFound(
        "no such job; its outcome is unknown. Re-submitting is safe "
        "(identical content is a no-op)",
        code="job_unknown",
    )


def _after(request: Request) -> int:
    raw = request.query_params.get("after", "0")
    if not raw.isdigit():
        raise UiBadRequest("after must be a non-negative integer", code="invalid_after")
    return int(raw)


async def list_jobs(request: Request) -> JSONResponse:
    """Recent jobs, newest first, without their event logs or results."""
    jobs = require_jobs(context_of(request))
    return ok({"jobs": [snapshot_payload(s, listing=True) for s in jobs.list_recent()]})


async def get_job(request: Request) -> JSONResponse:
    """One job, with only the events whose ``seq > after``."""
    jobs = require_jobs(context_of(request))
    after = _after(request)
    try:
        snap = jobs.get(request.path_params["job_id"], after=after)
    except JobUnknown as exc:
        raise _unknown() from exc
    return ok(snapshot_payload(snap))


async def cancel_job(request: Request) -> JSONResponse:
    """200 when cancelled or already terminal; 202 while a running job winds down."""
    jobs, _runtime = require_ingest(context_of(request))
    await json_body(request)  # the middleware requires a JSON body; `{}` is expected
    try:
        snap = jobs.cancel(request.path_params["job_id"])
    except JobUnknown as exc:
        raise _unknown() from exc
    except NotCancellable as exc:
        raise UiConflict(
            "writing has started; the job will finish", code="not_cancellable"
        ) from exc
    return ok(snapshot_payload(snap), status=202 if snap.state == "running" else 200)
