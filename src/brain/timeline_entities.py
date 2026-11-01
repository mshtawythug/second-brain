"""Resolve a timeline query to graph entities (moved out of ``timeline.py``, RM-46).

``brain timeline`` seeds its buckets with every ``graph_entities`` row whose name
or key contains the query. Extracted from :mod:`brain.timeline`, which is over
the file-size ceiling and asks to be extracted from before it grows: sec-118b's
#2-i needed a ``public_only`` filter here (over HTTP without
``--include-confidential`` the substring match would enumerate entity names only
confidential documents mention). ``brain_timeline`` is the one entity-name surface
still served in that mode; the graph tools are withheld there (RM-46 round 5).
:func:`top_cotopics` moved here for the same reason when the co-topics needed the
same rule (sec-118f LOW-1).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from .sensitivity import name_in_public_record_sql

#: The gated lens names an entity only if a non-confidential record carries its name
#: (sec-118f LOW-1: a public mention alone can be an alias re-pointed onto a target
#: named only in confidential mail).
_PUBLIC_MENTION_SQL = " AND " + name_in_public_record_sql("graph_entities")

#: Co-topic entities surfaced per bucket (spec §3f step 3).
COTOPIC_LIMIT = 3

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


def top_cotopics(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    doc_ids: list[str],
    seed_entity_ids: list[str],
    owner_entity_ids: list[str],
    *,
    public_only: bool = False,
) -> list[str]:
    """Top co-occurring entity names for a bucket's docs (spec §3f step 3).

    Aggregates ``graph_edge_contributions`` restricted to the bucket's docs and
    edges touching a seed entity; the non-seed endpoint is the co-topic. Excludes
    the seed entities and the corpus owner (same suppression as themes mode).
    Returns up to :data:`COTOPIC_LIMIT` names by descending co-occurrence.
    ``public_only`` (HTTP hide mode) names only co-topics a non-confidential
    record names, as :func:`resolve_entities` does.
    """
    if not doc_ids or not seed_entity_ids:
        return []
    excluded = seed_entity_ids + owner_entity_ids
    public = " AND " + name_in_public_record_sql("ge") if public_only else ""
    rows = conn.execute(
        "SELECT ge.name, SUM(sub.cooccur) AS total FROM ("
        "    SELECT CASE WHEN gec.src_id = ANY(%s) THEN gec.dst_id "
        "                ELSE gec.src_id END AS co_id, "
        "           gec.cooccur_count AS cooccur "
        "    FROM graph_edge_contributions gec "
        "    WHERE gec.tenant_id = %s AND gec.document_id = ANY(%s) "
        "      AND (gec.src_id = ANY(%s) OR gec.dst_id = ANY(%s)) "
        ") sub "
        "JOIN graph_entities ge ON ge.id = sub.co_id AND ge.tenant_id = %s "
        f"WHERE sub.co_id::text <> ALL(%s){public} "
        "GROUP BY ge.name "
        "ORDER BY total DESC, ge.name ASC "
        "LIMIT %s",
        (
            seed_entity_ids,
            tenant_id,
            doc_ids,
            seed_entity_ids,
            seed_entity_ids,
            tenant_id,
            excluded,
            COTOPIC_LIMIT,
        ),
    ).fetchall()
    return [str(r[0]) for r in rows]

