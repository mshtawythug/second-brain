"""`/api/notes` — read, create, update, draft, move, delete.

Thin by construction: parse → :mod:`brain.ui.notes_service` → serialize. No SQL,
no filesystem access, no business rules. Every mutation route declares its
methods explicitly so Starlette answers 405 for anything else — which is what
makes "a GET can never mutate" a property of the route table rather than a
convention.
"""
from __future__ import annotations

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import notes_service, telemetry
from ._http import context_of, db_guard, json_body, ok
from .errors import UiBadRequest
from .schemas import parse_note_create, parse_note_patch, require_confirm


async def get_note(request: Request) -> JSONResponse:
    """Fetch one note by id prefix (6+ hex characters) or full UUID."""
    ctx = context_of(request)
    prefix = request.path_params["id_prefix"]
    session_id = telemetry.parse_session_id(request.query_params.get("session_id"))
    try:
        with ctx.connect() as conn:
            document_id = notes_service.resolve_id(conn, prefix)
            payload = notes_service.read_note(ctx, conn, document_id)
            telemetry.record_ui_open(
                conn,
                enabled=ctx.logging_enabled,
                document_id=document_id,
                query=request.query_params.get("q"),
                session_id=session_id,
            )
    except psycopg.Error as exc:
        raise db_guard(exc) from exc
    return ok(payload)


async def create_note(request: Request) -> JSONResponse:
    """Create a vault-tier note."""
    ctx = context_of(request)
    spec = parse_note_create(await json_body(request))
    try:
        with ctx.connect() as conn:
            payload = notes_service.create_note(ctx, conn, spec)
    except psycopg.Error as exc:
        raise db_guard(exc) from exc
    return ok(payload, status=201)


async def update_note(request: Request) -> JSONResponse:
    """Save an edit. Requires ``body_hash``; 409s when it is stale."""
    ctx = context_of(request)
    patch = parse_note_patch(await json_body(request))
    try:
        with ctx.connect() as conn:
            document_id = notes_service.resolve_id(conn, request.path_params["id_prefix"])
            payload = notes_service.update_note(ctx, conn, document_id, patch)
    except psycopg.Error as exc:
        raise db_guard(exc) from exc
    return ok(payload)


async def set_draft(request: Request) -> JSONResponse:
    """Toggle draft ↔ published."""
    ctx = context_of(request)
    body = await json_body(request)
    draft = body.get("draft")
    if not isinstance(draft, bool):
        raise UiBadRequest("draft must be true or false", code="invalid_draft")
    try:
        with ctx.connect() as conn:
            document_id = notes_service.resolve_id(conn, request.path_params["id_prefix"])
            payload = notes_service.set_draft(ctx, conn, document_id, draft=draft)
    except psycopg.Error as exc:
        raise db_guard(exc) from exc
    return ok(payload)


async def move_note(request: Request) -> JSONResponse:
    """Rename and/or move a vault-tier note. Requires ``confirm``."""
    ctx = context_of(request)
    body = await json_body(request)
    require_confirm(body)
    new_title = (body.get("new_title") or "").strip() or None
    new_folder = body.get("new_folder")
    new_folder = new_folder.strip().strip("/") if isinstance(new_folder, str) else None
    if new_title is None and new_folder is None:
        raise UiBadRequest(
            "provide new_title and/or new_folder", code="nothing_to_move"
        )
    try:
        with ctx.connect() as conn:
            document_id = notes_service.resolve_id(conn, request.path_params["id_prefix"])
            payload = notes_service.move_note(
                ctx, conn, document_id, new_title=new_title, new_folder=new_folder
            )
    except psycopg.Error as exc:
        raise db_guard(exc) from exc
    return ok(payload)


async def delete_note(request: Request) -> JSONResponse:
    """Delete a document. Requires ``confirm`` **and** a matching title.

    The title is re-read and compared server-side; see
    :func:`brain.ui.notes_service.delete_note` for why that is not a UI
    courtesy.
    """
    ctx = context_of(request)
    body = await json_body(request)
    require_confirm(body)
    expected = body.get("expected_title")
    if not isinstance(expected, str) or not expected.strip():
        raise UiBadRequest(
            "expected_title must exactly match the document's current title",
            code="expected_title_required",
        )
    try:
        with ctx.connect() as conn:
            document_id = notes_service.resolve_id(conn, request.path_params["id_prefix"])
            payload = notes_service.delete_note(
                ctx, conn, document_id, expected_title=expected
            )
    except psycopg.Error as exc:
        raise db_guard(exc) from exc
    return ok(payload)
