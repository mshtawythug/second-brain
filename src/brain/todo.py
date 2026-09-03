"""Parser for ``- [ ]`` / ``- [x]`` Markdown action-item lines.

Standalone so it can be unit-tested without a DB. The CLI calls
:func:`parse_action_items` on each ``krisp_action_items`` doc body fetched
in one SELECT (see :mod:`brain.cli` `todo` command).
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg

from .sensitivity import not_confidential_sql

# Match leading whitespace, ``-`` or ``*``, whitespace, ``[ ]`` or ``[x]``
# (case-insensitive), whitespace, rest-of-line. Anchored with ``^`` +
# ``re.MULTILINE`` so we walk every body line in one pass.
_TASK_RE = re.compile(
    r"^\s*[-*]\s*\[([ xX])\]\s*(.+?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ActionItem:
    """One parsed action-item line.

    ``state`` is ``"open"`` for ``[ ]`` and ``"done"`` for ``[x]`` (case-
    insensitive). ``text`` is the rest-of-line content after the checkbox,
    stripped of trailing whitespace.
    """

    state: Literal["open", "done"]
    text: str


def parse_action_items(body: str) -> list[ActionItem]:
    """Return one :class:`ActionItem` per recognized task line.

    Lines without the ``[ ]`` / ``[x]`` pattern are ignored — comments,
    headers, the trailing ``Parent meeting:`` wikilink. The parser is
    Markdown-aware enough to handle leading whitespace + ``-`` / ``*``
    bullets but does NOT support nested checkboxes (a future wave can add
    nesting if the action-items shape grows). Pure function; the CLI feeds
    in pre-fetched bodies.
    """
    out: list[ActionItem] = []
    for m in _TASK_RE.finditer(body):
        ch = m.group(1)
        state: Literal["open", "done"] = "done" if ch.lower() == "x" else "open"
        out.append(ActionItem(state=state, text=m.group(2)))
    return out


@dataclass(frozen=True)
class TodoRow:
    """One flat row returned by :func:`iter_action_item_docs`.

    Carries the parent document context (id, title, ingested_at) alongside
    the parsed action item so the CLI ``brain todo`` view can render a flat
    table without re-joining at print time.
    """

    document_id: str
    document_title: str
    ingested_at: datetime | None
    state: Literal["open", "done"]
    text: str


def iter_action_item_docs(
    conn: psycopg.Connection[Any],
    *,
    source_kind: str | None = None,
    since_days: int | None = None,
    include_closed: bool = False,
    exclude_confidential: bool = False,
) -> Iterator[TodoRow]:
    """Yield one :class:`TodoRow` per parsed item across the corpus.

    Walks ``documents WHERE content_type='krisp_action_items'`` (optionally
    filtered by source / recency), parses each body line by line, and
    yields the parsed items in document-recency order. The caller decides
    whether to render a table or emit JSON.

    Filters:

    - ``source_kind`` — restricts to docs whose joined ``sources.kind``
      matches (today only ``"krisp"`` is meaningful, but the parameter
      stays open for future source kinds emitting the same content_type).
    - ``since_days`` — restricts to docs ingested in the last N days.
    - ``include_closed`` — when False (default), drop ``[x]`` items from
      the stream; when True, both states are returned.
    - ``exclude_confidential`` (F6) — drop documents marked
      ``sensitivity='confidential'`` entirely.

    THE CONFIDENTIALITY GATE HERE IS A BODY GATE, NOT A TITLE GATE, and that is
    the whole reason it matters. This query selects ``d.content`` and
    :func:`parse_action_items` lifts item text straight out of it into
    ``TodoRow.text``. So a confidential document does not merely have its name
    disclosed through this reader — its BODY TEXT is republished, one action item
    at a time. :func:`brain.brief.suggest_next_steps` then forwards exactly that
    text to a hosted model in its prompt. That is the precise egress F6 exists to
    prevent, reachable from ``brain_brief`` and ``brain_review_weekly`` with no
    parameters at all.

    It DEFAULTS FALSE — include — because ``brain todo`` at a terminal is inside
    the trust boundary; the MCP layer passes ``exclude_confidential=not
    include_confidential``. See :func:`brain.mcp_server._confidential_lens`.
    """
    sql = (
        "SELECT d.id::text, d.title, d.content, d.ingested_at, s.kind "
        "FROM documents d LEFT JOIN sources s ON s.id = d.source_id "
        "WHERE d.content_type = 'krisp_action_items'"
    )
    if exclude_confidential:
        sql += f" AND {not_confidential_sql('d')}"
    params: list[Any] = []
    if source_kind is not None:
        sql += " AND s.kind = %s"
        params.append(source_kind)
    if since_days is not None:
        sql += " AND d.ingested_at >= NOW() - (%s * INTERVAL '1 day')"
        params.append(since_days)
    sql += " ORDER BY d.ingested_at DESC"
    rows = conn.execute(sql, params).fetchall()
    for r in rows:
        doc_id = str(r[0])
        title = str(r[1])
        body = str(r[2] or "")
        ingested_at = r[3]
        for item in parse_action_items(body):
            if item.state == "done" and not include_closed:
                continue
            yield TodoRow(
                document_id=doc_id,
                document_title=title,
                ingested_at=ingested_at,
                state=item.state,
                text=item.text,
            )
