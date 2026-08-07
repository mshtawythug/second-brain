"""Usage logging that can never break a request.

Both telemetry tables constrain their ``source`` column with a CHECK, and
``'ui'`` only joined that set in migration 024. On a database where 024 has not
been applied, logging a UI search raises ``CheckViolation`` —
:func:`brain.gaps.record_search_query` swallows only ``OperationalError``,
``UndefinedTable``, and an ``UndefinedColumn`` narrowed to known additive
columns, and its docstring states plainly that anything else propagates. A
``CheckViolation`` is an ``IntegrityError``, matches no handler, and escapes.

**Unhandled, that means every single search through the UI returns a 500.** So
the constraint is probed exactly once at startup and the result is carried on
:class:`~brain.ui.context.UiContext`; when it comes back false the UI runs
normally and simply records nothing.

``record_interaction`` fails even earlier — at a Python gate that raises
``InteractionError`` before the INSERT is attempted — so the same flag guards
both writers.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

#: Read the live constraint rather than the migration file: what matters is
#: what *this* database enforces, not what the repository ships. Covers both
#: the pre-024 auto-generated name and 024's explicit one.
_CONSTRAINT_SQL = """
    SELECT pg_get_constraintdef(oid)
    FROM pg_constraint
    WHERE conrelid = 'search_queries'::regclass
      AND contype = 'c'
      AND conname IN ('search_queries_source_allowed', 'search_queries_source_check')
"""


def ui_source_supported(conn: psycopg.Connection[Any]) -> bool:
    """True when ``'ui'`` is admitted by the ``search_queries`` CHECK.

    Probed once at server startup. Any failure to read the catalogue — a
    missing table on a brand-new database, a permissions oddity — is answered
    ``False``, because the cost of a wrong ``True`` is a 500 on every search and
    the cost of a wrong ``False`` is an empty stats bucket.
    """
    try:
        rows = conn.execute(_CONSTRAINT_SQL).fetchall()
    except psycopg.Error as exc:
        logger.debug("ui telemetry probe failed: %s", type(exc).__name__)
        return False
    return any("'ui'" in str(row[0]) for row in rows)


def record_ui_search(
    conn: psycopg.Connection[Any],
    *,
    enabled: bool,
    query: str,
    result_count: int,
    session_id: uuid.UUID | None,
    fts_count: int | None = None,
    duration_ms: int | None = None,
) -> None:
    """Log one UI search when ``enabled``; otherwise do nothing.

    The ``psycopg.Error`` catch is a second belt on top of the startup probe:
    the probe can go stale if someone rolls a migration back while the server is
    running, and a stale probe must not become a 500.
    """
    if not enabled:
        return
    from ..gaps import record_search_query

    try:
        record_search_query(
            conn,
            query=query,
            result_count=result_count,
            session_id=session_id,
            source="ui",
            fts_count=fts_count,
            duration_ms=duration_ms,
        )
    except psycopg.Error as exc:
        logger.debug("ui search telemetry skipped: %s", type(exc).__name__)


def record_ui_open(
    conn: psycopg.Connection[Any],
    *,
    enabled: bool,
    document_id: str,
    query: str | None,
    session_id: uuid.UUID | None,
) -> None:
    """Log that a document was opened from the UI. Best-effort, like the search."""
    if not enabled or session_id is None:
        return
    from ..errors import InteractionError
    from ..interactions import record_interaction

    try:
        record_interaction(
            conn,
            document_id=document_id,
            action="opened",
            source="ui",
            query=query,
            session_id=session_id,
        )
    except (psycopg.Error, InteractionError) as exc:
        logger.debug("ui open telemetry skipped: %s", type(exc).__name__)


def parse_session_id(raw: str | None) -> uuid.UUID | None:
    """Parse a client-supplied session id, tolerating junk.

    A malformed session id is a telemetry problem, never a request problem — so
    it degrades to "no session" rather than to a 400.
    """
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None
