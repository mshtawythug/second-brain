"""Graph entities a non-confidential document mentions (RM-46, sec-118 #2c).

An entity's NAME is extracted from document bodies. One that only confidential
documents mention is confidential content: a codename, a person, a project. Over
HTTP without ``--include-confidential``, ``brain mcp serve`` names an entity only
if at least one non-confidential document mentions it. That applies to the
entity listing (:func:`brain.graph_rag.relational.list_entities`, ``public_only``)
and to every entity list a graph retrieval returns
(:func:`withhold_confidential_entities`). stdio does not call either.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import psycopg

from ..sensitivity import not_confidential_sql
from .schema import GraphContext, GraphEntity


def public_mention_sql(alias: str) -> str:
    """``EXISTS`` predicate: entity row ``alias`` has a non-confidential mention."""
    if not alias.isidentifier():
        raise ValueError(f"table alias must be a plain identifier (got {alias!r})")
    return (
        "EXISTS (SELECT 1 FROM graph_entity_mentions m "
        "JOIN documents d ON d.id = m.document_id "
        f"WHERE m.tenant_id = {alias}.tenant_id AND m.entity_id = {alias}.id "
        f"AND {not_confidential_sql('d')})"
    )


def public_entity_ids(
    conn: psycopg.Connection[Any], tenant_id: str, entity_ids: Iterable[str]
) -> set[str]:
    """The subset of ``entity_ids`` that a non-confidential document mentions."""
    wanted = sorted(set(entity_ids))
    if not wanted:
        return set()
    rows = conn.execute(
        "SELECT e.id::text FROM graph_entities e "
        f"WHERE e.tenant_id = %s AND e.id::text = ANY(%s) AND {public_mention_sql('e')}",
        (tenant_id, wanted),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _kept(entities: list[GraphEntity], public: set[str]) -> list[GraphEntity]:
    return [entity for entity in entities if entity.id in public]


def withhold_confidential_entities(
    conn: psycopg.Connection[Any], ctx: GraphContext
) -> GraphContext:
    """``ctx`` without the entities only confidential documents mention.

    Covers the top-level entities and every theme's and community's. Returns a
    new context; ``ctx`` is not changed.
    """
    every = [
        *ctx.entities,
        *(entity for theme in ctx.themes for entity in theme.entities),
        *(entity for community in ctx.communities for entity in community.entities),
    ]
    public = public_entity_ids(conn, ctx.tenant_id, (entity.id for entity in every))
    if all(entity.id in public for entity in every):
        return ctx
    return replace(
        ctx,
        entities=_kept(ctx.entities, public),
        themes=[replace(theme, entities=_kept(theme.entities, public)) for theme in ctx.themes],
        communities=[
            replace(community, entities=_kept(community.entities, public))
            for community in ctx.communities
        ],
    )
