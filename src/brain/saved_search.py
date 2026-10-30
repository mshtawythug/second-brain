"""Saved searches ("watches"): their definitions and the hit reads the daily brief uses (RM-15).

A watch is a saved query plus filters (``saved_searches``, migration 029). Each check records the
documents that newly match it (``saved_search_hits``); the matching lives in
:mod:`brain.saved_search_check`. This module owns the rows: create, read, list, pause, remove,
and the three reads of the hits:

- :func:`hits_since` is the TERMINAL set: every hit first seen inside a window, notified or not.
  ``brain brief`` (and MCP ``brain_brief`` through its lens) and ``brain watch hits`` read it.
- :func:`unnotified_view` is the PUBLISHED set: the hits no published brief has listed yet, at
  most :data:`PUBLISHED_CAP` per watch, gated by default, plus the key of EVERY unnotified hit
  (confidential and over-cap ones included) so the publisher can mark them all.
- :func:`mark_notified` marks exactly the keys it is given.

Every read that names a document takes ``exclude_confidential`` (F6): the brief's published page
and MCP are outside the trust boundary, the terminal is not.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .errors import BrainError
from .sensitivity import not_confidential_sql

MODE_LEXICAL = "lexical"
MODE_SEMANTIC = "semantic"
MODES = frozenset({MODE_LEXICAL, MODE_SEMANTIC})

#: The most hits per watch a published brief lists; the rest is "and N more" (the ticket's cap).
PUBLISHED_CAP = 5
#: Longest watch name. A name is printed in the terminal, the brief page and MCP.
MAX_NAME_LENGTH = 80

_COLUMNS = "id::text, name, query, filters, mode, enabled, created_at, last_checked_at"

#: Excludes the SEED rows of ``saved_search_hits`` (alias ``h``): the matches ``add`` records as
#: already seen. A seed is the only row inserted seen AND notified in one transaction, so its
#: ``notified_at`` equals its ``first_seen_at`` (both that transaction's ``NOW()``). A hit a check
#: finds is inserted unnotified and marked by a later transaction, so the two always differ.
NOT_SEED_SQL = "h.notified_at IS DISTINCT FROM h.first_seen_at"


class SavedSearchError(BrainError):
    """A watch request the caller must fix (a bad name, query or mode)."""


class SavedSearchNotFound(SavedSearchError):
    """No watch has this name."""


class SavedSearchExists(SavedSearchError):
    """A watch with this name already exists."""


@dataclass(frozen=True)
class WatchFilters:
    """The search filters a watch saves. ``None`` means no filter."""

    source: str | None = None
    tag: str | None = None
    person: str | None = None
    kind: str | None = None
    without_tag: str | None = None

    def to_json(self) -> dict[str, str]:
        """The set filters only, as stored in ``saved_searches.filters``."""
        return {
            f.name: value for f in fields(self) if (value := getattr(self, f.name)) is not None
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> WatchFilters:
        """Read the stored filters, ignoring keys this version does not know."""
        known = {f.name for f in fields(cls)}
        return cls(**{key: str(value) for key, value in raw.items() if key in known})


@dataclass(frozen=True)
class SavedSearch:
    """One watch, as stored."""

    id: str
    name: str
    query: str
    filters: WatchFilters
    mode: str
    enabled: bool
    created_at: datetime
    last_checked_at: datetime


@dataclass(frozen=True)
class WatchHit:
    """A document a watch matched. ``notified`` is whether a published brief listed it."""

    watch: str
    document_id: str
    title: str
    source_kind: str | None
    first_seen_at: datetime
    notified: bool


@dataclass(frozen=True)
class WatchNote:
    """A per-watch line in the brief: ``more`` hits beyond the cap, or why a check was skipped."""

    watch: str
    more: int = 0
    skipped: str | None = None


@dataclass(frozen=True)
class UnnotifiedView:
    """What a published brief lists, and the key of every unnotified hit it found."""

    hits: list[WatchHit]
    notes: list[WatchNote]
    #: ``(saved_search_id, document_id)`` of EVERY unnotified hit read, whatever the lens or cap.
    pending: frozenset[tuple[str, str]]


def _row(row: tuple[Any, ...]) -> SavedSearch:
    return SavedSearch(
        id=row[0],
        name=row[1],
        query=row[2],
        filters=WatchFilters.from_json(row[3] or {}),
        mode=row[4],
        enabled=row[5],
        created_at=row[6],
        last_checked_at=row[7],
    )


def validate_name(name: str) -> str:
    """Return ``name`` stripped, or raise :class:`SavedSearchError`.

    A name is printed in the terminal, the brief page and MCP, so it is one short line of
    printable text.
    """
    stripped = name.strip()
    if not stripped:
        raise SavedSearchError("a watch name must not be blank")
    if len(stripped) > MAX_NAME_LENGTH:
        raise SavedSearchError(f"a watch name must be at most {MAX_NAME_LENGTH} characters")
    if not stripped.isprintable():
        raise SavedSearchError("a watch name must be printable text on one line")
    return stripped


def watch_tables_present(conn: psycopg.Connection[Any]) -> bool:
    """Whether migration 029 is applied (both tables exist in ``public``)."""
    row = conn.execute(
        "SELECT to_regclass('public.saved_searches') IS NOT NULL "
        "AND to_regclass('public.saved_search_hits') IS NOT NULL"
    ).fetchone()
    return bool(row and row[0])


def create_watch(
    conn: psycopg.Connection[Any],
    *,
    name: str,
    query: str,
    filters: WatchFilters,
    mode: str,
) -> SavedSearch:
    """Insert a watch. The filters must already be validated (:func:`add_watch` does that).

    Raises:
        SavedSearchError: a bad name, blank query or unknown mode.
        SavedSearchExists: the name is taken.
    """
    name = validate_name(name)
    if not query.strip():
        raise SavedSearchError("a watch query must not be blank")
    if mode not in MODES:
        raise SavedSearchError(f"mode must be one of {', '.join(sorted(MODES))}")
    row = conn.execute(
        f"""
        INSERT INTO saved_searches (name, query, filters, mode)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (name) DO NOTHING
        RETURNING {_COLUMNS}
        """,
        (name, query.strip(), Jsonb(filters.to_json()), mode),
    ).fetchone()
    if row is None:
        raise SavedSearchExists(f"a watch named {name!r} already exists")
    return _row(row)


def get_watch(conn: psycopg.Connection[Any], name: str) -> SavedSearch:
    """Return the watch called ``name``. Raises :class:`SavedSearchNotFound`."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM saved_searches WHERE name = %s", (name,)
    ).fetchone()
    if row is None:
        raise SavedSearchNotFound(f"no watch named {name!r}")
    return _row(row)


def lock_watch(conn: psycopg.Connection[Any], name: str) -> tuple[SavedSearch, datetime]:
    """Lock the watch's row (``FOR UPDATE``) and return it with the transaction's start time.

    Call inside a transaction: the lock is held until it ends. Raises
    :class:`SavedSearchNotFound`.
    """
    row = conn.execute(
        f"SELECT {_COLUMNS}, now() FROM saved_searches WHERE name = %s FOR UPDATE", (name,)
    ).fetchone()
    if row is None:
        raise SavedSearchNotFound(f"no watch named {name!r}")
    return _row(row), row[8]


def list_watches(
    conn: psycopg.Connection[Any], *, enabled_only: bool = False
) -> list[SavedSearch]:
    """Every watch, by name; only the enabled ones when ``enabled_only``."""
    where = "WHERE enabled" if enabled_only else ""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM saved_searches {where} ORDER BY name"
    ).fetchall()
    return [_row(row) for row in rows]


def remove_watch(conn: psycopg.Connection[Any], name: str) -> None:
    """Delete a watch and (by cascade) its hits. Raises :class:`SavedSearchNotFound`."""
    row = conn.execute(
        "DELETE FROM saved_searches WHERE name = %s RETURNING id", (name,)
    ).fetchone()
    if row is None:
        raise SavedSearchNotFound(f"no watch named {name!r}")


def set_enabled(conn: psycopg.Connection[Any], name: str, *, enabled: bool) -> SavedSearch:
    """Pause (``enabled=False``) or resume a watch. Raises :class:`SavedSearchNotFound`."""
    row = conn.execute(
        f"UPDATE saved_searches SET enabled = %s WHERE name = %s RETURNING {_COLUMNS}",
        (enabled, name),
    ).fetchone()
    if row is None:
        raise SavedSearchNotFound(f"no watch named {name!r}")
    return _row(row)


def _hit(row: tuple[Any, ...]) -> WatchHit:
    return WatchHit(
        watch=row[0],
        document_id=row[1],
        title=row[2],
        source_kind=row[3],
        first_seen_at=row[4],
        notified=row[5],
    )


_HIT_SELECT = """
    SELECT s.name, d.id::text, d.title, src.kind, h.first_seen_at, h.notified_at IS NOT NULL,
           h.saved_search_id::text
    FROM   saved_search_hits h
    JOIN   saved_searches s ON s.id = h.saved_search_id
    JOIN   documents d ON d.id = h.document_id
    LEFT JOIN sources src ON src.id = d.source_id
"""


def hits_since(
    conn: psycopg.Connection[Any],
    *,
    hours: int,
    name: str | None = None,
    exclude_confidential: bool = False,
) -> list[WatchHit]:
    """Every hit first seen in the last ``hours`` hours, notified or not (the terminal set).

    Seeds are not hits: the matches ``add`` records as already seen are left out
    (:data:`NOT_SEED_SQL`).

    By watch name, newest first. ``name`` keeps one watch's hits. ``exclude_confidential`` (F6)
    drops confidential documents in the predicate; it defaults to include, because the terminal
    is inside the trust boundary and MCP passes its lens.
    """
    where = ["h.first_seen_at >= NOW() - make_interval(hours => %s)", NOT_SEED_SQL]
    params: list[Any] = [hours]
    if name is not None:
        where.append("s.name = %s")
        params.append(name)
    if exclude_confidential:
        where.append(not_confidential_sql("d"))
    rows = conn.execute(
        f"{_HIT_SELECT} WHERE {' AND '.join(where)} "
        "ORDER BY s.name, h.first_seen_at DESC, d.id",
        params,
    ).fetchall()
    return [_hit(row) for row in rows]


def unnotified_view(
    conn: psycopg.Connection[Any],
    *,
    per_watch_cap: int = PUBLISHED_CAP,
    exclude_confidential: bool = True,
) -> UnnotifiedView:
    """The unnotified hits a published brief lists, and the keys of all of them.

    ``hits`` holds at most ``per_watch_cap`` per watch (newest first) that pass the lens;
    ``notes`` says how many more passed it. ``pending`` holds every unnotified hit read, over-cap
    and confidential ones included, so marking it leaves nothing unnotified that the page was
    built over. The lens is applied here in Python, over one read, so the keys and the listing
    cannot come from two different snapshots.

    ``exclude_confidential`` DEFAULTS TRUE: the only caller is the published page.
    """
    rows = conn.execute(
        f"""
        SELECT s.name, d.id::text, d.title, src.kind, h.first_seen_at, FALSE,
               h.saved_search_id::text, NOT ({not_confidential_sql("d")})
        FROM   saved_search_hits h
        JOIN   saved_searches s ON s.id = h.saved_search_id
        JOIN   documents d ON d.id = h.document_id
        LEFT JOIN sources src ON src.id = d.source_id
        WHERE  h.notified_at IS NULL
        ORDER  BY s.name, h.first_seen_at DESC, d.id
        """
    ).fetchall()
    shown: list[WatchHit] = []
    per_watch: dict[str, int] = {}
    more: dict[str, int] = {}
    pending: set[tuple[str, str]] = set()
    for row in rows:
        pending.add((row[6], row[1]))
        if exclude_confidential and row[7]:
            continue
        watch = row[0]
        per_watch[watch] = per_watch.get(watch, 0) + 1
        if per_watch[watch] <= per_watch_cap:
            shown.append(_hit(row))
        else:
            more[watch] = more.get(watch, 0) + 1
    notes = [WatchNote(watch=watch, more=count) for watch, count in sorted(more.items())]
    return UnnotifiedView(hits=shown, notes=notes, pending=frozenset(pending))


def mark_notified(conn: psycopg.Connection[Any], pending: Iterable[tuple[str, str]]) -> int:
    """Mark exactly these ``(saved_search_id, document_id)`` hits notified; return how many."""
    keys = list(pending)
    if not keys:
        return 0
    cursor = conn.execute(
        """
        UPDATE saved_search_hits h
        SET    notified_at = NOW()
        FROM   unnest(%s::uuid[], %s::uuid[]) AS k(saved_search_id, document_id)
        WHERE  h.saved_search_id = k.saved_search_id
          AND  h.document_id = k.document_id
          AND  h.notified_at IS NULL
        """,
        ([key[0] for key in keys], [key[1] for key in keys]),
    )
    return cursor.rowcount
