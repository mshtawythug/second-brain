"""The daily brief's "new this week" section: people, orgs and projects new to the brain (RM-17).

:func:`new_entities` lists the entities whose FIRST appearance in the corpus falls inside the
window. Two facts about the graph tables (``012_graphrag.sql``) shape the query:

* ``graph_entity_mentions`` carries no timestamp, so "first appeared" is read from the
  mentioning documents: ``MIN(d.ingested_at)`` over the entity's NON-DRAFT documents must be
  ``>= since`` (RM-17.D1: ``ingested_at``, not ``sent_at``, because an old thread imported this
  week is exactly what the owner has not seen yet).
* ``graph_entities.created_at`` resets when ``graph_rag/aggregates.py`` deletes an orphaned
  entity that is later re-created, so it is used ONLY as a prefilter (``created_at >= since``)
  that keeps the aggregate small. It never decides on its own. The accepted cost: an entity
  created before ``since`` whose older documents were all deleted since is not listed. That
  delays a name; it never leaks one.

**The confidential lens is inside the aggregate.** With ``exclude_confidential`` the
confidential documents are removed in the ``WHERE`` clause, before ``GROUP BY`` and ``MIN``, so
an entity known only from confidential documents has no rows and is not listed, and an old
confidential mention cannot make an entity look old (or, by its absence, say that such a
mention exists) to the gated audience. Filtering after the aggregate would do both.

**Drafts never count**, in either lens: a draft-only entity is in neither payload, so
``brief_delivery.without_drafts`` has nothing of this section's to remove.

The section is empty when the graph is disabled (``BRAIN_GRAPH_ENABLED``) or the graph tables are
absent (:func:`entity_tables_present`, two ``to_regclass`` probes), so a stock brief is unchanged.
Only entity names leave this module; no document title or body is read.

Plan: ``docs/plans/2026-10-30-rm-17-brief-new-entities.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .config import Config
from .sensitivity import not_confidential_sql

#: "New this week": the window the brief reports on.
WINDOW_DAYS = 7
#: The most names the section lists.
DEFAULT_LIMIT = 10
#: The page's and the delivered message's heading for the section.
SECTION_TITLE = "New this week"
#: The graph entity types the section reports; topics and tools are too noisy for a daily list.
REPORTED_TYPES = ("person", "org", "project")

# One statement, two lens variants chosen by a frozen fragment (never a bound level): the
# sensitivity predicate sits in the WHERE clause, so it filters the rows the MIN aggregates.
_NEW_ENTITIES_SQL = """
    SELECT e.id::text, e.name, e.entity_type,
           MIN(d.ingested_at) AS first_ingested_at,
           COUNT(*) AS document_count
    FROM   graph_entities e
    JOIN   graph_entity_mentions m
           ON m.tenant_id = e.tenant_id AND m.entity_id = e.id
    JOIN   documents d ON d.id = m.document_id
    WHERE  e.tenant_id = %s
      AND  e.created_at >= %s
      AND  e.entity_type = ANY(%s)
      AND  d.draft = FALSE
      {lens}
    GROUP  BY e.id, e.name, e.entity_type
    HAVING MIN(d.ingested_at) >= %s
    ORDER  BY document_count DESC, first_ingested_at DESC, e.name, e.id
    LIMIT  %s
"""
_GATED_SQL = _NEW_ENTITIES_SQL.format(lens=f"AND  {not_confidential_sql('d')}")
_PERMISSIVE_SQL = _NEW_ENTITIES_SQL.format(lens="")


@dataclass(frozen=True)
class NewEntity:
    """One entity first seen inside the window, as the audience's lens sees it."""

    entity_id: str
    name: str
    entity_type: str
    first_ingested_at: datetime
    #: Non-draft mentioning documents visible through the lens.
    document_count: int

    def to_dict(self) -> dict[str, Any]:
        """The wire shape inside ``BriefData.to_dict()['new_entities']``."""
        return {
            "id": self.entity_id,
            "name": self.name,
            "type": self.entity_type,
            "first_ingested_at": self.first_ingested_at.isoformat(),
            "documents": self.document_count,
        }


def entity_tables_present(conn: psycopg.Connection[Any]) -> bool:
    """Whether both graph tables the section reads exist (a cheap catalog probe)."""
    row = conn.execute(
        "SELECT to_regclass('public.graph_entities') IS NOT NULL "
        "AND to_regclass('public.graph_entity_mentions') IS NOT NULL"
    ).fetchone()
    return bool(row and row[0])


def new_entities(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    since: datetime,
    exclude_confidential: bool,
    limit: int = DEFAULT_LIMIT,
) -> list[NewEntity]:
    """People, orgs and projects whose first non-draft mention was ingested at or after ``since``.

    Most-mentioned first, then the newest, then by name. See the module docstring for why
    ``created_at`` only prefilters and why the lens sits inside the aggregate.
    """
    rows = conn.execute(
        _GATED_SQL if exclude_confidential else _PERMISSIVE_SQL,
        (tenant_id, since, list(REPORTED_TYPES), since, limit),
    ).fetchall()
    return [
        NewEntity(
            entity_id=row[0],
            name=row[1],
            entity_type=row[2],
            first_ingested_at=row[3],
            document_count=int(row[4]),
        )
        for row in rows
    ]


def for_brief(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    exclude_confidential: bool,
    now: datetime | None = None,
) -> list[NewEntity]:
    """The brief's section: the last :data:`WINDOW_DAYS` days, or nothing without the graph.

    ``now`` is the test seam for the window's end; production passes nothing.
    """
    if not cfg.graph_enabled or not entity_tables_present(conn):
        return []
    end = now if now is not None else datetime.now(UTC)
    return new_entities(
        conn,
        tenant_id=cfg.graph_tenant_id,
        since=end - timedelta(days=WINDOW_DAYS),
        exclude_confidential=exclude_confidential,
    )


def section_lines(entities: list[NewEntity]) -> list[str]:
    """``[type] name`` per entity, without bullets; a name's whitespace runs become one space."""
    return [f"[{entity.entity_type}] {' '.join(entity.name.split())}" for entity in entities]
