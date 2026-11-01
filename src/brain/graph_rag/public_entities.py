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
from .schema import GraphContext, GraphEntity, ThemeGroup


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


def _public_themes(themes: list[ThemeGroup], public: set[str]) -> list[ThemeGroup]:
    """Each theme with its withheld entities removed; a summary written from one dropped.

    A synthesized summary is written from ALL the theme's entity names, so a
    theme that lost any of them loses its summary too, and a theme left with no
    public entity is dropped (RM-46 rev-118 round 2).
    """
    kept: list[ThemeGroup] = []
    for theme in themes:
        entities = _kept(theme.entities, public)
        if not entities:
            continue
        lost = len(entities) != len(theme.entities)
        kept.append(replace(theme, entities=entities, summary=None if lost else theme.summary))
    return kept


def withhold_confidential_entities(
    conn: psycopg.Connection[Any], ctx: GraphContext
) -> GraphContext:
    """``ctx`` without the entities only confidential documents mention.

    Covers the top-level entities, every theme's and community's, and the
    explanation's seed ids; a theme summary written from a withheld name is
    dropped (:func:`_public_themes`), and a community that lost a member is
    dropped whole, as ``list_communities`` withholds one. Returns a new context;
    ``ctx`` is not changed.
    """
    every = [
        *ctx.entities,
        *(entity for theme in ctx.themes for entity in theme.entities),
        *(entity for community in ctx.communities for entity in community.entities),
    ]
    seeds = ctx.explanation.seed_entity_ids if ctx.explanation is not None else []
    public = public_entity_ids(
        conn, ctx.tenant_id, [*(entity.id for entity in every), *seeds]
    )
    if all(entity.id in public for entity in every) and all(seed in public for seed in seeds):
        return ctx
    explanation = (
        replace(ctx.explanation, seed_entity_ids=[seed for seed in seeds if seed in public])
        if ctx.explanation is not None
        else None
    )
    return replace(
        ctx,
        explanation=explanation,
        entities=_kept(ctx.entities, public),
        themes=_public_themes(ctx.themes, public),
        communities=[
            community
            for community in ctx.communities
            if all(entity.id in public for entity in community.entities)
        ],
    )
