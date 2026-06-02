"""Interactive elicitation session: draft each gap, let the user correct & codify.

The loop is the human-in-the-loop core of ``brain elicit``: for every queued
:class:`~brain.elicit.schema.Gap` it asks the injected :class:`Drafter` for a
confident first draft, presents it, and routes the user's keypress to one of
four terminal actions — edit & save (codify a vault note), skip (dismiss),
snooze N days, or quit. Every collaborator that touches the outside world
(the editor, stdin, the drafter) is injected so the loop is fully
deterministic under test. This module MUST NOT import :mod:`brain.cli` — the
CLI imports the session, never the reverse.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import typer

from ..config import Config
from ..edit_session import (
    EditorAbortedError,
    EditorParseFailedError,
    EditorUnchangedError,
    build_payload,
)
from ..edit_session import run_editor_session as _run_editor_session
from ..vault import create_vault_note
from .drafter import Drafter
from .schema import ElicitDraft, ElicitOutcome, Gap

InputFn = Callable[[], str]
EditFn = Callable[..., "tuple[dict[str, Any], str]"]

_MENU = "[e] edit & save   [s] skip   [n] snooze N days   [q] quit"
_EVIDENCE_PREVIEW = 3


def _default_input() -> str:
    """Production input source: prompt the user for a single line via Typer."""
    return str(typer.prompt("›", prompt_suffix=" "))


def run_session(
    cfg: Config,
    conn: psycopg.Connection[Any],
    *,
    drafter: Drafter,
    gaps: list[Gap],
    tenant_id: str,
    vault_path: Path | None,
    input_fn: InputFn = _default_input,
    edit_fn: EditFn = _run_editor_session,
) -> list[ElicitOutcome]:
    """Drive the interactive review loop over ``gaps``; return what happened.

    For each gap: draft → present → prompt. ``e`` codifies the corrected rule
    into a vault note (outcome ``accepted``); ``s`` dismisses the gap (outcome
    ``dismissed``); ``n`` snoozes it for N days (outcome ``snoozed``); ``q``
    stops the loop early, returning the outcomes accumulated so far.

    The connection is expected to be in autocommit mode so each status
    transition (and any authored note) persists immediately — a quit midway
    must not roll back already-resolved gaps.
    """
    outcomes: list[ElicitOutcome] = []
    for gap in gaps:
        draft = drafter.draft(conn, gap, tenant_id=tenant_id)
        _present(gap, draft)
        outcome = _handle_gap(
            cfg,
            conn,
            gap=gap,
            draft=draft,
            tenant_id=tenant_id,
            vault_path=vault_path,
            input_fn=input_fn,
            edit_fn=edit_fn,
        )
        if outcome is None:  # user quit
            break
        outcomes.append(outcome)
    return outcomes


def _present(gap: Gap, draft: ElicitDraft) -> None:
    """Print the gap's draft rule, rationale, and a few evidence references."""
    typer.echo("")
    typer.echo(f"▸ {draft.title}")
    if gap.rationale:
        typer.echo(f"  why:   {gap.rationale}")
    typer.echo(f"  draft: {draft.draft_text}")
    if draft.evidence_ids:
        preview = ", ".join(draft.evidence_ids[:_EVIDENCE_PREVIEW])
        typer.echo(f"  evidence: {preview}")
    if draft.evidence_texts:
        excerpt = draft.evidence_texts[0].strip().replace("\n", " ")
        typer.echo(f"  excerpt: {excerpt[:160]}")


def _handle_gap(
    cfg: Config,
    conn: psycopg.Connection[Any],
    *,
    gap: Gap,
    draft: ElicitDraft,
    tenant_id: str,
    vault_path: Path | None,
    input_fn: InputFn,
    edit_fn: EditFn,
) -> ElicitOutcome | None:
    """Prompt until the user makes a terminal choice; ``None`` means quit."""
    while True:
        typer.echo(_MENU)
        choice = input_fn().strip().lower()
        if choice == "q":
            return None
        if choice == "s":
            return _skip(conn, gap)
        if choice == "n":
            return _snooze(conn, gap, input_fn)
        if choice == "e":
            outcome = _edit_and_save(
                cfg,
                conn,
                gap=gap,
                draft=draft,
                tenant_id=tenant_id,
                vault_path=vault_path,
                edit_fn=edit_fn,
            )
            if outcome is not None:
                return outcome
            continue  # empty / unchanged / aborted edit — re-prompt
        typer.echo("Unrecognized choice; pick [e]dit, [s]kip, [n]snooze, or [q]uit.")


def _skip(conn: psycopg.Connection[Any], gap: Gap) -> ElicitOutcome:
    """Dismiss the gap so it drops out of the open queue."""
    conn.execute(
        "UPDATE elicitation_gaps SET status='dismissed', updated_at=now() WHERE id=%s",
        (gap.gap_id,),
    )
    typer.echo("Skipped.")
    return ElicitOutcome(gap_id=gap.gap_id, action="dismissed")


def _snooze(
    conn: psycopg.Connection[Any], gap: Gap, input_fn: InputFn
) -> ElicitOutcome:
    """Snooze the gap for a user-supplied number of days (>= 1)."""
    typer.echo("Snooze for how many days?")
    while True:
        raw = input_fn().strip()
        try:
            days = int(raw)
        except ValueError:
            typer.echo("Please enter a whole number of days (>= 1).")
            continue
        if days < 1:
            typer.echo("Days must be >= 1.")
            continue
        break
    conn.execute(
        "UPDATE elicitation_gaps SET status='snoozed', "
        "snoozed_until=now() + make_interval(days => %s), updated_at=now() "
        "WHERE id=%s",
        (days, gap.gap_id),
    )
    typer.echo(f"Snoozed for {days} day(s).")
    return ElicitOutcome(gap_id=gap.gap_id, action="snoozed", snoozed_days=days)


def _edit_and_save(
    cfg: Config,
    conn: psycopg.Connection[Any],
    *,
    gap: Gap,
    draft: ElicitDraft,
    tenant_id: str,
    vault_path: Path | None,
    edit_fn: EditFn,
) -> ElicitOutcome | None:
    """Open the editor on the draft; codify the corrected body into a note.

    Returns ``None`` (so the caller re-prompts) when the user makes no usable
    change — an empty body, a body identical to the draft, an aborted editor,
    or a payload that never parsed.
    """
    initial_text = build_payload(
        title=draft.title,
        content_type="note",
        tags=_note_tags(gap),
        metadata={
            "gap_id": gap.gap_id,
            "signal_kind": gap.signal_kind,
            "evidence_ids": list(gap.evidence_ids),
        },
        body=draft.draft_text,
    )
    try:
        _header, body = edit_fn(initial_text, doc_id_label=gap.gap_id)
    except EditorUnchangedError:
        typer.echo("No changes made — [e]dit again or [s]kip?")
        return None
    except (EditorAbortedError, EditorParseFailedError) as exc:
        typer.echo(f"Edit not saved ({exc}) — [e]dit again or [s]kip?")
        return None

    body = body.strip()
    if not body or body == draft.draft_text.strip():
        typer.echo("Draft unchanged — [e]dit again or [s]kip?")
        return None

    note_id = _codify(
        cfg,
        conn,
        vault_path=vault_path,
        gap=gap,
        draft=draft,
        body=body,
        tenant_id=tenant_id,
    )
    typer.echo(f"Saved rule → {note_id}")
    return ElicitOutcome(gap_id=gap.gap_id, action="accepted", note_id=note_id)


def _codify(
    cfg: Config,
    conn: psycopg.Connection[Any],
    *,
    vault_path: Path | None,
    gap: Gap,
    draft: ElicitDraft,
    body: str,
    tenant_id: str,
) -> str:
    """Author the corrected rule as a vault note and mark the gap resolved.

    Deliberately writes NO ``interactions`` row — that table is append-only
    with a fixed action vocabulary and has no value for "rule codified"; the
    resolved ``elicitation_gaps`` row (with ``resolved_note_id``) is the
    durable record.
    """
    if vault_path is None:
        raise ValueError("vault_path is required to codify an elicited rule")
    note_id = create_vault_note(
        conn,
        cfg=cfg,
        vault_path=vault_path,
        title=draft.title,
        body=body,
        tags=_note_tags(gap),
    )
    conn.execute(
        "UPDATE elicitation_gaps SET status='resolved', resolved_note_id=%s::uuid, "
        "updated_at=now() WHERE id=%s",
        (note_id, gap.gap_id),
    )
    return note_id


def _note_tags(gap: Gap) -> list[str]:
    """Canonical tag set for an elicited rule note."""
    return ["tacit", gap.signal_kind]
