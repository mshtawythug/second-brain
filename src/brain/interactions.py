"""Append-only interaction log writes (one row per user event).

Each row in ``interactions`` records one feedback event (open / rate /
pin / click). Q1-C populated this table from two DOCUMENT surfaces:

- CLI: ``brain rate <id> useful|irrelevant`` → source='cli', action one
  of ``rated_useful`` / ``rated_irrelevant``, ``session_id=NULL``.
- MCP: ``brain_show(..., originating_query=...)`` → source='mcp',
  action='opened', ``query=originating_query``, optional
  ``session_id`` minted by a prior ``brain_search`` call.

The wiki click surface (source='wiki') is reserved in the schema for a
future wave; this module accepts it today so the deferred surface needs
no additional migration.

G4-a (migration 015, spec §17d Q2) generalizes the writer so the graph
surfaces — entity / community / theme — become FIRST-CLASS rateable
targets. A row now targets EITHER a document (``document_id``) OR a graph
target (``target_type`` + ``target_id``), never both and never neither —
the XOR enforced both here (Python boundary) and by the authoritative DB
``CHECK``. ``graph_retrieved`` is a PROVENANCE flag (a graph surface
produced this row), independent of the target shape: a document row
surfaced via a graph path is still a document row with
``graph_retrieved=True``. No new ``source`` value is introduced.

Per plan §3.a, the writer is intentionally narrow: callers go through
:func:`record_interaction` instead of issuing INSERTs directly so the
Python-side enum + XOR validation can produce a clean
:class:`InteractionError` ahead of the authoritative DB-level ``CHECK``
constraints.
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
InteractionTargetType = Literal["entity", "community", "theme"]


# Python-side enum gates — kept in sync with the SQL ``CHECK`` constraints
# on ``interactions.action`` / ``interactions.source`` (migration 010) and
# ``interactions.target_type`` (migration 015).
# Adding a new value here without the matching migration would silently
# bypass the Python gate but still trip the SQL ``CHECK``; adding to the
# SQL side without updating these sets would let a Python caller pass an
# enum value the DB rejects at INSERT time. Keep both in lockstep.
_VALID_ACTIONS: frozenset[str] = frozenset({
    "clicked", "opened", "rated_useful", "rated_irrelevant", "pinned",
})
_VALID_SOURCES: frozenset[str] = frozenset({"cli", "mcp", "wiki"})
_VALID_TARGET_TYPES: frozenset[str] = frozenset({"entity", "community", "theme"})


@dataclass(frozen=True)
class InteractionRow:
    """Read-back projection of one ``interactions`` row.

    Used by tests + a future ``brain interactions show`` command (not
    exposed yet — the table is write-only from the user's perspective for
    now). ``id`` and ``document_id`` are stringified UUIDs so callers
    don't need to import ``uuid`` just to compare; ``document_id`` is
    ``None`` for graph-target rows (G4-a). ``target_type`` / ``target_id``
    are set (and ``document_id`` ``None``) for graph-target rows, mutually
    exclusive with ``document_id`` per the XOR. ``graph_retrieved`` is the
    provenance flag, orthogonal to the target shape.
    """

    id: str
    document_id: str | None
    query: str | None
    action: str
    source: str
    session_id: str | None
    at: datetime
    target_type: str | None = None
    target_id: str | None = None
    graph_retrieved: bool = False


def record_interaction(
    conn: psycopg.Connection[Any],
    *,
    document_id: str | None = None,
    action: InteractionAction,
    source: InteractionSource,
    query: str | None = None,
    session_id: uuid.UUID | None = None,
    target_type: InteractionTargetType | None = None,
    target_id: str | None = None,
    graph_retrieved: bool = False,
) -> str:
    """INSERT one row into ``interactions`` and return its UUID as text.

    Validates ``action`` / ``source`` / ``target_type`` against the
    Python-side enums and enforces the document-XOR-graph-target shape
    before issuing the SQL, so an obvious mistake produces a clean
    :class:`brain.errors.InteractionError` rather than a generic
    :class:`psycopg.errors.CheckViolation`. The DB-level ``CHECK``
    constraints remain the authoritative gate; this is belt-and-braces.

    A row targets EITHER a document OR a graph target, never both and
    never neither (migration 015 / spec §17d Q2):

    - Document row: pass ``document_id``; leave ``target_type`` /
      ``target_id`` unset. This is the unchanged Q1-C path used by
      ``brain rate`` and MCP ``brain_show``.
    - Graph-target row: pass BOTH ``target_type`` and ``target_id``;
      leave ``document_id`` unset.

    Args:
        conn: Live Postgres connection. The caller controls the
            transaction — for CLI / MCP we run with ``autocommit=True``
            so each interaction is one round-trip.
        document_id: Stringified UUID of the doc the event applies to.
            Foreign-key enforced; deleting the doc cascades to its
            interaction rows. ``None`` (the default) for graph-target rows.
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
        target_type: One of :data:`_VALID_TARGET_TYPES` ('entity' /
            'community' / 'theme') for a graph-target row; ``None`` for a
            document row. Must be paired with ``target_id``.
        target_id: Durable id of the graph target (entity UUID /
            community_key / theme key) as text; ``None`` for a document
            row. Must be paired with ``target_type``.
        graph_retrieved: Provenance flag — ``True`` when a graph surface
            produced this interaction. Orthogonal to the target shape: a
            document row surfaced via a graph path is still a document row
            with ``graph_retrieved=True``.

    Returns:
        The inserted row's UUID as a string.

    Raises:
        InteractionError: ``action`` / ``source`` / ``target_type`` is not
            a recognised enum value, or the document-XOR-graph-target shape
            is violated (both set, neither set, or a half-specified graph
            target).
        psycopg.Error: The INSERT itself failed (FK violation, DB
            outage, etc.) — propagated unchanged so the CLI / MCP outer
            wrappers can surface it with their framework's error type.
    """
    if action not in _VALID_ACTIONS:
        raise InteractionError(f"unknown action: {action!r}")
    if source not in _VALID_SOURCES:
        raise InteractionError(f"unknown source: {source!r}")
    if target_type is not None and target_type not in _VALID_TARGET_TYPES:
        raise InteractionError(f"unknown target_type: {target_type!r}")

    has_document = document_id is not None
    has_target = target_type is not None or target_id is not None
    if has_document and has_target:
        raise InteractionError(
            "interaction must target EITHER a document or a graph target, "
            "not both (document_id is mutually exclusive with "
            "target_type/target_id)"
        )
    if not has_document and not has_target:
        raise InteractionError(
            "interaction must target either a document (document_id) or a "
            "graph target (target_type + target_id)"
        )
    if has_target and (target_type is None or target_id is None):
        raise InteractionError(
            "graph-target interaction requires BOTH target_type and target_id"
        )

    row = conn.execute(
        """
        INSERT INTO interactions
            (document_id, query, action, source, session_id,
             target_type, target_id, graph_retrieved)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id::text
        """,
        (
            document_id,
            query,
            action,
            source,
            str(session_id) if session_id is not None else None,
            target_type,
            target_id,
            graph_retrieved,
        ),
    ).fetchone()
    assert row is not None  # RETURNING always yields one row
    return str(row[0])
