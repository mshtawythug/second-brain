"""Append-only interaction log writes (one row per user event).

Each row in ``interactions`` records one feedback event (open / rate /
pin / click) tied to a single document. Q1-C populates this table from
two surfaces:

- CLI: ``brain rate <id> useful|irrelevant`` → source='cli', action one
  of ``rated_useful`` / ``rated_irrelevant``, ``session_id=NULL``.
- MCP: ``brain_show(..., originating_query=...)`` → source='mcp',
  action='opened', ``query=originating_query``, optional
  ``session_id`` minted by a prior ``brain_search`` call.

The wiki click surface (source='wiki') is reserved in the schema for a
future wave; this module accepts it today so the deferred surface needs
no additional migration.

Per plan §3.a, the writer is intentionally narrow: callers go through
:func:`record_interaction` instead of issuing INSERTs directly so the
Python-side enum validation can produce a clean :class:`InteractionError`
ahead of the authoritative DB-level ``CHECK`` constraints.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg

from .errors import InteractionError

InteractionAction = Literal[
    "clicked", "opened", "rated_useful", "rated_irrelevant", "pinned"
]
InteractionSource = Literal["cli", "mcp", "wiki"]


# Python-side enum gates — kept in sync with the SQL ``CHECK`` constraints
# on ``interactions.action`` / ``interactions.source`` (migration 010).
# Adding a new value here without the matching migration would silently
# bypass the Python gate but still trip the SQL ``CHECK``; adding to the
# SQL side without updating these sets would let a Python caller pass an
# enum value the DB rejects at INSERT time. Keep both in lockstep.
_VALID_ACTIONS: frozenset[str] = frozenset({
    "clicked", "opened", "rated_useful", "rated_irrelevant", "pinned",
})
_VALID_SOURCES: frozenset[str] = frozenset({"cli", "mcp", "wiki"})


@dataclass(frozen=True)
class InteractionRow:
    """Read-back projection of one ``interactions`` row.

    Used by tests + a future ``brain interactions show <doc>`` command
    (not exposed in Q1-C — the table is write-only from the user's
    perspective for now). ``id`` and ``document_id`` are stringified
    UUIDs so callers don't need to import ``uuid`` just to compare.
    """

    id: str
    document_id: str
    query: str | None
    action: str
    source: str
    session_id: str | None
    at: datetime


def record_interaction(
    conn: psycopg.Connection[Any],
    *,
    document_id: str,
    action: InteractionAction,
    source: InteractionSource,
    query: str | None = None,
    session_id: uuid.UUID | None = None,
) -> str:
    """INSERT one row into ``interactions`` and return its UUID as text.

    Validates ``action`` / ``source`` against the Python-side enums before
    issuing the SQL so an obvious typo produces an
    :class:`brain.errors.InteractionError` rather than a generic
    :class:`psycopg.errors.CheckViolation`. The DB-level ``CHECK``
    constraints remain the authoritative gate; this is belt-and-braces.

    Args:
        conn: Live Postgres connection. The caller controls the
            transaction — for CLI / MCP we run with ``autocommit=True``
            so each interaction is one round-trip.
        document_id: Stringified UUID of the doc the event applies to.
            Foreign-key enforced; deleting the doc cascades to its
            interaction rows.
        action: One of :data:`_VALID_ACTIONS`. Type-narrowed by
            :data:`InteractionAction` for static checkers.
        source: One of :data:`_VALID_SOURCES`. Type-narrowed by
            :data:`InteractionSource` for static checkers.
        query: Optional originating query (the search string that led
            the user to this doc). ``None`` is valid for surfaces with
            no query intent (e.g., a direct CLI ``brain show``).
        session_id: Optional UUID grouping a search-then-open pair.
            ``None`` for the CLI rating path; populated by MCP when the
            client passes back the id returned from ``brain_search``.

    Returns:
        The inserted row's UUID as a string.

    Raises:
        InteractionError: ``action`` or ``source`` is not a recognised
            enum value.
        psycopg.Error: The INSERT itself failed (FK violation, DB
            outage, etc.) — propagated unchanged so the CLI / MCP outer
            wrappers can surface it with their framework's error type.
    """
    if action not in _VALID_ACTIONS:
        raise InteractionError(f"unknown action: {action!r}")
    if source not in _VALID_SOURCES:
        raise InteractionError(f"unknown source: {source!r}")
    row = conn.execute(
        """
        INSERT INTO interactions (document_id, query, action, source, session_id)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id::text
        """,
        (
            document_id,
            query,
            action,
            source,
            str(session_id) if session_id is not None else None,
        ),
    ).fetchone()
    assert row is not None  # RETURNING always yields one row
    return str(row[0])
