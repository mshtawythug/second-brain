"""Shared seeders for the brief's "new this week" tests (RM-17): documents, entities, mentions.

Every row is synthetic. Callers date each row explicitly, so the 7-day window is deterministic.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import psycopg

TENANT = "default"


def seed_doc(
    conn: psycopg.Connection,
    *,
    ingested_at: datetime,
    draft: bool = False,
    sensitivity: str = "normal",
    title: str = "Synthetic note",
) -> str:
    """Insert one document; return its id."""
    return str(
        conn.execute(
            "INSERT INTO documents"
            " (title, content, content_hash, content_type, ingested_at, draft, sensitivity)"
            " VALUES (%s, 'synthetic body', %s, 'text', %s, %s, %s)"
            " RETURNING id::text",
            (title, str(uuid.uuid4()), ingested_at, draft, sensitivity),
        ).fetchone()[0]
    )


def seed_entity(
    conn: psycopg.Connection,
    name: str,
    *,
    created_at: datetime,
    entity_type: str = "person",
    tenant_id: str = TENANT,
) -> str:
    """Insert one graph entity; return its id."""
    return str(
        conn.execute(
            "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key, created_at)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
            (tenant_id, entity_type, name, name.lower(), created_at),
        ).fetchone()[0]
    )


def seed_mention(
    conn: psycopg.Connection, entity_id: str, document_id: str, *, tenant_id: str = TENANT
) -> None:
    """Record that ``document_id`` mentions ``entity_id``, as a participant.

    A ``people`` mention means the document lists that person, so the entity's name
    joins the document's ``metadata.participants`` too: the gated lens names an
    entity only if a record carries its name (RM-46 sec-118f LOW-1).
    """
    conn.execute(
        "INSERT INTO graph_entity_mentions (tenant_id, entity_id, document_id, source)"
        " VALUES (%s, %s, %s, 'people')",
        (tenant_id, entity_id, document_id),
    )
    conn.execute(
        "UPDATE documents SET metadata = jsonb_set(coalesce(metadata, '{}'::jsonb),"
        " '{participants}', coalesce(metadata->'participants', '[]'::jsonb)"
        " || to_jsonb((SELECT name FROM graph_entities WHERE id = %s::uuid)))"
        " WHERE id = %s::uuid",
        (entity_id, document_id),
    )
