"""Tests for ``brain.graph_rag.reconcile`` — person-aspect reconcile (wave G1-b).

Two layers:

* **Live-AGE integration** (``test_db`` against the AGE test instance on port
  5434): single-doc build, edit (add/remove person), delete + orphan GC,
  idempotency skip, tenant isolation, the ``<2 persons`` no-edge case, generic
  suppression, weight = G1-a normalized lift, and the atomicity guarantees
  (reconcile rolls back relational + AGE together; the GC primitive is
  all-or-nothing). Each test bootstraps the ``AgeBackend`` and verifies BOTH the
  relational source-of-truth and the AGE graph via independent raw Cypher.
* **Orchestration units** (real Postgres relational side + a recording
  ``FakeBackend`` + injected ``person_resolver``): proves reconcile depends only
  on the ``GraphBackend`` Protocol + the resolver seam (dependency inversion),
  and exercises the empty-mentions / orphan-GC / cap branches deterministically.

All people are synthetic (alice / bob / carol / dave / erin); no PII. The schema
+ AGE graph are reset per test by the ``test_db`` fixture.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from brain.db import DEFAULT_GRAPH_NAME, connect_age, load_age
from brain.errors import GraphReconcileError
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.reconcile import (
    ReconcileConfig,
    ResolvedPerson,
    default_person_resolver,
    reconcile_document,
    remove_document,
)
from brain.graph_rag.schema import EntityMention, GraphEntity
from brain.graph_rag.weighting import normalized_lift
from brain.vault.derived_links.directory import DirectoryStore

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# Suppression-disabled ratio: cap = round(N * 1.0) = N, so no entity (df <= N) is
# ever generic. Used by every test that wants edges to materialize so the small
# test corpora don't trip the absolute generic cap (round(0.3 * N) == 0 for
# N == 1). The default-ratio behaviour is exercised by the suppression test.
_NO_SUPPRESS = 1.0


def _cfg(
    tenant_id: str = "default",
    generic_df_ratio: float = _NO_SUPPRESS,
    **kwargs: Any,
) -> ReconcileConfig:
    """Build a ReconcileConfig defaulting to no-suppression (most tests)."""
    return ReconcileConfig(
        tenant_id=tenant_id, generic_df_ratio=generic_df_ratio, **kwargs
    )


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
def _seed_directory(
    conn: psycopg.Connection[Any], pairs: Sequence[tuple[str, str]]
) -> None:
    """Insert ``(display_name, email)`` directory rows (source='gmail')."""
    store = DirectoryStore(conn)
    for name, email in pairs:
        store.upsert_pair(display_name=name, email=email, source="gmail")


def _seed_gmail_doc(
    conn: psycopg.Connection[Any],
    *,
    external_id: str,
    participants: Sequence[tuple[str, str]],
    content: str = "body",
) -> str:
    """Insert a sources+documents pair for a gmail doc; return the doc id.

    The first participant becomes ``from``; the rest join into ``to``. Content is
    salted so the global ``content_hash`` UNIQUE never collides.
    """
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('gmail', %s, '{}'::jsonb) RETURNING id",
        (external_id,),
    ).fetchone()
    assert src_row is not None
    source_id = src_row[0]

    from_hdr = f"{participants[0][0]} <{participants[0][1]}>"
    to_hdr = ", ".join(f"{n} <{e}>" for n, e in participants[1:])
    metadata = {"from": from_hdr, "to": to_hdr, "thread_id": external_id}

    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type, metadata)
        VALUES (%s, %s, %s, %s, 'email', %s::jsonb)
        RETURNING id::text
        """,
        (source_id, external_id, salted, content_hash, json.dumps(metadata)),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _set_doc_participants(
    conn: psycopg.Connection[Any],
    document_id: str,
    participants: Sequence[tuple[str, str]],
) -> None:
    """Rewrite a doc's gmail from/to metadata to a new participant set (edit)."""
    from_hdr = f"{participants[0][0]} <{participants[0][1]}>"
    to_hdr = ", ".join(f"{n} <{e}>" for n, e in participants[1:])
    conn.execute(
        "UPDATE documents SET metadata = metadata || %s::jsonb WHERE id = %s",
        (json.dumps({"from": from_hdr, "to": to_hdr}), document_id),
    )


def _seed_manual_doc(conn: psycopg.Connection[Any], *, external_id: str) -> str:
    """Insert a manual note (no participants → resolves to zero persons)."""
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('manual', %s, '{}'::jsonb) RETURNING id",
        (external_id,),
    ).fetchone()
    assert src_row is not None
    salted = f"note\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type)
        VALUES (%s, %s, %s, %s, 'note')
        RETURNING id::text
        """,
        (src_row[0], external_id, salted, content_hash),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


# --------------------------------------------------------------------------- #
# Relational assertions
# --------------------------------------------------------------------------- #
def _person_keys(conn: psycopg.Connection[Any], tenant: str) -> set[str]:
    rows = conn.execute(
        "SELECT canonical_key FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = 'person'",
        (tenant,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _entity_id(conn: psycopg.Connection[Any], tenant: str, canonical_key: str) -> str:
    row = conn.execute(
        "SELECT id::text FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = 'person' AND canonical_key = %s",
        (tenant, canonical_key),
    ).fetchone()
    assert row is not None, f"no entity for {canonical_key!r}"
    return str(row[0])


def _mention_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_entity_mentions WHERE tenant_id = %s",
        (tenant,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _contribution_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_edge_contributions WHERE tenant_id = %s",
        (tenant,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _relationships(
    conn: psycopg.Connection[Any], tenant: str
) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        "SELECT src_id::text, dst_id::text, weight FROM graph_relationships "
        "WHERE tenant_id = %s",
        (tenant,),
    ).fetchall()
    return {(str(s), str(d)): float(w) for s, d, w in rows}


# --------------------------------------------------------------------------- #
# AGE assertions (independent raw Cypher, not via the backend under test)
# --------------------------------------------------------------------------- #
def _cypher_scalar(
    conn: psycopg.Connection[Any], query: str, params: Mapping[str, Any]
) -> list[tuple[Any, ...]]:
    # AGE must be loaded once per backend session before ``cypher()`` is
    # callable, or it raises ``unhandled cypher(cstring) function call``. Done
    # HERE rather than relied upon from elsewhere: this helper used to inherit
    # the ``LOAD`` that ``conftest``'s ``_reset_age_graph`` happened to issue on
    # the same connection, which only ran on the ~17% of resets that had a graph
    # to drop — so these assertions passed on borrowed state and would fail
    # whenever the reset took its early-out.
    load_age(conn)
    conn.execute('SET search_path = ag_catalog, "$user", public')
    try:
        rows = conn.execute(
            f"SELECT * FROM ag_catalog.cypher('{DEFAULT_GRAPH_NAME}', "
            f"$$ {query} $$, %s::ag_catalog.agtype) AS (v ag_catalog.agtype)",
            (json.dumps(params),),
        ).fetchall()
    finally:
        conn.execute("RESET search_path")
    return rows


def _age_entity_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn, "MATCH (e:Entity {tenant_id: $t}) RETURN count(e)", {"t": tenant}
    )
    return int(str(rows[0][0]))


def _age_document_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn, "MATCH (d:Document {tenant_id: $t}) RETURN count(d)", {"t": tenant}
    )
    return int(str(rows[0][0]))


def _age_mention_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn,
        "MATCH ()-[r:MENTIONED_IN {tenant_id: $t}]->() RETURN count(r)",
        {"t": tenant},
    )
    return int(str(rows[0][0]))


def _age_cooccur_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn,
        "MATCH ()-[r:CO_OCCURS {tenant_id: $t}]->() RETURN count(r)",
        {"t": tenant},
    )
    return int(str(rows[0][0]))


def _age_entity_uuids(conn: psycopg.Connection[Any], tenant: str) -> set[str]:
    rows = _cypher_scalar(
        conn,
        "MATCH (e:Entity {tenant_id: $t}) RETURN e.entity_uuid",
        {"t": tenant},
    )
    return {json.loads(str(r[0])) for r in rows}


def _age_entity_name(
    conn: psycopg.Connection[Any], tenant: str, entity_uuid: str
) -> str | None:
    rows = _cypher_scalar(
        conn,
        "MATCH (e:Entity {entity_uuid: $u, tenant_id: $t}) RETURN e.name",
        {"u": entity_uuid, "t": tenant},
    )
    return json.loads(str(rows[0][0])) if rows else None


def _backend(test_db: psycopg.Connection[Any]) -> AgeBackend:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    return backend


def _ge(eid: str) -> GraphEntity:
    return GraphEntity(
        id=eid, entity_type="person", name=eid[:8], canonical_key=eid[:8]
    )


# --------------------------------------------------------------------------- #
# 1. Single-doc build
# --------------------------------------------------------------------------- #
def test_single_doc_builds_entities_mentions_edges(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )

    result = reconcile_document(test_db, doc, backend=backend, config=_cfg())

    assert result.skipped is False
    assert result.person_count == 2
    assert result.mention_count == 2
    assert result.contribution_count == 1
    assert result.relationship_count == 1
    assert result.orphans_removed == 0

    # Relational source-of-truth.
    assert _person_keys(test_db, "default") == {"alice", "bob"}
    assert _mention_count(test_db, "default") == 2
    assert _contribution_count(test_db, "default") == 1
    rels = _relationships(test_db, "default")
    assert len(rels) == 1
    assert next(iter(rels.values())) == pytest.approx(1.0)

    # name is the humanized form; canonical_key the lowercase identity.
    name_row = test_db.execute(
        "SELECT name FROM graph_entities "
        "WHERE tenant_id = 'default' AND canonical_key = 'alice'"
    ).fetchone()
    assert name_row is not None and str(name_row[0]) == "Alice"

    # AGE graph mirror.
    assert _age_entity_count(test_db, "default") == 2
    assert _age_document_count(test_db, "default") == 1
    assert _age_mention_count(test_db, "default") == 2
    assert _age_cooccur_count(test_db, "default") == 1


def test_single_person_doc_creates_no_cooccur_edge(
    test_db: psycopg.Connection[Any],
) -> None:
    """A doc resolving to <2 persons yields a mention but no person-person edge."""
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com")])
    # bob has no directory entry → unresolved → only alice resolves.
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )

    result = reconcile_document(test_db, doc, backend=backend, config=_cfg())

    assert result.person_count == 1
    assert result.mention_count == 1
    assert result.contribution_count == 0
    assert result.relationship_count == 0
    assert _person_keys(test_db, "default") == {"alice"}
    assert _age_entity_count(test_db, "default") == 1
    assert _age_cooccur_count(test_db, "default") == 0


# --------------------------------------------------------------------------- #
# 2. Idempotency
# --------------------------------------------------------------------------- #
def test_reconcile_is_idempotent_skip(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )

    first = reconcile_document(test_db, doc, backend=backend, config=_cfg())
    assert first.skipped is False

    second = reconcile_document(test_db, doc, backend=backend, config=_cfg())
    assert second.skipped is True

    # Identical graph after the skipped re-run.
    assert _person_keys(test_db, "default") == {"alice", "bob"}
    assert _mention_count(test_db, "default") == 2
    assert _age_entity_count(test_db, "default") == 2
    assert _age_cooccur_count(test_db, "default") == 1


def test_suppress_ver_change_forces_reindex(
    test_db: psycopg.Connection[Any],
) -> None:
    """A different generic ratio changes suppress_ver → re-reconcile, not skip."""
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )

    reconcile_document(test_db, doc, backend=backend, config=_cfg())
    again = reconcile_document(
        test_db, doc, backend=backend, config=_cfg(generic_df_ratio=0.5)
    )
    assert again.skipped is False


def test_display_name_change_reindexes(test_db: psycopg.Connection[Any]) -> None:
    """A resolver display_name change (same canonical_key) re-indexes the name.

    Regression for the watermark under-invalidation Codex flagged: inputs_hash
    must include the display_name, not just the canonical_key, or a renamed
    person would leave a stale ``name`` on the catalog row + AGE vertex.
    """
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1")
    state = {"name": "Alice"}

    def resolver(
        conn: Any,
        document_id: str,
        *,
        owner_keys: frozenset[str],
        sender_denylist: frozenset[str] = frozenset(),
    ) -> list[ResolvedPerson]:
        return [ResolvedPerson("alice", state["name"])]

    first = reconcile_document(
        test_db, doc, backend=backend, config=_cfg(), person_resolver=resolver
    )
    assert first.skipped is False

    # Same canonical_key, different display_name.
    state["name"] = "Alice B."
    second = reconcile_document(
        test_db, doc, backend=backend, config=_cfg(), person_resolver=resolver
    )
    assert second.skipped is False

    name_row = test_db.execute(
        "SELECT name FROM graph_entities "
        "WHERE tenant_id = 'default' AND canonical_key = 'alice'"
    ).fetchone()
    assert name_row is not None and str(name_row[0]) == "Alice B."
    alice = _entity_id(test_db, "default", "alice")
    assert _age_entity_name(test_db, "default", alice) == "Alice B."


# --------------------------------------------------------------------------- #
# 3. Edit
# --------------------------------------------------------------------------- #
def test_edit_adds_person(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_directory(
        test_db,
        [("alice", "alice@x.com"), ("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    reconcile_document(test_db, doc, backend=backend, config=_cfg())

    # Add carol to the doc; the changed person set busts the watermark.
    _set_doc_participants(
        test_db,
        doc,
        [("alice", "alice@x.com"), ("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    result = reconcile_document(test_db, doc, backend=backend, config=_cfg())

    assert result.skipped is False
    assert result.person_count == 3
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}
    assert _mention_count(test_db, "default") == 3
    # Complete graph over 3 persons → 3 pairs.
    assert _contribution_count(test_db, "default") == 3
    assert result.relationship_count == 3
    assert _age_cooccur_count(test_db, "default") == 3


def test_edit_removes_person_gcs_orphan(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    reconcile_document(test_db, doc, backend=backend, config=_cfg())
    bob_id = _entity_id(test_db, "default", "bob")

    # Drop bob (in no other doc) → bob orphaned + GC'd everywhere.
    _set_doc_participants(test_db, doc, [("alice", "alice@x.com")])
    result = reconcile_document(test_db, doc, backend=backend, config=_cfg())

    assert result.orphans_removed == 1
    assert result.contribution_count == 0
    assert _person_keys(test_db, "default") == {"alice"}
    assert _mention_count(test_db, "default") == 1
    assert _relationships(test_db, "default") == {}
    # AGE: bob's vertex GC'd, only alice remains, no co-occurrence edge.
    remaining = _age_entity_uuids(test_db, "default")
    assert remaining == {_entity_id(test_db, "default", "alice")}
    assert bob_id not in remaining
    assert _age_cooccur_count(test_db, "default") == 0


def test_edit_to_zero_persons_removes_document_vertex(
    test_db: psycopg.Connection[Any],
) -> None:
    """Editing a doc down to zero resolvable persons removes its Document vertex."""
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    reconcile_document(test_db, doc, backend=backend, config=_cfg())
    assert _age_document_count(test_db, "default") == 1

    # Replace participants with people who have no directory entry → 0 persons.
    _set_doc_participants(
        test_db, doc, [("ghost", "ghost@x.com"), ("phantom", "phantom@x.com")]
    )
    result = reconcile_document(test_db, doc, backend=backend, config=_cfg())

    assert result.person_count == 0
    assert result.orphans_removed == 2
    assert _person_keys(test_db, "default") == set()
    assert _age_entity_count(test_db, "default") == 0
    assert _age_document_count(test_db, "default") == 0


# --------------------------------------------------------------------------- #
# 4. Delete
# --------------------------------------------------------------------------- #
def test_remove_document_deletes_and_gcs(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    reconcile_document(test_db, doc, backend=backend, config=_cfg())

    result = remove_document(test_db, doc, backend=backend, config=_cfg())

    assert result.orphans_removed == 2
    assert _person_keys(test_db, "default") == set()
    assert _mention_count(test_db, "default") == 0
    assert _contribution_count(test_db, "default") == 0
    assert _relationships(test_db, "default") == {}
    # AGE fully cleared for the tenant.
    assert _age_entity_count(test_db, "default") == 0
    assert _age_document_count(test_db, "default") == 0
    assert _age_cooccur_count(test_db, "default") == 0
    # Watermark gone.
    state = test_db.execute(
        "SELECT count(*) FROM graph_index_state WHERE document_id = %s", (doc,)
    ).fetchone()
    assert state is not None and int(state[0]) == 0


def test_remove_document_keeps_shared_person(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    _seed_directory(
        test_db,
        [("alice", "alice@x.com"), ("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    doc1 = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    doc2 = _seed_gmail_doc(
        test_db,
        external_id="m2",
        participants=[("alice", "alice@x.com"), ("carol", "carol@x.com")],
    )
    reconcile_document(test_db, doc1, backend=backend, config=_cfg())
    reconcile_document(test_db, doc2, backend=backend, config=_cfg())
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}

    # Remove doc1: bob (only in doc1) is GC'd; alice + carol (in doc2) survive.
    # SAME config (ratio) reconcile used, so doc2's surviving edge isn't
    # suppressed in the now-smaller corpus.
    result = remove_document(test_db, doc1, backend=backend, config=_cfg())

    assert result.orphans_removed == 1
    assert _person_keys(test_db, "default") == {"alice", "carol"}
    assert _age_entity_uuids(test_db, "default") == {
        _entity_id(test_db, "default", "alice"),
        _entity_id(test_db, "default", "carol"),
    }
    # doc2's single pair remains.
    assert _contribution_count(test_db, "default") == 1
    assert _age_cooccur_count(test_db, "default") == 1


def test_remove_document_is_idempotent(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    reconcile_document(test_db, doc, backend=backend, config=_cfg())
    remove_document(test_db, doc, backend=backend, config=_cfg())
    # Second removal converges to the same empty graph (no error).
    again = remove_document(test_db, doc, backend=backend, config=_cfg())
    assert again.orphans_removed == 0
    assert _age_entity_count(test_db, "default") == 0


# --------------------------------------------------------------------------- #
# 5. Tenant isolation
# --------------------------------------------------------------------------- #
def test_tenant_isolation(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_directory(
        test_db,
        [("alice", "alice@x.com"), ("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    doc_a = _seed_gmail_doc(
        test_db,
        external_id="ma",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    doc_b = _seed_gmail_doc(
        test_db,
        external_id="mb",
        participants=[("alice", "alice@x.com"), ("carol", "carol@x.com")],
    )

    reconcile_document(test_db, doc_a, backend=backend, config=_cfg("tenant-a"))
    reconcile_document(test_db, doc_b, backend=backend, config=_cfg("tenant-b"))

    assert _person_keys(test_db, "tenant-a") == {"alice", "bob"}
    assert _person_keys(test_db, "tenant-b") == {"alice", "carol"}
    # tenant-a's alice is a DISTINCT row/vertex from tenant-b's alice.
    assert _entity_id(test_db, "tenant-a", "alice") != _entity_id(
        test_db, "tenant-b", "alice"
    )
    assert _age_entity_count(test_db, "tenant-a") == 2
    assert _age_entity_count(test_db, "tenant-b") == 2

    # Removing doc_b from tenant-b leaves tenant-a completely untouched.
    remove_document(test_db, doc_b, backend=backend, config=_cfg("tenant-b"))
    assert _person_keys(test_db, "tenant-a") == {"alice", "bob"}
    assert _age_entity_count(test_db, "tenant-a") == 2
    assert _age_cooccur_count(test_db, "tenant-a") == 1
    assert _age_entity_count(test_db, "tenant-b") == 0


# --------------------------------------------------------------------------- #
# 6. Weights = G1-a normalized lift
# --------------------------------------------------------------------------- #
def test_weights_match_normalized_lift(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_directory(
        test_db,
        [("alice", "alice@x.com"), ("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    # alice-bob co-occur in 1 doc; alice & bob each appear in 2 docs.
    doc1 = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    doc2 = _seed_gmail_doc(
        test_db,
        external_id="m2",
        participants=[("alice", "alice@x.com"), ("carol", "carol@x.com")],
    )
    doc3 = _seed_gmail_doc(
        test_db,
        external_id="m3",
        participants=[("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    for doc in (doc1, doc2, doc3):
        reconcile_document(test_db, doc, backend=backend, config=_cfg())

    alice = _entity_id(test_db, "default", "alice")
    bob = _entity_id(test_db, "default", "bob")
    rels = _relationships(test_db, "default")
    assert len(rels) == 3  # 3 distinct pairs

    # alice-bob: co_doc=1, df(alice)=2, df(bob)=2 → lift = 1/min(2,2) = 0.5.
    pair = (alice, bob) if alice < bob else (bob, alice)
    assert rels[pair] == pytest.approx(normalized_lift(1, 2, 2))
    assert rels[pair] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# 7. Generic suppression (default ratio)
# --------------------------------------------------------------------------- #
def test_generic_entity_edges_are_suppressed(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    people = [
        ("alice", "alice@x.com"),
        ("bob", "bob@x.com"),
        ("carol", "carol@x.com"),
        ("dave", "dave@x.com"),
        ("erin", "erin@x.com"),
    ]
    _seed_directory(test_db, people)
    # 5 docs: alice in 4 (df=4); bob,carol in 2; dave,erin in 1.
    # corpus_N=5, default cap=round(0.3*5)=2 → alice (df=4>2) is generic.
    pairs = [
        ("alice", "bob"),
        ("alice", "carol"),
        ("alice", "dave"),
        ("alice", "erin"),
        ("bob", "carol"),
    ]
    addr = dict(people)
    # Default generic ratio (ReconcileConfig() ⇒ DEFAULT_GENERIC_DF = 0.30).
    default_cfg = ReconcileConfig()
    for idx, (a, b) in enumerate(pairs):
        doc = _seed_gmail_doc(
            test_db,
            external_id=f"m{idx}",
            participants=[(a, addr[a]), (b, addr[b])],
        )
        reconcile_document(test_db, doc, backend=backend, config=default_cfg)

    rels = _relationships(test_db, "default")
    bob = _entity_id(test_db, "default", "bob")
    carol = _entity_id(test_db, "default", "carol")
    # Only the non-generic bob-carol edge survives; every alice-* edge dropped.
    expected = (bob, carol) if bob < carol else (carol, bob)
    assert set(rels) == {expected}
    assert rels[expected] == pytest.approx(normalized_lift(1, 2, 2))
    assert _age_cooccur_count(test_db, "default") == 1
    # All five persons still have mentions (suppression only drops edges).
    assert _person_keys(test_db, "default") == {
        "alice",
        "bob",
        "carol",
        "dave",
        "erin",
    }


# --------------------------------------------------------------------------- #
# 8. Atomicity (relational + AGE roll back together; GC all-or-nothing)
# --------------------------------------------------------------------------- #
class _FailRefreshBackend(AgeBackend):
    """AgeBackend whose CO_OCCURS refresh always fails — to prove rollback."""

    def refresh_cooccur_edges(
        self, conn: psycopg.Connection[Any], tenant_id: str
    ) -> int:
        raise RuntimeError("boom-refresh")


def test_reconcile_rolls_back_relational_and_age_on_failure(
    test_db: psycopg.Connection[Any],
) -> None:
    """A mid-sync failure rolls back BOTH relational writes AND AGE changes.

    Runs on a realistic ``autocommit=False`` connection (the G1-c contract): the
    reconcile transaction must be top-level (not a SAVEPOINT under a pre-opened
    implicit txn), so a failure leaves no partial graph and no dangling
    transaction.
    """
    _backend(test_db)  # bootstrap AGE labels (committed) for the second conn
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )

    with connect_age(TEST_DATABASE_URL) as conn2:
        assert conn2.autocommit is False
        with pytest.raises(RuntimeError, match="boom-refresh"):
            reconcile_document(
                conn2, doc, backend=_FailRefreshBackend(), config=_cfg()
            )
        # reconcile's top-level transaction rolled back AND closed — no dangling
        # transaction left open on the caller's connection.
        assert conn2.info.transaction_status == TransactionStatus.IDLE

    # Committed state is empty: relational AND AGE rolled back together.
    assert _person_keys(test_db, "default") == set()
    assert _mention_count(test_db, "default") == 0
    assert _contribution_count(test_db, "default") == 0
    assert _age_entity_count(test_db, "default") == 0
    assert _age_document_count(test_db, "default") == 0


def test_detach_delete_entities_is_atomic_on_failure(
    test_db: psycopg.Connection[Any],
) -> None:
    """The GC primitive is all-or-nothing: a mid-batch failure deletes nothing.

    Directly exercises the standalone (autocommit) caller path — the loop must be
    wrapped in its own transaction so a partial delete is impossible.
    """
    backend = _backend(test_db)
    e1 = "11111111-1111-4111-8111-111111111111"
    e2 = "22222222-2222-4222-8222-222222222222"
    backend.upsert_entities(test_db, "default", [_ge(e1), _ge(e2)])
    assert _age_entity_count(test_db, "default") == 2

    real_cypher = backend._cypher
    state = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        state["n"] += 1
        # e1: count (1) + delete (2) succeed; e2: count (3) fails mid-batch.
        if state["n"] == 3:
            raise psycopg.OperationalError("boom-gc")
        return real_cypher(*args, **kwargs)

    with (
        patch.object(backend, "_cypher", side_effect=flaky),
        pytest.raises(psycopg.OperationalError, match="boom-gc"),
    ):
        backend.detach_delete_entities(test_db, "default", [e1, e2])

    # e1's already-issued delete rolled back with the failed batch.
    assert _age_entity_count(test_db, "default") == 2


# --------------------------------------------------------------------------- #
# 9. Zero-person doc / errors / resolver
# --------------------------------------------------------------------------- #
def test_manual_doc_creates_no_graph_presence(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1")

    result = reconcile_document(test_db, doc, backend=backend, config=_cfg())

    assert result.person_count == 0
    assert result.mention_count == 0
    assert _person_keys(test_db, "default") == set()
    assert _age_entity_count(test_db, "default") == 0
    assert _age_document_count(test_db, "default") == 0
    # Re-running skips on the unchanged (empty) watermark.
    again = reconcile_document(test_db, doc, backend=backend, config=_cfg())
    assert again.skipped is True


def test_reconcile_missing_document_raises(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    missing = str(uuid.uuid4())
    with pytest.raises(GraphReconcileError, match="not found"):
        reconcile_document(test_db, missing, backend=backend, config=_cfg())


def test_reconcile_rejects_bad_max_entities(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1")
    with pytest.raises(GraphReconcileError, match="max_entities_per_doc"):
        reconcile_document(
            test_db, doc, backend=backend, config=_cfg(max_entities_per_doc=0)
        )


def test_default_person_resolver_filters_owner_and_unknown(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    persons = default_person_resolver(
        test_db, doc, owner_keys=frozenset({"alice@x.com"})
    )
    assert [p.canonical_key for p in persons] == ["bob"]
    assert persons[0].display_name == "Bob"


def test_default_person_resolver_drops_automated_sender(
    test_db: psycopg.Connection[Any],
) -> None:
    """A no-reply sender never becomes a graph person (Phase 1 A.1)."""
    _seed_directory(
        test_db,
        [("acme notifications", "no-reply@acme.example.com"), ("bob", "bob@x.com")],
    )
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[
            ("acme notifications", "no-reply@acme.example.com"),
            ("bob", "bob@x.com"),
        ],
    )
    persons = default_person_resolver(test_db, doc)
    assert [p.canonical_key for p in persons] == ["bob"]


def test_default_person_resolver_drops_owner_variant_keeps_distinct_person(
    test_db: psycopg.Connection[Any],
) -> None:
    """Owner first-name leak filtered, but a distinct same-first-name person is
    KEPT (Phase 1 A.2; no over-filtering). Owner name is SYNTHETIC."""
    # "pat" is the owner's leaked first-name variant; "pat rivera" is a distinct
    # person sharing the first name.
    _seed_directory(
        test_db,
        [("pat", "pat.leak@x.com"), ("pat rivera", "pat.rivera@x.com")],
    )
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("pat", "pat.leak@x.com"), ("pat rivera", "pat.rivera@x.com")],
    )
    persons = default_person_resolver(
        test_db, doc, owner_keys=frozenset({"pat owner", "pat.owner@x.com"})
    )
    # The leaked first-name variant is dropped; the distinct person survives.
    assert [p.canonical_key for p in persons] == ["pat rivera"]


def test_default_person_resolver_merges_separator_variants(
    test_db: psycopg.Connection[Any],
) -> None:
    """Handle-style and spaced directory names collapse to one canonical key."""
    _seed_directory(
        test_db, [("jane.doe", "jane@x.com"), ("jane doe", "jane.alt@x.com")]
    )
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("jane.doe", "jane@x.com"), ("jane doe", "jane.alt@x.com")],
    )
    persons = default_person_resolver(test_db, doc)
    assert [p.canonical_key for p in persons] == ["jane doe"]
    assert persons[0].display_name == "Jane Doe"


def test_default_person_resolver_honors_sender_denylist(
    test_db: psycopg.Connection[Any],
) -> None:
    """``sender_denylist`` extends the automated-sender filter (Phase 1 knob)."""
    _seed_directory(
        test_db,
        [("billing team", "billing@acme.example.com"), ("bob", "bob@x.com")],
    )
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[
            ("billing team", "billing@acme.example.com"),
            ("bob", "bob@x.com"),
        ],
    )
    persons = default_person_resolver(
        test_db, doc, sender_denylist=frozenset({"billing@"})
    )
    assert [p.canonical_key for p in persons] == ["bob"]


def test_default_person_resolver_missing_doc_returns_empty(
    test_db: psycopg.Connection[Any],
) -> None:
    assert default_person_resolver(test_db, str(uuid.uuid4())) == []


def test_gc_primitives_empty_input_are_noops(
    test_db: psycopg.Connection[Any],
) -> None:
    """The backend GC primitives short-circuit (return 0) on an empty list."""
    backend = _backend(test_db)
    assert backend.detach_delete_entities(test_db, "default", []) == 0
    assert backend.detach_delete_documents(test_db, "default", []) == 0


# --------------------------------------------------------------------------- #
# 10. Orchestration units — FakeBackend + injected resolver (no AGE)
# --------------------------------------------------------------------------- #
class FakeBackend:
    """Records GraphBackend calls; satisfies the Protocol for orchestration tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def bootstrap(self, conn: Any) -> None:  # pragma: no cover - unused here
        self.calls.append(("bootstrap", None))

    def upsert_entities(
        self, conn: Any, tenant_id: str, entities: Sequence[GraphEntity]
    ) -> int:
        self.calls.append(("upsert_entities", [e.canonical_key for e in entities]))
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
        self.calls.append(("upsert_mention_edges", document_props))
        return len(mentions)

    def refresh_cooccur_edges(self, conn: Any, tenant_id: str) -> int:
        self.calls.append(("refresh_cooccur_edges", tenant_id))
        return 0

    def traverse(self, *args: Any, **kwargs: Any) -> list[Any]:  # pragma: no cover
        self.calls.append(("traverse", None))
        return []

    def scope_person(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls.append(("scope_person", None))
        return None

    def detach_delete_entities(
        self, conn: Any, tenant_id: str, entity_uuids: Sequence[str]
    ) -> int:
        self.calls.append(("detach_delete_entities", list(entity_uuids)))
        return len(entity_uuids)

    def detach_delete_documents(
        self, conn: Any, tenant_id: str, document_uuids: Sequence[str]
    ) -> int:
        self.calls.append(("detach_delete_documents", list(document_uuids)))
        return len(document_uuids)

    def clear_tenant(self, conn: Any, tenant_id: str) -> int:  # pragma: no cover
        self.calls.append(("clear_tenant", tenant_id))
        return 0

    def drop_graph(self, conn: Any, tenant_id: str) -> int:  # pragma: no cover
        self.calls.append(("drop_graph", tenant_id))
        return 0

    def method_names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _static_resolver(persons: list[ResolvedPerson]) -> Any:
    def _resolve(
        conn: Any,
        document_id: str,
        *,
        owner_keys: frozenset[str],
        sender_denylist: frozenset[str] = frozenset(),
    ) -> list[ResolvedPerson]:
        return persons

    return _resolve


def test_orchestration_sequence_with_fake_backend(
    test_db: psycopg.Connection[Any],
) -> None:
    """reconcile orchestrates the Protocol primitives in the right order."""
    doc = _seed_manual_doc(test_db, external_id="n1")
    backend = FakeBackend()
    resolver = _static_resolver(
        [
            ResolvedPerson("alice", "Alice"),
            ResolvedPerson("bob", "Bob"),
        ]
    )

    result = reconcile_document(
        test_db, doc, backend=backend, config=_cfg(), person_resolver=resolver
    )

    assert result.person_count == 2
    assert result.mention_count == 2
    assert result.contribution_count == 1
    # MERGE vertices → recreate MENTIONED_IN → rematerialize CO_OCCURS.
    assert backend.method_names() == [
        "upsert_entities",
        "upsert_mention_edges",
        "refresh_cooccur_edges",
    ]
    # Document vertex tagged with the real content_type.
    props = backend.calls[1][1]
    assert props == {"content_type": "note"}


def test_orchestration_empty_persons_deletes_document_vertex(
    test_db: psycopg.Connection[Any],
) -> None:
    doc = _seed_manual_doc(test_db, external_id="n1")
    backend = FakeBackend()
    resolver = _static_resolver([])

    reconcile_document(
        test_db, doc, backend=backend, config=_cfg(), person_resolver=resolver
    )

    # No mentions → detach the Document vertex instead of upserting edges.
    assert backend.method_names() == [
        "detach_delete_documents",
        "refresh_cooccur_edges",
    ]
    assert backend.calls[0][1] == [doc]


def test_orchestration_caps_person_set(test_db: psycopg.Connection[Any]) -> None:
    doc = _seed_manual_doc(test_db, external_id="n1")
    backend = FakeBackend()
    resolver = _static_resolver(
        [
            ResolvedPerson("alice", "Alice"),
            ResolvedPerson("bob", "Bob"),
            ResolvedPerson("carol", "Carol"),
        ]
    )

    result = reconcile_document(
        test_db,
        doc,
        backend=backend,
        config=_cfg(max_entities_per_doc=2),
        person_resolver=resolver,
    )

    # Capped to the 2 lexicographically-first canonical keys.
    assert result.person_count == 2
    assert _person_keys(test_db, "default") == {"alice", "bob"}
    assert backend.calls[0][1] == ["alice", "bob"]
