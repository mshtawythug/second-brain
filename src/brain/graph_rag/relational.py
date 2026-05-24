"""Per-document relational source-of-truth helpers for graph reconcile.

Aspect-agnostic read/write + per-aspect watermark helpers shared by the person
and concept reconcile paths in :mod:`brain.graph_rag.reconcile`. Extracted from
that module (mirroring the G1 :mod:`brain.graph_rag.aggregates` boundary) so the
orchestrator stays lean and these parameterized-SQL primitives live in one
focused place. Every function is tenant-scoped and operates only on the
migration-012 relational tables (``documents`` is read-only here); the AGE mirror
is the orchestrator's concern.
"""
from __future__ import annotations

from typing import Any

import psycopg

from ..errors import GraphBackendError
from .schema import EdgeContribution, EntityMention, EntitySummary, GraphStats

__all__ = [
    "delete_doc_relational",
    "fetch_doc_content",
    "fetch_doc_meta",
    "graph_stats",
    "index_state",
    "list_entities",
    "read_doc_mentions",
    "rewrite_doc_relational",
    "upsert_index_state",
]

# Allowlists for validated query parameters — never interpolated into SQL.
_VALID_ENTITY_TYPES: frozenset[str] = frozenset({"org", "project", "tool", "topic", "person"})
_VALID_SORT_OPTIONS: frozenset[str] = frozenset({"docs", "name"})


def fetch_doc_meta(
    conn: psycopg.Connection[Any], document_id: str
) -> tuple[str, str] | None:
    """Return ``(content_hash, content_type)`` for a document, or ``None``."""
    row = conn.execute(
        "SELECT content_hash, content_type FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    return (str(row[0]), str(row[1]))


def fetch_doc_content(conn: psycopg.Connection[Any], document_id: str) -> str:
    """Return a document's body text (for concept extraction; spec §4 D4).

    Fetched lazily — only when the concept aspect is stale and about to extract —
    so the person-only path never pays to load a (potentially large) body.
    """
    row = conn.execute(
        "SELECT content FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else ""


def read_doc_mentions(
    conn: psycopg.Connection[Any], tenant_id: str, document_id: str
) -> list[EntityMention]:
    """Read a doc's COMBINED current mentions (both aspects) from relational.

    The relational source-of-truth after the per-aspect rewrites; feeds the
    single AGE ``MENTIONED_IN`` rebuild so a fresh aspect's edges are preserved
    intact. Ordered by ``entity_id`` for deterministic Cypher emission.
    """
    rows = conn.execute(
        "SELECT entity_id::text, source, mention_count FROM graph_entity_mentions "
        "WHERE tenant_id = %s AND document_id = %s ORDER BY entity_id",
        (tenant_id, document_id),
    ).fetchall()
    return [
        EntityMention(
            entity_id=str(row[0]),
            document_id=document_id,
            source=str(row[1]),
            tenant_id=tenant_id,
            mention_count=int(row[2]),
        )
        for row in rows
    ]


def index_state(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    document_id: str,
    aspect: str,
) -> tuple[str, str, str, str] | None:
    """Return the stored watermark tuple for one aspect, or ``None`` if absent."""
    row = conn.execute(
        "SELECT content_hash, inputs_hash, extractor_ver, suppress_ver "
        "FROM graph_index_state "
        "WHERE tenant_id = %s AND document_id = %s AND aspect = %s",
        (tenant_id, document_id, aspect),
    ).fetchone()
    if row is None:
        return None
    return (str(row[0]), str(row[1]), str(row[2]), str(row[3]))


def upsert_index_state(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    document_id: str,
    *,
    aspect: str,
    content_hash: str,
    inputs_hash: str,
    extractor_ver: str,
    sver: str,
) -> None:
    """Write/refresh one aspect's ``graph_index_state`` watermark row."""
    conn.execute(
        """
        INSERT INTO graph_index_state
            (tenant_id, document_id, aspect, content_hash, inputs_hash,
             extractor_ver, suppress_ver, indexed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (tenant_id, document_id, aspect) DO UPDATE SET
            content_hash = EXCLUDED.content_hash,
            inputs_hash = EXCLUDED.inputs_hash,
            extractor_ver = EXCLUDED.extractor_ver,
            suppress_ver = EXCLUDED.suppress_ver,
            indexed_at = NOW()
        """,
        (
            tenant_id,
            document_id,
            aspect,
            content_hash,
            inputs_hash,
            extractor_ver,
            sver,
        ),
    )


def rewrite_doc_relational(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    document_id: str,
    mentions: list[EntityMention],
    contributions: list[EdgeContribution],
    *,
    entity_types: tuple[str, ...],
) -> None:
    """Aspect-scoped delete + reinsert of one doc's mentions + contributions.

    Deletes ONLY the doc's rows whose entity is one of ``entity_types`` (the
    aspect being rewritten) before reinserting, so the two aspects never clobber
    each other's rows for the same document. People and concepts never share a
    co-occurrence pair (each aspect's co-occurrence is computed within its own
    entity set), so a contribution's ``src_id`` aspect determines the whole row's
    aspect — scoping the contribution delete on ``src_id`` alone is correct. With
    a single aspect active (the person-only default) this scopes to ``('person',)``
    and matches exactly the rows the old delete-all removed.
    """
    type_list = list(entity_types)
    conn.execute(
        "DELETE FROM graph_entity_mentions "
        "WHERE tenant_id = %s AND document_id = %s AND entity_id IN ("
        "  SELECT id FROM graph_entities "
        "  WHERE tenant_id = %s AND entity_type = ANY(%s))",
        (tenant_id, document_id, tenant_id, type_list),
    )
    conn.execute(
        "DELETE FROM graph_edge_contributions "
        "WHERE tenant_id = %s AND document_id = %s AND src_id IN ("
        "  SELECT id FROM graph_entities "
        "  WHERE tenant_id = %s AND entity_type = ANY(%s))",
        (tenant_id, document_id, tenant_id, type_list),
    )
    for mention in mentions:
        conn.execute(
            "INSERT INTO graph_entity_mentions "
            "(tenant_id, entity_id, document_id, mention_count, source) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                tenant_id,
                mention.entity_id,
                document_id,
                mention.mention_count,
                mention.source,
            ),
        )
    for contribution in contributions:
        conn.execute(
            "INSERT INTO graph_edge_contributions "
            "(tenant_id, document_id, src_id, dst_id, cooccur_count) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                tenant_id,
                document_id,
                contribution.src_id,
                contribution.dst_id,
                contribution.cooccur_count,
            ),
        )


def delete_doc_relational(
    conn: psycopg.Connection[Any], tenant_id: str, document_id: str
) -> None:
    """Delete one doc's source rows + BOTH aspects' watermarks (idempotent).

    Aspect-agnostic delete-all for the document — mentions/contributions of every
    entity type, and the people AND concepts ``graph_index_state`` rows — so a
    removed document leaves no graph presence regardless of which aspects indexed
    it (wave G2-c).
    """
    conn.execute(
        "DELETE FROM graph_entity_mentions WHERE tenant_id = %s AND document_id = %s",
        (tenant_id, document_id),
    )
    conn.execute(
        "DELETE FROM graph_edge_contributions "
        "WHERE tenant_id = %s AND document_id = %s",
        (tenant_id, document_id),
    )
    conn.execute(
        "DELETE FROM graph_index_state WHERE tenant_id = %s AND document_id = %s",
        (tenant_id, document_id),
    )


# ---------------------------------------------------------------------------
# Listing helpers for the admin enumeration surfaces (wave plan 2026-05-23).
# ---------------------------------------------------------------------------


def list_entities(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    *,
    entity_type: str | None = None,
    sort: str = "docs",
    limit: int = 50,
) -> list[EntitySummary]:
    """List entities for the tenant, filtered and sorted (admin listing).

    Returns :class:`~brain.graph_rag.schema.EntitySummary` rows from
    ``graph_entities``, filtered to ``entity_type`` when given, ordered by
    ``sort`` (``"docs"`` → ``doc_count DESC, name ASC``; ``"name"`` →
    ``name ASC``), and capped at ``limit`` rows (``limit <= 0`` returns all).
    Read-only; no raw Cypher or AGE traversal. The raw ``embedding`` column is
    not selected (a storage handle, not a wire value).

    Raises:
        GraphBackendError: ``entity_type`` is not one of the five known types, or
            ``sort`` is not one of the two valid options.
    """
    if entity_type is not None and entity_type not in _VALID_ENTITY_TYPES:
        raise GraphBackendError(
            f"invalid entity_type {entity_type!r}; "
            f"must be one of {sorted(_VALID_ENTITY_TYPES)}"
        )
    if sort not in _VALID_SORT_OPTIONS:
        raise GraphBackendError(
            f"invalid sort {sort!r}; must be one of {sorted(_VALID_SORT_OPTIONS)}"
        )

    params: list[Any] = [tenant_id]
    where_clause = "WHERE tenant_id = %s"
    if entity_type is not None:
        where_clause += " AND entity_type = %s"
        params.append(entity_type)

    order_clause = (
        "ORDER BY doc_count DESC, name ASC" if sort == "docs" else "ORDER BY name ASC"
    )

    sql = (
        "SELECT entity_type, name, canonical_key, doc_count, description "
        f"FROM graph_entities {where_clause} {order_clause}"
    )
    if limit > 0:
        sql += " LIMIT %s"
        params.append(limit)

    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        EntitySummary(
            entity_type=str(row[0]),
            name=str(row[1]),
            canonical_key=str(row[2]),
            doc_count=int(row[3]),
            description=str(row[4]) if row[4] is not None else None,
        )
        for row in rows
    ]


def graph_stats(
    conn: psycopg.Connection[Any],
    tenant_id: str,
) -> GraphStats:
    """Return an at-a-glance graph overview for the tenant.

    Reads entity counts grouped by type from ``graph_entities``, the
    relationship count from ``graph_relationships``, the community count from
    ``graph_communities``, and the top-10 entities by ``doc_count``
    (the same slice :func:`list_entities` with ``limit=10, sort="docs"``
    returns). All queries are tenant-scoped and parameterized. Read-only.
    """
    type_rows = conn.execute(
        "SELECT entity_type, COUNT(*) FROM graph_entities "
        "WHERE tenant_id = %s GROUP BY entity_type ORDER BY entity_type",
        (tenant_id,),
    ).fetchall()
    counts_by_type: dict[str, int] = {str(row[0]): int(row[1]) for row in type_rows}
    total_entities = sum(counts_by_type.values())

    rel_row = conn.execute(
        "SELECT COUNT(*) FROM graph_relationships WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    total_relationships = int(rel_row[0]) if rel_row is not None else 0

    comm_row = conn.execute(
        "SELECT COUNT(*) FROM graph_communities WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    total_communities = int(comm_row[0]) if comm_row is not None else 0

    top_rows = conn.execute(
        "SELECT entity_type, name, canonical_key, doc_count, description "
        "FROM graph_entities WHERE tenant_id = %s "
        "ORDER BY doc_count DESC, name ASC LIMIT 10",
        (tenant_id,),
    ).fetchall()
    top_entities = tuple(
        EntitySummary(
            entity_type=str(row[0]),
            name=str(row[1]),
            canonical_key=str(row[2]),
            doc_count=int(row[3]),
            description=str(row[4]) if row[4] is not None else None,
        )
        for row in top_rows
    )
    return GraphStats(
        counts_by_type=counts_by_type,
        total_entities=total_entities,
        total_relationships=total_relationships,
        total_communities=total_communities,
        top_entities=top_entities,
    )
