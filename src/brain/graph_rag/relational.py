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

from .schema import EdgeContribution, EntityMention

__all__ = [
    "delete_doc_relational",
    "fetch_doc_content",
    "fetch_doc_meta",
    "index_state",
    "read_doc_mentions",
    "rewrite_doc_relational",
    "upsert_index_state",
]


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
