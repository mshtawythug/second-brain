"""Shared graph-writer test doubles: a no-op backend, a static resolver, a doc seed.

Importable as ``from tests.graph_fakes import ...``. Not a ``test_*`` module, so
pytest does not collect it. Used by the graph write-lock and deadlock tests,
which drive the real reconcile / alias / community writers and care only about
their relational locking, never about what lands in AGE.

All data is synthetic; no PII.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg

from brain.graph_rag.reconcile import PersonResolver, ResolvedPerson
from brain.graph_rag.schema import EntityMention, GraphEntity


class NullBackend:
    """A ``GraphBackend`` that writes nothing and reports every call a success."""

    def bootstrap(self, conn: Any) -> None:  # pragma: no cover - unused
        return None

    def upsert_entities(
        self, conn: Any, tenant_id: str, entities: Sequence[GraphEntity]
    ) -> int:
        return len(entities)

    def upsert_mention_edges(
        self,
        conn: Any,
        tenant_id: str,
        document_id: str,
        mentions: Sequence[EntityMention],
        *,
        document_props: Mapping[str, Any] | None = None,
    ) -> int:
        return len(mentions)

    def refresh_cooccur_edges(self, conn: Any, tenant_id: str) -> int:
        return 0

    def detach_delete_entities(
        self, conn: Any, tenant_id: str, entity_uuids: Sequence[str]
    ) -> int:
        return len(entity_uuids)

    def detach_delete_documents(
        self, conn: Any, tenant_id: str, document_uuids: Sequence[str]
    ) -> int:
        return len(document_uuids)

    def clear_tenant(self, conn: Any, tenant_id: str) -> int:
        return 0

    def drop_graph(self, conn: Any, tenant_id: str) -> int:  # pragma: no cover
        return 0

    def traverse(self, *args: Any, **kwargs: Any) -> list[Any]:  # pragma: no cover
        return []

    def scope_person(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        return None


def static_resolver(persons: list[ResolvedPerson]) -> PersonResolver:
    """A ``PersonResolver`` that resolves every document to ``persons``."""

    def _resolve(
        conn: psycopg.Connection[Any],
        document_id: str,
        *,
        owner_keys: frozenset[str],
        sender_denylist: frozenset[str] = frozenset(),
    ) -> list[ResolvedPerson]:
        return persons

    return _resolve


def seed_note_doc(conn: psycopg.Connection[Any], name: str, body: str = "") -> str:
    """Insert a manual note titled ``name``; return its id.

    The body is salted, so the ``content_hash`` never collides.
    """
    src = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('manual', %s, '{}'::jsonb) RETURNING id",
        (name,),
    ).fetchone()
    assert src is not None
    salted = f"{body or name}\n<!-- {uuid.uuid4()} -->"
    row = conn.execute(
        "INSERT INTO documents (source_id, title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s, 'note') RETURNING id::text",
        (src[0], name, salted, hashlib.sha256(salted.encode()).hexdigest()),
    ).fetchone()
    assert row is not None
    return str(row[0])
