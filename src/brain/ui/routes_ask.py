"""`/api/ask` — the Agent tab's config and the ask submit (RM-18a; follow-ups RM-18c).

Thin: gate → capped read → validate → submit. A follow-up's ``history`` is
untrusted: this module checks its SHAPE only, and ``brain.ask.ask`` applies the
3-turn, 2,000-token cap itself (``brain.ask_history``), so no caller reaches the
plan prompt with more. Status and cancel reuse
``/api/jobs/{id}`` and ``/api/jobs/{id}/cancel`` (``routes_jobs``); an ask job is
never listed there (A10).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..ask_history import MAX_HISTORY_ANSWER_CHARS, MAX_HISTORY_TURNS, AskTurn
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

#: The cap ON THE WIRE. JSON escaping can grow a string up to 12 bytes a
#: character (an astral character is two ``\uXXXX`` escapes): the question plus
#: three maximal earlier turns (RM-18c), and 2 KiB for the rest of the object.
#: Over it: 413 before any parse. More turns than three fit only when they are
#: short, and the cap in ``ask`` drops the oldest.
MAX_ASK_WIRE_BYTES = (
    12 * (MAX_QUESTION_CHARS + MAX_HISTORY_TURNS * (MAX_QUESTION_CHARS + MAX_HISTORY_ANSWER_CHARS))
    + 2048
)

#: The fields an ask request may carry. Anything else is a 400, so a client that
#: believes it chose a mode is told it did not.
_ASK_FIELDS = frozenset({"question", "no_loop", "history"})
_TURN_FIELDS = frozenset({"question", "answer"})


async def ask_config(request: Request) -> JSONResponse:
    """What the tab renders from: whether it may ask, why not, and the limits."""
    reason = ask_unavailable_reason(context_of(request))
    return ok(
        {
            "enabled": reason is None,
            "reason": reason,
            "max_question_chars": MAX_QUESTION_CHARS,
            "max_history_turns": MAX_HISTORY_TURNS,
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
    history = parse_history(payload.get("history", []))
    if no_loop and history:
        # ask_no_loop has no plan step, the only step that reads history (RM-18c).
        raise UiBadRequest(
            "a follow-up needs the planning loop; send no_loop false",
            code="history_requires_loop",
        )
    return AskSpec(question=question, no_loop=no_loop, history=history)


def parse_history(raw: Any) -> tuple[AskTurn, ...]:
    """A follow-up's earlier turns, oldest first, shape-checked (RM-18c).

    A list of ``{question, answer}`` objects, each a non-empty UTF-8 string within
    its character cap. The count and token caps are ``ask``'s (oldest dropped).
    """
    if not isinstance(raw, list):
        raise UiBadRequest("history must be a list of turns", code="invalid_history")
    return tuple(_parse_turn(item) for item in raw)


def _parse_turn(item: Any) -> AskTurn:
    if not isinstance(item, dict) or set(item) != _TURN_FIELDS:
        raise UiBadRequest(
            "each history turn must be {question, answer}", code="invalid_history"
        )
    question, answer = item["question"], item["answer"]
    for text in (question, answer):
        if not isinstance(text, str) or not text.strip() or not encodes_utf8(text):
            raise UiBadRequest(
                "each history turn needs a non-empty question and answer",
                code="invalid_history",
            )
    if len(question) > MAX_QUESTION_CHARS or len(answer) > MAX_HISTORY_ANSWER_CHARS:
        raise UiBadRequest(
            f"a history turn's question is at most {MAX_QUESTION_CHARS} characters "
            f"and its answer at most {MAX_HISTORY_ANSWER_CHARS}",
            code="history_turn_too_long",
        )
    return AskTurn(question=question.strip(), answer=answer.strip())
