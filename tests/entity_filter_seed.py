"""Synthetic seeders for the ``--entity`` search filter tests (RM-24).

Every document, entity name and key here is invented. The entity rows are written
directly, as the graph build would leave them, so the tests need no Ollama and no AGE graph:
the filter reads only the relational tables from migration 012.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.ingest import Embedder, ExtractedDoc, ingest_document

TENANT = "default"
OTHER_TENANT = "tenant-b"

#: One word every seeded body carries, so the FTS leg matches every document.
SHARED_TERM = "lanternfish"


def seed_document(
    conn: psycopg.Connection[Any], embedder: Embedder, title: str
) -> str:
    """Ingest one synthetic note whose body carries :data:`SHARED_TERM`; return its id."""
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=f"{title} talks about the {SHARED_TERM} survey.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=f"manual:{title}",
    )
    assert result.document_id is not None
    return result.document_id


def seed_entity(
    conn: psycopg.Connection[Any],
    *,
    name: str,
    canonical_key: str,
    entity_type: str = "project",
    tenant_id: str = TENANT,
) -> str:
    """Insert one ``graph_entities`` row; return its id."""
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key)"
        " VALUES (%s, %s, %s, %s) RETURNING id::text",
        (tenant_id, entity_type, name, canonical_key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def seed_mention(
    conn: psycopg.Connection[Any],
    entity_id: str,
    document_id: str,
    *,
    tenant_id: str = TENANT,
) -> None:
    """Record that ``document_id`` mentions ``entity_id``."""
    conn.execute(
        "INSERT INTO graph_entity_mentions (tenant_id, entity_id, document_id, source)"
        " VALUES (%s, %s, %s, 'extractor:fake@test')",
        (tenant_id, entity_id, document_id),
    )
