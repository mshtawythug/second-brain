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

import uuid
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
from ..errors import ElicitError
from ..vault import create_vault_note
from ..vault.paths import safe_wikilink_alias, strip_md_extension
from .drafter import Drafter
from .schema import ElicitDraft, ElicitOutcome, Gap

InputFn = Callable[[], str]
EditFn = Callable[..., "tuple[dict[str, Any], str]"]

_MENU = "[e] edit & save   [s] skip   [n] snooze N days   [q] quit"
_EVIDENCE_PREVIEW = 3

# Target types whose ``target_id`` is a real ``graph_entities`` UUID (so the
# source entity's display name can be resolved). ``user_flagged`` gaps may
# carry a raw-string ``target_id`` that is not a UUID, so resolution is always
# guarded by a UUID parse regardless of target_type.
_ENTITY_TARGET_TYPES = frozenset({"person", "org", "project", "topic", "tool"})


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
    target_label = gap.target_name or gap.target_id
    if target_label:
        typer.echo(f"  about: {target_label}")
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
            return _skip(conn, gap, tenant_id=tenant_id)
        if choice == "n":
            return _snooze(conn, gap, input_fn, tenant_id=tenant_id)
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


def _assert_one_row(cur: psycopg.Cursor[Any], gap: Gap, action: str) -> None:
    """Fail loud when a status UPDATE didn't hit exactly one tenant-owned row.

    A ``rowcount != 1`` means the gap id either doesn't exist or belongs to a
    different tenant — a silent no-op would hide that, so raise instead.
    """
    if cur.rowcount != 1:
        raise ElicitError(
            f"{action} affected {cur.rowcount} rows for gap {gap.gap_id} "
            "(gap missing or owned by another tenant)"
        )


def _skip(
    conn: psycopg.Connection[Any], gap: Gap, *, tenant_id: str
) -> ElicitOutcome:
    """Dismiss the gap so it drops out of the open queue."""
    cur = conn.execute(
        "UPDATE elicitation_gaps SET status='dismissed', updated_at=now() "
        "WHERE id=%s AND tenant_id=%s",
        (gap.gap_id, tenant_id),
    )
    _assert_one_row(cur, gap, "dismiss")
    typer.echo("Skipped.")
    return ElicitOutcome(gap_id=gap.gap_id, action="dismissed")


def _snooze(
    conn: psycopg.Connection[Any], gap: Gap, input_fn: InputFn, *, tenant_id: str
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
    cur = conn.execute(
        "UPDATE elicitation_gaps SET status='snoozed', "
        "snoozed_until=now() + make_interval(days => %s), updated_at=now() "
        "WHERE id=%s AND tenant_id=%s",
        (days, gap.gap_id, tenant_id),
    )
    _assert_one_row(cur, gap, "snooze")
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
    footer = _build_source_footer(conn, gap, tenant_id=tenant_id)
    note_body = f"{body}{footer}" if footer else body
    note_id = create_vault_note(
        conn,
        cfg=cfg,
        vault_path=vault_path,
        title=draft.title,
        body=note_body,
        tags=_note_tags(gap),
    )
    cur = conn.execute(
        "UPDATE elicitation_gaps SET status='resolved', resolved_note_id=%s::uuid, "
        "updated_at=now() WHERE id=%s AND tenant_id=%s",
        (note_id, gap.gap_id, tenant_id),
    )
    _assert_one_row(cur, gap, "resolve")
    return note_id


def _note_tags(gap: Gap) -> list[str]:
    """Canonical tag set for an elicited rule note."""
    return ["tacit", gap.signal_kind]


def _build_source_footer(
    conn: psycopg.Connection[Any], gap: Gap, *, tenant_id: str
) -> str:
    """Build a grounded ``## Source`` footer wiki-linking the gap's provenance.

    Spec §1/§4.5: codified rules are "wikilinked to their source entity." We
    resolve only REAL data so no link is ever guessed (repo rule: never invent
    wiki-link targets):

    - **Evidence:** the gap's evidence documents are resolved to their actual
      ``documents.vault_path`` (rows that exist AND have been exported to the
      vault). Each is rendered as a path-form wiki-link
      ``[[<vault-path-no-md>|<title>]]`` — the same shape the derived-links
      fence / People Hub emit and that
      :func:`brain.vault.resolver._resolve_by_vault_path` matches, so the link
      is guaranteed to resolve.
    - **Source entity:** for an entity-typed gap whose ``target_id`` is a real
      entity UUID, the entity's display name is included as PLAIN TEXT (no
      wiki-link — we can't confirm a vault page exists for the entity, and a
      broken link is worse than plain text).

    Returns ``""`` when neither an entity name nor any evidence vault_path
    resolves, so the caller appends nothing rather than an empty section.
    """
    lines: list[str] = []

    entity_name = _resolve_entity_name(conn, gap, tenant_id=tenant_id)
    if entity_name:
        lines.append(f"Elicited from: {entity_name}")

    evidence_links = _resolve_evidence_links(conn, gap)
    if evidence_links:
        if lines:
            lines.append("")
        lines.append("Evidence:")
        lines.extend(f"- {link}" for link in evidence_links)

    if not lines:
        return ""
    return "\n\n## Source\n\n" + "\n".join(lines) + "\n"


def _resolve_entity_name(
    conn: psycopg.Connection[Any], gap: Gap, *, tenant_id: str
) -> str | None:
    """Return the source entity's display name, or ``None`` if not resolvable.

    Guarded so a ``user_flagged`` gap with a raw-string ``target_id`` (not a
    UUID) is skipped gracefully rather than raising on the UUID cast.
    """
    if gap.target_type not in _ENTITY_TARGET_TYPES:
        return None
    try:
        uuid.UUID(gap.target_id)
    except (ValueError, AttributeError, TypeError):
        return None
    row = conn.execute(
        "SELECT name FROM graph_entities WHERE id = %s::uuid AND tenant_id = %s",
        (gap.target_id, tenant_id),
    ).fetchone()
    if row is None or not row[0]:
        return None
    return str(row[0])


def _resolve_evidence_links(
    conn: psycopg.Connection[Any], gap: Gap
) -> list[str]:
    """Render the gap's evidence docs as resolvable path-form wiki-links.

    Only evidence ids that parse as UUIDs are queried (non-UUID placeholders
    are skipped, never cast), and only rows with a non-null ``vault_path`` are
    rendered — so every emitted link points at a real, exported file.
    """
    valid_ids: list[str] = []
    for ev in gap.evidence_ids:
        try:
            uuid.UUID(str(ev))
        except (ValueError, AttributeError, TypeError):
            continue
        valid_ids.append(str(ev))
    if not valid_ids:
        return []
    rows = conn.execute(
        "SELECT vault_path, title FROM documents "
        "WHERE id = ANY(%s::uuid[]) AND vault_path IS NOT NULL "
        "ORDER BY vault_path",
        (valid_ids,),
    ).fetchall()
    links: list[str] = []
    for vault_path, title in rows:
        target = strip_md_extension(str(vault_path))
        if title:
            links.append(f"[[{target}|{safe_wikilink_alias(str(title))}]]")
        else:
            links.append(f"[[{target}]]")
    return links
