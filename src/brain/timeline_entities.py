"""Resolve a timeline query to graph entities (moved out of ``timeline.py``, RM-46).

``brain timeline`` seeds its buckets with every ``graph_entities`` row whose name
or key contains the query. Extracted from :mod:`brain.timeline`, which is over
the file-size ceiling and asks to be extracted from before it grows: sec-118b's
#2-i needed a ``public_only`` filter here (over HTTP without
``--include-confidential`` the substring match would enumerate entity names only
confidential documents mention). ``brain_timeline`` is the one entity-name surface
still served in that mode; the graph tools are withheld there (RM-46 round 5).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from .sensitivity import not_confidential_sql

#: ``graph_entities`` row has a mention in a non-confidential document.
_PUBLIC_MENTION_SQL = (
    " AND EXISTS (SELECT 1 FROM graph_entity_mentions m "
    "JOIN documents d ON d.id = m.document_id "
    "WHERE m.tenant_id = graph_entities.tenant_id AND m.entity_id = graph_entities.id "
    f"AND {not_confidential_sql('d')})"
)

#: Entity-resolution fan-out cap — a free-text query that ILIKE-matches more than
#: this many entities is almost certainly too broad; mirrors the
#: ``relational.list_entities`` default LIMIT (spec §3f).
ENTITY_RESOLVE_LIMIT = 50


@dataclass(frozen=True)
class EntityRow:
    """A resolved ``graph_entities`` row for the timeline seed set.

    Projected from ``graph_entities`` by :func:`resolve_entities`. ``id`` is the
    UUID (as text); ``name`` / ``canonical_key`` / ``entity_type`` mirror the
    catalog row. Not the full :class:`brain.graph_rag.schema.GraphEntity` — the
    timeline only needs the identity + display name.
    """

    id: str
    name: str
    canonical_key: str
    entity_type: str


def resolve_entities(
    conn: psycopg.Connection[Any], tenant_id: str, query: str, *, public_only: bool = False
) -> list[EntityRow]:
    """Resolve a free-text query to ``graph_entities`` via ILIKE (spec §3f step 1).

    A dedicated parameterized ILIKE against ``name`` / ``canonical_key`` (NOT
    ``relational.list_entities``, which has no name filter). Both placeholders
    receive ``f"%{query}%"`` — never f-stringed into the SQL. Returns up to
    :data:`ENTITY_RESOLVE_LIMIT` matches ordered by name; an empty result is a
    normal "no entities found" outcome, not an error.
    """
    needle = f"%{query.strip()}%"
    # RM-46 (sec-118b #2-i): over HTTP, only entities a non-confidential document
    # mentions; the substring match would otherwise enumerate the graph.
    public = _PUBLIC_MENTION_SQL if public_only else ""
    rows = conn.execute(
        "SELECT id::text, name, canonical_key, entity_type "
        "FROM graph_entities "
        "WHERE tenant_id = %s AND (canonical_key ILIKE %s OR name ILIKE %s)"
        f"{public} "
        "ORDER BY name ASC, id ASC "
        "LIMIT %s",
        (tenant_id, needle, needle, ENTITY_RESOLVE_LIMIT),
    ).fetchall()
    return [
        EntityRow(
            id=str(r[0]), name=str(r[1]), canonical_key=str(r[2]), entity_type=str(r[3])
        )
        for r in rows
    ]
