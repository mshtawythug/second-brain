"""`/api/jobs` — list, poll and cancel jobs (Ingest spec §4.3, §4.4, §6).

Thin: every read is an in-memory registry read with no database call, which is
why polling every second costs microseconds. Cancel is the one mutation, and like
every ingest mutation it is refused off loopback (D8).
"""
from __future__ import annotations

from collections.abc import Mapping
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


#: The result keys that name a document, withheld by the lens below. The id is
#: one of them: ``GET /api/notes/<id>`` opens the note, title and body included.
#: So is an upload's ``original_filename`` (RM-13): a filename is often more
#: revealing than a title (Ingest spec §7.5).
_NAMING_KEYS = ("title", "vault_path", "document_id", "original_filename")


def _thaw(value: Any) -> Any:
    """A JSON-ready copy of a frozen result (``jobs.freeze`` reversed)."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def lensed_result(
    result: Mapping[str, Any] | None, *, serve_confidential_titles: bool
) -> dict[str, Any] | None:
    """The result as this server may show it: the confidential-titles lens.

    A job id is NOT a capability: ``GET /api/jobs`` lists every retained id, and
    a dedup skip can land on an existing confidential document the paste did not
    name. So unless ``serve_confidential_titles`` is on (the gate the tree and the
    recent rail use), a result that names a document has every key in
    :data:`_NAMING_KEYS` withheld (``None``, with
    ``title_withheld: true``) unless it says the document is ``normal``. That is
    fail-closed: a result naming a document with no ``sensitivity`` at all is
    withheld too. Applied on every path that returns a snapshot. The cost: a
    confidential paste gets no "open in Notes" link while the flag is off, the
    same as the tree.
    """
    if result is None:
        return None
    payload: dict[str, Any] = _thaw(result)
    names_a_document = any(payload.get(key) is not None for key in _NAMING_KEYS)
    withhold = (
        not serve_confidential_titles
        and names_a_document
        and payload.get("sensitivity") != "normal"
    )
    if withhold:
        for key in _NAMING_KEYS:
            if key in payload:
                payload[key] = None
    payload["title_withheld"] = withhold
    return payload


def snapshot_payload(
    snap: JobSnapshot, *, serve_confidential_titles: bool, listing: bool = False
) -> dict[str, Any]:
    """The wire shape of a :class:`JobSnapshot`, with the result lensed.

    ``listing`` drops the event log and the result: the list only reports which
    jobs exist and their state. A client reads a result by id, and that result
    passes the confidential-titles lens (:func:`lensed_result`) like every other.
    Pinned by ``tests/test_ui_confidential_titles_gate.py``, which discovers both
    GET routes.
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
    payload["result"] = lensed_result(
        snap.result, serve_confidential_titles=serve_confidential_titles
    )
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


#: ``after`` is a seq number; 18 digits is far past any real one, and the bound
#: keeps ``int()`` away from CPython's 4300-digit conversion limit (a 500).
_MAX_AFTER_DIGITS = 18


def _after(request: Request) -> int:
    raw = request.query_params.get("after", "0")
    # isascii(): str.isdigit() is true for "²", which int() rejects.
    if not (raw.isascii() and raw.isdigit() and len(raw) <= _MAX_AFTER_DIGITS):
        raise UiBadRequest("after must be a non-negative integer", code="invalid_after")
    return int(raw)


async def list_jobs(request: Request) -> JSONResponse:
    """Recent jobs, newest first, without their event logs or results."""
    ctx = context_of(request)
    jobs = require_jobs(ctx)
    titles = ctx.serve_confidential_titles
    return ok(
        {
            "jobs": [
                snapshot_payload(s, serve_confidential_titles=titles, listing=True)
                for s in jobs.list_recent()
            ]
        }
    )


async def get_job(request: Request) -> JSONResponse:
    """One job, with only the events whose ``seq > after``."""
    ctx = context_of(request)
    jobs = require_jobs(ctx)
    after = _after(request)
    try:
        snap = jobs.get(request.path_params["job_id"], after=after)
    except JobUnknown as exc:
        raise _unknown() from exc
    return ok(snapshot_payload(snap, serve_confidential_titles=ctx.serve_confidential_titles))


async def cancel_job(request: Request) -> JSONResponse:
    """200 when cancelled or already terminal; 202 while a running job winds down."""
    ctx = context_of(request)
    jobs, _runtime = require_ingest(ctx)
    await json_body(request)  # the middleware requires a JSON body; `{}` is expected
    try:
        snap = jobs.cancel(request.path_params["job_id"])
    except JobUnknown as exc:
        raise _unknown() from exc
    except NotCancellable as exc:
        raise UiConflict(
            "writing has started; the job will finish", code="not_cancellable"
        ) from exc
    return ok(
        snapshot_payload(snap, serve_confidential_titles=ctx.serve_confidential_titles),
        status=202 if snap.state == "running" else 200,
    )
