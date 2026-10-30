"""`/api/ask` — the Agent tab's config and the ask submit (RM-18a).

Thin: gate → capped read → validate → submit. Status and cancel reuse
``/api/jobs/{id}`` and ``/api/jobs/{id}/cancel`` (``routes_jobs``); an ask job is
never listed there (A10).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._http import context_of, ok, parse_json_object, read_body_capped
from .ask_service import (
    ASK_MODES,
    MAX_QUESTION_CHARS,
    AskSpec,
    ask_unavailable_reason,
    require_ask,
    submit_ask,
)
from .errors import UiBadRequest
from .ingest_schemas import encodes_utf8
from .routes_ingest import queue_errors
from .routes_jobs import job_payload

#: The cap ON THE WIRE. JSON escaping can grow a question up to 12 bytes a
#: character (an astral character is two ``\uXXXX`` escapes); 1 KiB covers the
#: rest of the object. Over it: 413 before any parse.
MAX_ASK_WIRE_BYTES = 12 * MAX_QUESTION_CHARS + 1024

#: The fields an ask request may carry. Anything else is a 400, so a client that
#: believes it chose a mode, or sent history (RM-18c), is told it did not.
_ASK_FIELDS = frozenset({"question", "no_loop"})


async def ask_config(request: Request) -> JSONResponse:
    """What the tab renders from: whether it may ask, why not, and the limits."""
    reason = ask_unavailable_reason(context_of(request))
    return ok(
        {
            "enabled": reason is None,
            "reason": reason,
            "max_question_chars": MAX_QUESTION_CHARS,
            "modes": list(ASK_MODES),
        }
    )


async def submit(request: Request) -> JSONResponse:
    """Queue an ask: 202 with the job's snapshot."""
    ctx = context_of(request)
    jobs = require_ask(ctx)
    raw = await read_body_capped(request, MAX_ASK_WIRE_BYTES)
    spec = parse_ask(parse_json_object(raw))
    with queue_errors():
        snap = submit_ask(ctx, jobs, spec)
    return ok(job_payload(ctx, snap), status=202)


def parse_ask(payload: Mapping[str, Any]) -> AskSpec:
    """Validate an ask request body. Raises 400 at submit, so it is never a failed job."""
    unknown = sorted(set(payload) - _ASK_FIELDS)
    if unknown:
        raise UiBadRequest(f"unknown field(s): {', '.join(unknown)}", code="unknown_field")
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip() or not encodes_utf8(question):
        raise UiBadRequest("question must be non-empty UTF-8 text", code="invalid_question")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise UiBadRequest(
            f"question must be at most {MAX_QUESTION_CHARS} characters",
            code="question_too_long",
        )
    no_loop = payload.get("no_loop", False)
    if not isinstance(no_loop, bool):
        raise UiBadRequest("no_loop must be true or false", code="invalid_no_loop")
    return AskSpec(question=question, no_loop=no_loop)
