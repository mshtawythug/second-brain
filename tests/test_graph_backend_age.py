"""Live-AGE integration tests for :class:`AgeBackend` (G0-4 canary).

All run against the AGE test instance (``TEST_DATABASE_URL``, port 5434). The
``test_db`` fixture installs the ``age`` extension, drops the canonical
``brain_graph`` graph, and runs migrations — so each test starts from a fresh
``brain init``-like state. These tests prove the generated Cypher works on a
real AGE engine: bootstrap idempotency + labels/indexes, MERGE upsert
idempotency, split-MERGE mention edges, CO_OCCURS materialization from the
relational mirror, bounded traversal + Python affinity scoring, person scoping,
**tenant isolation** (a tenant-A query never sees tenant-B data), transactional
DML, and error wrapping. All UUIDs are synthetic; no PII.
"""
from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from brain.db import DEFAULT_GRAPH_NAME, connect, connect_age
from brain.errors import GraphBackendError
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.schema import EntityMention, GraphEntity

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# Synthetic, lexically-ordered UUIDs (A < B < C < D) so src_id < dst_id holds.
_A = "11111111-1111-4111-8111-111111111111"
_B = "22222222-2222-4222-8222-222222222222"
_C = "33333333-3333-4333-8333-333333333333"
_D = "44444444-4444-4444-8444-444444444444"
_DOC1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
_DOC2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
_DOC3 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc3"
_DOC4 = "dddddddd-dddd-4ddd-8ddd-ddddddddddd4"


# --------------------------------------------------------------------------- #
# Helpers — independent raw-Cypher verification (not via the backend)
# --------------------------------------------------------------------------- #
def _cypher(
    conn: psycopg.Connection[Any],
    query: str,
    params: dict[str, Any] | None = None,
    columns: str = "v ag_catalog.agtype",
) -> list[tuple[Any, ...]]:
    conn.execute('SET search_path = ag_catalog, "$user", public')
    try:
        if params is None:
            return conn.execute(
                f"SELECT * FROM ag_catalog.cypher('{DEFAULT_GRAPH_NAME}', "
                f"$$ {query} $$) AS ({columns})"
            ).fetchall()
        return conn.execute(
            f"SELECT * FROM ag_catalog.cypher('{DEFAULT_GRAPH_NAME}', "
            f"$$ {query} $$, %s::ag_catalog.agtype) AS ({columns})",
            (json.dumps(params),),
        ).fetchall()
    finally:
        conn.execute("RESET search_path")


def _entity_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher(
        conn,
        "MATCH (e:Entity {tenant_id: $t}) RETURN count(e)",
        {"t": tenant},
        "c ag_catalog.agtype",
    )
    return int(str(rows[0][0]))


def _cooccur_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher(
        conn,
        "MATCH ()-[r:CO_OCCURS {tenant_id: $t}]->() RETURN count(r)",
        {"t": tenant},
        "c ag_catalog.agtype",
    )
    return int(str(rows[0][0]))


def _mention_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher(
        conn,
        "MATCH ()-[r:MENTIONED_IN {tenant_id: $t}]->() RETURN count(r)",
        {"t": tenant},
        "c ag_catalog.agtype",
    )
    return int(str(rows[0][0]))


def _entity_name(conn: psycopg.Connection[Any], tenant: str, uuid: str) -> str | None:
    rows = _cypher(
        conn,
        "MATCH (e:Entity {entity_uuid: $u, tenant_id: $t}) RETURN e.name",
        {"u": uuid, "t": tenant},
    )
    return json.loads(str(rows[0][0])) if rows else None


def _mention_count_for(
    conn: psycopg.Connection[Any], tenant: str, entity_uuid: str
) -> int:
    rows = _cypher(
        conn,
        "MATCH (e:Entity {entity_uuid: $u})-[r:MENTIONED_IN {tenant_id: $t}]->() "
        "RETURN r.mention_count",
        {"u": entity_uuid, "t": tenant},
    )
    return int(str(rows[0][0]))


def _cooccur_weight(
    conn: psycopg.Connection[Any], tenant: str, src: str, dst: str
) -> float | None:
    rows = _cypher(
        conn,
        "MATCH (a:Entity {entity_uuid: $s})-[r:CO_OCCURS {tenant_id: $t}]->"
        "(b:Entity {entity_uuid: $d}) RETURN r.weight",
        {"s": src, "d": dst, "t": tenant},
    )
    return float(str(rows[0][0])) if rows else None


def _set_cooccur_weight(
    conn: psycopg.Connection[Any],
    tenant: str,
    src: str,
    dst: str,
    weight: float,
) -> None:
    """Directly overwrite an existing CO_OCCURS edge's ``weight`` in AGE.

    Used to plant a sentinel on the AGE edge that DIFFERS from its
    ``graph_relationships`` row, so an atomicity test can tell a true rollback
    (sentinel survives) from a delete-then-recreate (edge comes back at the
    relational weight).
    """
    _cypher(
        conn,
        "MATCH (a:Entity {entity_uuid: $s, tenant_id: $t})"
        "-[r:CO_OCCURS {tenant_id: $t}]->"
        "(b:Entity {entity_uuid: $d, tenant_id: $t}) SET r.weight = $w",
        {"s": src, "d": dst, "t": tenant, "w": weight},
    )


def _insert_entity(
    conn: psycopg.Connection[Any],
    eid: str,
    tenant: str,
    *,
    name: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, %s, 'person', %s, %s)",
        (eid, tenant, name or eid[:8], eid[:8]),
    )


def _insert_relationship(
    conn: psycopg.Connection[Any],
    tenant: str,
    src: str,
    dst: str,
    weight: float,
    *,
    co_count: int = 1,
    doc_count: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO graph_relationships "
        "(tenant_id, src_id, dst_id, weight, co_count, doc_count) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (tenant, src, dst, weight, co_count, doc_count),
    )


def _insert_document(
    conn: psycopg.Connection[Any],
    doc_id: str,
    *,
    title: str | None = None,
    ingested_at: str | None = None,
) -> None:
    """Insert a minimal ``documents`` row (FK target for relational mentions).

    ``ingested_at`` (an ISO timestamp string) is set explicitly when a test needs
    a deterministic "newest shared doc" ordering for scope-truncation tiebreaks;
    omitting it defaults to ``NOW()`` (the production default).
    """
    if ingested_at is None:
        conn.execute(
            "INSERT INTO documents (id, title, content, content_hash, content_type) "
            "VALUES (%s, %s, %s, %s, 'note')",
            (doc_id, title or doc_id[:8], f"synthetic body {doc_id[:8]}", doc_id),
        )
    else:
        conn.execute(
            "INSERT INTO documents "
            "(id, title, content, content_hash, content_type, ingested_at) "
            "VALUES (%s, %s, %s, %s, 'note', %s)",
            (
                doc_id,
                title or doc_id[:8],
                f"synthetic body {doc_id[:8]}",
                doc_id,
                ingested_at,
            ),
        )


def _insert_mention(
    conn: psycopg.Connection[Any],
    tenant: str,
    entity_id: str,
    document_id: str,
) -> None:
    """Insert a relational ``graph_entity_mentions`` row (scope_person source)."""
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) "
        "VALUES (%s, %s, %s, 1, 'people')",
        (tenant, entity_id, document_id),
    )


def _entity(eid: str, tenant: str, name: str | None = None) -> GraphEntity:
    return GraphEntity(
        id=eid,
        entity_type="person",
        name=name or eid[:8],
        canonical_key=eid[:8],
        tenant_id=tenant,
    )


def _seed_cooccur_chain(
    conn: psycopg.Connection[Any],
    backend: AgeBackend,
    tenant: str,
) -> None:
    """A-B (0.8), B-C (0.5), C-D (0.9) in ``tenant`` — relational + AGE."""
    for eid in (_A, _B, _C, _D):
        _insert_entity(conn, eid, tenant)
    _insert_relationship(conn, tenant, _A, _B, 0.8)
    _insert_relationship(conn, tenant, _B, _C, 0.5)
    _insert_relationship(conn, tenant, _C, _D, 0.9)
    backend.upsert_entities(
        conn, tenant, [_entity(e, tenant) for e in (_A, _B, _C, _D)]
    )
    backend.refresh_cooccur_edges(conn, tenant)


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
def test_bootstrap_creates_labels_and_indexes(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)

    label_rows = test_db.execute(
        "SELECT l.name FROM ag_catalog.ag_label l "
        "JOIN ag_catalog.ag_graph g ON l.graph = g.graphid "
        "WHERE g.name = %s",
        (DEFAULT_GRAPH_NAME,),
    ).fetchall()
    labels = {str(r[0]) for r in label_rows}
    assert {"Entity", "Document", "MENTIONED_IN", "CO_OCCURS"} <= labels

    index_rows = test_db.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
        (DEFAULT_GRAPH_NAME,),
    ).fetchall()
    indexes = {str(r[0]) for r in index_rows}
    assert {
        "idx_entity_tenant_id",
        "idx_entity_entity_uuid",
        "idx_entity_canonical_key",
        "idx_cooccur_weight",
    } <= indexes


def test_bootstrap_is_idempotent(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    backend.bootstrap(test_db)  # must not raise (labels already exist)

    label_rows = test_db.execute(
        "SELECT count(*) FROM ag_catalog.ag_label l "
        "JOIN ag_catalog.ag_graph g ON l.graph = g.graphid "
        "WHERE g.name = %s AND l.name = 'Entity'",
        (DEFAULT_GRAPH_NAME,),
    ).fetchone()
    assert label_rows is not None
    assert int(label_rows[0]) == 1


def test_bootstrap_requires_autocommit() -> None:
    backend = AgeBackend()
    with connect(TEST_DATABASE_URL) as conn:
        assert conn.autocommit is False
        with pytest.raises(GraphBackendError, match="autocommit"):
            backend.bootstrap(conn)
        conn.rollback()


def test_bootstrap_wraps_label_failure(test_db: psycopg.Connection) -> None:
    """A psycopg.Error during label DDL surfaces as GraphBackendError."""
    backend = AgeBackend()
    real_execute = test_db.execute
    boom = psycopg.OperationalError("simulated create_vlabel failure")

    def _fail_on_vlabel(query: object, *args: object, **kwargs: object) -> object:
        if "create_vlabel" in str(query):
            raise boom
        return real_execute(query, *args, **kwargs)

    with (
        patch.object(test_db, "execute", side_effect=_fail_on_vlabel),
        pytest.raises(GraphBackendError, match="labels/indexes"),
    ):
        backend.bootstrap(test_db)


# --------------------------------------------------------------------------- #
# upsert_entities
# --------------------------------------------------------------------------- #
def test_upsert_entities_creates_and_updates(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)

    n = backend.upsert_entities(
        test_db,
        "default",
        [_entity(_A, "default", "Alpha"), _entity(_B, "default", "Beta")],
    )
    assert n == 2
    assert _entity_count(test_db, "default") == 2
    assert _entity_name(test_db, "default", _A) == "Alpha"

    # Re-upsert is idempotent (no duplicate) and updates the mutable name.
    backend.upsert_entities(test_db, "default", [_entity(_A, "default", "Alpha-2")])
    assert _entity_count(test_db, "default") == 2
    assert _entity_name(test_db, "default", _A) == "Alpha-2"


def test_upsert_entities_cross_tenant_raises(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    with pytest.raises(GraphBackendError, match="cross-tenant"):
        backend.upsert_entities(test_db, "default", [_entity(_A, "acme")])


# --------------------------------------------------------------------------- #
# upsert_mention_edges (split MERGE)
# --------------------------------------------------------------------------- #
def test_upsert_mention_edges_split_merge_idempotent(
    test_db: psycopg.Connection,
) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    backend.upsert_entities(
        test_db, "default", [_entity(_A, "default"), _entity(_B, "default")]
    )

    mentions = [
        EntityMention(entity_id=_A, document_id=_DOC1, source="people", mention_count=2),
        EntityMention(entity_id=_B, document_id=_DOC1, source="people", mention_count=1),
    ]
    n = backend.upsert_mention_edges(
        test_db,
        "default",
        _DOC1,
        mentions,
        document_props={"content_type": "transcript"},
    )
    assert n == 2
    assert _mention_count(test_db, "default") == 2
    assert _mention_count_for(test_db, "default", _A) == 2

    # Re-run with a changed count: delete+recreate keeps it idempotent.
    backend.upsert_mention_edges(
        test_db,
        "default",
        _DOC1,
        [
            EntityMention(
                entity_id=_A, document_id=_DOC1, source="people", mention_count=9
            )
        ],
    )
    assert _mention_count(test_db, "default") == 1
    assert _mention_count_for(test_db, "default", _A) == 9


def test_upsert_mention_edges_atomic_on_partial_recreate_failure(
    test_db: psycopg.Connection,
) -> None:
    """REGRESSION (Wave 3, item 3.6): the delete-then-recreate of a document's
    MENTIONED_IN edges must be atomic.

    ``upsert_mention_edges`` DELETEs all of a doc's existing mention edges then
    CREATEs the new set. Without its own ``conn.transaction()`` (every sibling
    write has one — ``upsert_entities`` / ``refresh_cooccur_edges`` /
    ``clear_tenant``), a failure partway through the recreate loop on an
    autocommit connection leaves the DELETE committed and only some edges
    recreated — a half-rebuilt edge set. Wrapping the delete+recreate in one
    transaction makes it all-or-nothing: a mid-recreate failure rolls the DELETE
    back too, so the prior edge set survives intact.
    """
    backend = AgeBackend()
    backend.bootstrap(test_db)
    backend.upsert_entities(
        test_db, "default", [_entity(_A, "default"), _entity(_B, "default")]
    )
    two = [
        EntityMention(entity_id=_A, document_id=_DOC1, source="people", mention_count=1),
        EntityMention(entity_id=_B, document_id=_DOC1, source="people", mention_count=1),
    ]
    # Establish a committed 2-edge baseline for _DOC1.
    assert backend.upsert_mention_edges(test_db, "default", _DOC1, two) == 2
    assert _mention_count(test_db, "default") == 2

    # Re-run, failing on the SECOND recreate CREATE. Without the transaction the
    # DELETE (both edges) has already committed and only A's edge got recreated,
    # leaving 1 edge; with the transaction the whole rebuild rolls back to 2.
    real_cypher = backend._cypher
    creates = {"n": 0}

    def _fail_second_create(conn, query, params=None, **kwargs):  # type: ignore[no-untyped-def]
        if "CREATE (e)-[:MENTIONED_IN" in query:
            creates["n"] += 1
            if creates["n"] == 2:
                raise GraphBackendError("simulated mid-recreate failure")
        return real_cypher(conn, query, params, **kwargs)

    with (
        patch.object(backend, "_cypher", side_effect=_fail_second_create),
        pytest.raises(GraphBackendError, match="simulated mid-recreate"),
    ):
        backend.upsert_mention_edges(test_db, "default", _DOC1, two)

    # All-or-nothing: the failed rebuild rolled back, so the original 2 edges
    # survive (a non-atomic delete+recreate would have left only 1).
    assert _mention_count(test_db, "default") == 2


def test_upsert_mention_edges_scope_mismatch_raises(
    test_db: psycopg.Connection,
) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    bad = [EntityMention(entity_id=_A, document_id=_DOC2, source="people")]
    with pytest.raises(GraphBackendError, match="does not match the call scope"):
        backend.upsert_mention_edges(test_db, "default", _DOC1, bad)


@pytest.mark.parametrize("reserved_key", ["tenant_id", "document_uuid"])
def test_upsert_mention_edges_rejects_reserved_document_prop(
    test_db: psycopg.Connection, reserved_key: str
) -> None:
    """A caller document_props bag may not overwrite the Document's identity."""
    backend = AgeBackend()
    backend.bootstrap(test_db)
    backend.upsert_entities(test_db, "default", [_entity(_A, "default")])
    mentions = [EntityMention(entity_id=_A, document_id=_DOC1, source="people")]
    with pytest.raises(GraphBackendError, match="reserved identity key"):
        backend.upsert_mention_edges(
            test_db,
            "default",
            _DOC1,
            mentions,
            document_props={reserved_key: "evil", "content_type": "note"},
        )


# --------------------------------------------------------------------------- #
# refresh_cooccur_edges
# --------------------------------------------------------------------------- #
def test_refresh_cooccur_edges_materializes_and_idempotent(
    test_db: psycopg.Connection,
) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    for eid in (_A, _B, _C):
        _insert_entity(test_db, eid, "default")
    _insert_relationship(test_db, "default", _A, _B, 0.8, co_count=4, doc_count=2)
    _insert_relationship(test_db, "default", _B, _C, 0.5)
    backend.upsert_entities(
        test_db, "default", [_entity(e, "default") for e in (_A, _B, _C)]
    )

    n = backend.refresh_cooccur_edges(test_db, "default")
    assert n == 2
    assert _cooccur_count(test_db, "default") == 2
    assert _cooccur_weight(test_db, "default", _A, _B) == pytest.approx(0.8)

    # Re-running rebuilds without duplicating.
    assert backend.refresh_cooccur_edges(test_db, "default") == 2
    assert _cooccur_count(test_db, "default") == 2


def test_refresh_cooccur_edges_raises_on_missing_vertex(
    test_db: psycopg.Connection,
) -> None:
    """G0 fix #3: a relational edge whose AGE vertex is missing → loud failure.

    The relational ``graph_relationships`` mirror carries FK-valid entity rows,
    but if ``upsert_entities`` was never run the AGE Entity vertices don't exist,
    so the ``MATCH`` endpoints can't bind and the ``CREATE`` would silently
    no-op. ``refresh_cooccur_edges`` must RAISE rather than over-report
    ``len(rows)`` for a write that didn't happen (complete-or-loud-failure).
    """
    backend = AgeBackend()
    backend.bootstrap(test_db)
    # FK-valid relational rows only — NO backend.upsert_entities() call, so the
    # AGE Entity vertices for _A / _B never get created.
    _insert_entity(test_db, _A, "default")
    _insert_entity(test_db, _B, "default")
    _insert_relationship(test_db, "default", _A, _B, 0.8)

    with pytest.raises(GraphBackendError, match="endpoint Entity vertex is missing"):
        backend.refresh_cooccur_edges(test_db, "default")

    # Nothing partial leaked into the graph — the loud failure replaced the
    # silent under-write.
    assert _cooccur_count(test_db, "default") == 0


def test_refresh_cooccur_edges_atomic_on_partial_failure(
    test_db: psycopg.Connection,
) -> None:
    """G0 fix #1 (atomicity): a mixed batch with one bad endpoint → all-or-nothing.

    The delete + recreates run in one transaction, so a missing endpoint vertex
    discovered mid-batch must roll the WHOLE rebuild back. To PROVE a true
    rollback (and not merely "delete-then-recreate happened to restore A-B before
    C-D failed"), we plant a SENTINEL weight (0.123) on the existing A-B AGE edge
    that differs from its graph_relationships row (0.8): with a real rollback the
    sentinel survives; a non-atomic rebuild would drop the sentinel edge and
    recreate A-B at the relational 0.8.
    """
    backend = AgeBackend()
    backend.bootstrap(test_db)
    # Relational FK targets for A, B, C, D; AGE vertices for A, B, C only — D is
    # referenced relationally but never gets an AGE vertex.
    for eid in (_A, _B, _C, _D):
        _insert_entity(test_db, eid, "default")
    backend.upsert_entities(
        test_db, "default", [_entity(e, "default") for e in (_A, _B, _C)]
    )

    # Establish a pre-existing CO_OCCURS set: just A-B (recreated at 0.8).
    _insert_relationship(test_db, "default", _A, _B, 0.8)
    assert backend.refresh_cooccur_edges(test_db, "default") == 1
    assert _cooccur_count(test_db, "default") == 1
    assert _cooccur_weight(test_db, "default", _A, _B) == pytest.approx(0.8)

    # Plant a sentinel on the AGE edge that DIFFERS from graph_relationships
    # (still 0.8). A true rollback keeps 0.123; a delete+recreate restores 0.8.
    _set_cooccur_weight(test_db, "default", _A, _B, 0.123)
    assert _cooccur_weight(test_db, "default", _A, _B) == pytest.approx(0.123)

    # Add a second relationship C-D whose endpoint D has NO AGE vertex → the
    # batch is now mixed (A-B valid, C-D bad).
    _insert_relationship(test_db, "default", _C, _D, 0.5)

    with pytest.raises(GraphBackendError, match="endpoint Entity vertex is missing"):
        backend.refresh_cooccur_edges(test_db, "default")

    # All-or-nothing: the rebuild rolled back, so the A-B edge KEEPS its sentinel
    # 0.123 (a non-atomic recreate would have reset it to the relational 0.8) and
    # the bad C-D edge was never created.
    assert _cooccur_count(test_db, "default") == 1
    assert _cooccur_weight(test_db, "default", _A, _B) == pytest.approx(0.123)
    assert _cooccur_weight(test_db, "default", _C, _D) is None


# --------------------------------------------------------------------------- #
# traverse — the §6a canary
# --------------------------------------------------------------------------- #
def test_traverse_returns_paths_with_affinity(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    _seed_cooccur_chain(test_db, backend, "default")

    hits = backend.traverse(test_db, "default", _A, depth=2, frontier_cap=50)

    by_id = {h.entity_uuid: h for h in hits}
    assert set(by_id) == {_B, _C}  # D is 3 hops away — out of depth-2 range
    assert by_id[_B].affinity == pytest.approx(0.8)
    assert by_id[_B].hops == 1
    assert by_id[_C].affinity == pytest.approx(0.4)  # 0.8 * 0.5
    assert by_id[_C].hops == 2
    # Ordered by affinity descending.
    assert [h.entity_uuid for h in hits] == [_B, _C]


def test_traverse_depth_one_limits_frontier(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    _seed_cooccur_chain(test_db, backend, "default")

    hits = backend.traverse(test_db, "default", _A, depth=1, frontier_cap=50)
    assert {h.entity_uuid for h in hits} == {_B}


def test_traverse_min_edge_weight_excludes_weak_paths(
    test_db: psycopg.Connection,
) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    _seed_cooccur_chain(test_db, backend, "default")

    # min 0.6 excludes the B-C edge (0.5), so C (reached only via B-C) drops out.
    hits = backend.traverse(
        test_db, "default", _A, depth=2, frontier_cap=50, min_edge_weight=0.6
    )
    assert {h.entity_uuid for h in hits} == {_B}


def test_traverse_empty_for_isolated_seed(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    backend.upsert_entities(test_db, "default", [_entity(_A, "default")])

    assert backend.traverse(test_db, "default", _A, depth=2, frontier_cap=50) == []


def test_traverse_never_returns_seed_on_cycle(test_db: psycopg.Connection) -> None:
    """A triangle lets a depth-3 path cycle back to the seed — it must NOT
    appear in the results (Protocol: the seed is never returned)."""
    backend = AgeBackend()
    backend.bootstrap(test_db)
    for eid in (_A, _B, _C):
        _insert_entity(test_db, eid, "default")
    # Triangle A-B, B-C, A-C (all canonical src < dst).
    _insert_relationship(test_db, "default", _A, _B, 0.8)
    _insert_relationship(test_db, "default", _B, _C, 0.7)
    _insert_relationship(test_db, "default", _A, _C, 0.6)
    backend.upsert_entities(
        test_db, "default", [_entity(e, "default") for e in (_A, _B, _C)]
    )
    backend.refresh_cooccur_edges(test_db, "default")

    hits = backend.traverse(test_db, "default", _A, depth=3, frontier_cap=50)
    ids = {h.entity_uuid for h in hits}
    assert _A not in ids  # seed excluded despite the 3-hop cycle back to A
    assert ids == {_B, _C}


def test_traverse_cap_applied_after_scoring(test_db: psycopg.Connection) -> None:
    """The frontier cap is applied AFTER affinity scoring, so the highest-
    affinity entities are returned deterministically — a pre-scoring Cypher
    LIMIT could have dropped the best path."""
    backend = AgeBackend()
    backend.bootstrap(test_db)
    for eid in (_A, _B, _C, _D):
        _insert_entity(test_db, eid, "default")
    # A->B weak (0.3), A->C strong (0.9); B->D (0.8) gives A-B-D = 0.24 at 2 hops.
    _insert_relationship(test_db, "default", _A, _B, 0.3)
    _insert_relationship(test_db, "default", _A, _C, 0.9)
    _insert_relationship(test_db, "default", _B, _D, 0.8)
    backend.upsert_entities(
        test_db, "default", [_entity(e, "default") for e in (_A, _B, _C, _D)]
    )
    backend.refresh_cooccur_edges(test_db, "default")

    # Reachable at depth 2: C(0.9), B(0.3), D(0.24). cap=1 must keep the BEST.
    top1 = backend.traverse(test_db, "default", _A, depth=2, frontier_cap=1)
    assert [h.entity_uuid for h in top1] == [_C]
    # cap=2 keeps the top two by affinity, in order.
    top2 = backend.traverse(test_db, "default", _A, depth=2, frontier_cap=2)
    assert [h.entity_uuid for h in top2] == [_C, _B]


# --------------------------------------------------------------------------- #
# TENANT ISOLATION — the headline guarantee
# --------------------------------------------------------------------------- #
def test_traverse_tenant_isolation(test_db: psycopg.Connection) -> None:
    """A tenant-A traversal never sees tenant-B vertices or edges."""
    backend = AgeBackend()
    backend.bootstrap(test_db)
    _seed_cooccur_chain(test_db, backend, "default")
    # Same UUIDs, different tenant, different (stronger) edge.
    _insert_entity(test_db, _A, "acme")
    _insert_entity(test_db, _B, "acme")
    _insert_relationship(test_db, "acme", _A, _B, 0.99)
    backend.upsert_entities(
        test_db, "acme", [_entity(_A, "acme"), _entity(_B, "acme")]
    )
    backend.refresh_cooccur_edges(test_db, "acme")

    default_hits = backend.traverse(test_db, "default", _A, depth=2, frontier_cap=50)
    acme_hits = backend.traverse(test_db, "acme", _A, depth=2, frontier_cap=50)

    # default still sees its own chain (B at 0.8, C at 0.4), NOT the acme 0.99.
    default_by_id = {h.entity_uuid: h for h in default_hits}
    assert set(default_by_id) == {_B, _C}
    assert default_by_id[_B].affinity == pytest.approx(0.8)
    # acme sees ONLY its single B edge (0.99); never reaches C/D (no acme edges).
    assert {h.entity_uuid for h in acme_hits} == {_B}
    assert acme_hits[0].affinity == pytest.approx(0.99)
    assert all(h.tenant_id == "acme" for h in acme_hits)


# --------------------------------------------------------------------------- #
# scope_person
# --------------------------------------------------------------------------- #
def _seed_mentions(conn: psycopg.Connection[Any], tenant: str) -> None:
    """Seed the RELATIONAL source-of-truth: A, B, C co-mentioned in _DOC1.

    ``scope_person`` is an indexed relational two-hop over
    ``graph_entity_mentions`` (the G2-k OOM fix), NOT a Cypher self-join — so the
    scope tests seed the relational mirror directly rather than the AGE graph.
    """
    for eid in (_A, _B, _C):
        _insert_entity(conn, eid, tenant)
    _insert_document(conn, _DOC1)
    for eid in (_A, _B, _C):
        _insert_mention(conn, tenant, eid, _DOC1)


def test_scope_person_returns_comentioned(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    _seed_mentions(test_db, "default")

    scope = backend.scope_person(test_db, "default", _A, frontier_cap=50)

    assert scope.seed_entity_uuid == _A
    # Deterministically sorted (_B < _C); seed _A excluded.
    assert scope.entity_uuids == (_B, _C)
    assert scope.document_uuids == (_DOC1,)
    assert scope.tenant_id == "default"


def test_scope_person_tenant_isolation(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    _seed_mentions(test_db, "default")
    # acme: a different doc co-mentioning A and D only (entity _A is reused
    # across tenants — the composite (tenant_id, id) key allows it).
    _insert_entity(test_db, _A, "acme")
    _insert_entity(test_db, _D, "acme")
    _insert_document(test_db, _DOC2)
    _insert_mention(test_db, "acme", _A, _DOC2)
    _insert_mention(test_db, "acme", _D, _DOC2)

    acme_scope = backend.scope_person(test_db, "acme", _A, frontier_cap=50)
    assert acme_scope.entity_uuids == (_D,)  # never sees default's B/C
    assert acme_scope.document_uuids == (_DOC2,)


def test_scope_person_truncates_on_overflow(test_db: psycopg.Connection) -> None:
    """A person scope larger than frontier_cap is deterministically truncated to
    the strongest co-entities (bounded ranked truncation), not raised.

    Regression for the hub-person crash: ``scope_person`` used to raise
    ``GraphBackendError('person scope exceeded safe bound')`` once a person's
    co-mention rows exceeded ``frontier_cap``, making themes/audio unusable for
    exactly the hub people they matter most for. It must now keep the strongest
    ``frontier_cap`` worth of scope instead.
    """
    backend = AgeBackend()
    _seed_mentions(test_db, "default")  # _DOC1 co-mentions A, B, C (B, C co-rows)

    # 2 co-mention rows (B, C) exceed frontier_cap=1 → truncate to the strongest 1
    # (B and C tie on count=1 and share _DOC1's date → entity-id tiebreak keeps B).
    scope = backend.scope_person(test_db, "default", _A, frontier_cap=1)

    assert scope.seed_entity_uuid == _A
    assert scope.entity_uuids == (_B,)  # _B < _C deterministic tiebreak
    assert scope.document_uuids == (_DOC1,)
    assert scope.tenant_id == "default"


def test_scope_person_truncation_ranks_by_comention_frequency(
    test_db: psycopg.Connection,
) -> None:
    """Truncation keeps the highest co-mention-frequency entities; the weakest
    co-entity is dropped once the row budget is met."""
    backend = AgeBackend()
    for eid in (_A, _B, _C, _D):
        _insert_entity(test_db, eid, "default")
    for doc in (_DOC1, _DOC2, _DOC3, _DOC4):
        _insert_document(test_db, doc)
    # Seed _A appears in all four docs.
    for doc in (_DOC1, _DOC2, _DOC3, _DOC4):
        _insert_mention(test_db, "default", _A, doc)
    # _B co-mentioned in 3 docs (strongest), _C in 2, _D in 1 (weakest).
    for doc in (_DOC1, _DOC2, _DOC3):
        _insert_mention(test_db, "default", _B, doc)
    for doc in (_DOC1, _DOC2):
        _insert_mention(test_db, "default", _C, doc)
    _insert_mention(test_db, "default", _D, _DOC4)

    # total rows = 3 (B) + 2 (C) + 1 (D) = 6 > 5 → truncate. Greedy by rank:
    # B (3) fits; C (3+2=5) fits and hits the cap; D would overflow → dropped.
    scope = backend.scope_person(test_db, "default", _A, frontier_cap=5)

    assert scope.entity_uuids == (_B, _C)  # _D (weakest) dropped, sorted asc
    assert scope.document_uuids == (_DOC1, _DOC2, _DOC3)  # union of B+C docs


def test_scope_person_truncation_tiebreak_newest_doc(
    test_db: psycopg.Connection,
) -> None:
    """Entities tied on co-mention count are ranked by newest shared doc first."""
    backend = AgeBackend()
    for eid in (_A, _B, _C):
        _insert_entity(test_db, eid, "default")
    # _DOC1 (older) co-mentions A+C; _DOC2 (newer) co-mentions A+B. B and C both
    # have count=1, so the newest-shared-doc tiebreak ranks B (newer _DOC2) above
    # C (older _DOC1).
    _insert_document(test_db, _DOC1, ingested_at="2020-01-01T00:00:00+00:00")
    _insert_document(test_db, _DOC2, ingested_at="2025-01-01T00:00:00+00:00")
    _insert_mention(test_db, "default", _A, _DOC1)
    _insert_mention(test_db, "default", _A, _DOC2)
    _insert_mention(test_db, "default", _C, _DOC1)
    _insert_mention(test_db, "default", _B, _DOC2)

    # 2 co-rows (B, C) > frontier_cap=1 → keep the newer-doc entity B.
    scope = backend.scope_person(test_db, "default", _A, frontier_cap=1)

    assert scope.entity_uuids == (_B,)
    assert scope.document_uuids == (_DOC2,)


def test_scope_person_truncation_caps_documents_for_dominant_entity(
    test_db: psycopg.Connection,
) -> None:
    """A single co-entity whose shared-doc count alone exceeds the cap is still
    kept (never an empty scope), but its connecting docs are bounded to the cap,
    keeping the newest."""
    backend = AgeBackend()
    _insert_entity(test_db, _A, "default")
    _insert_entity(test_db, _B, "default")
    # _B shares 3 docs with _A; cap=2 → keep _B but only the 2 newest docs.
    _insert_document(test_db, _DOC1, ingested_at="2020-01-01T00:00:00+00:00")
    _insert_document(test_db, _DOC2, ingested_at="2022-01-01T00:00:00+00:00")
    _insert_document(test_db, _DOC3, ingested_at="2024-01-01T00:00:00+00:00")
    for doc in (_DOC1, _DOC2, _DOC3):
        _insert_mention(test_db, "default", _A, doc)
        _insert_mention(test_db, "default", _B, doc)

    scope = backend.scope_person(test_db, "default", _A, frontier_cap=2)

    assert scope.entity_uuids == (_B,)
    # 3 shared docs > cap=2 → keep the 2 newest (_DOC2, _DOC3), sorted asc.
    assert scope.document_uuids == (_DOC2, _DOC3)


def test_scope_person_truncation_is_deterministic(
    test_db: psycopg.Connection,
) -> None:
    """Repeated truncated scopes are byte-identical (no run-to-run drift)."""
    backend = AgeBackend()
    for eid in (_A, _B, _C, _D):
        _insert_entity(test_db, eid, "default")
    for doc in (_DOC1, _DOC2, _DOC3):
        _insert_document(test_db, doc)
    for eid in (_A, _B, _C, _D):
        _insert_mention(test_db, "default", eid, _DOC1)
    _insert_mention(test_db, "default", _A, _DOC2)
    _insert_mention(test_db, "default", _B, _DOC2)

    first = backend.scope_person(test_db, "default", _A, frontier_cap=2)
    second = backend.scope_person(test_db, "default", _A, frontier_cap=2)

    assert first == second


def test_scope_person_truncation_emits_single_warning(
    test_db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """Truncation logs exactly one actionable WARNING (never silent)."""
    backend = AgeBackend()
    _seed_mentions(test_db, "default")  # B, C co-rows exceed cap=1

    with caplog.at_level("WARNING", logger="brain.graph_rag.backends._age_helpers"):
        backend.scope_person(test_db, "default", _A, frontier_cap=1)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "BRAIN_GRAPH_FRONTIER_CAP" in message
    assert _A in message


def test_scope_person_under_cap_emits_no_warning(
    test_db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """A scope within the cap returns the complete set with NO warning (no
    behavior change for under-cap scopes)."""
    backend = AgeBackend()
    _seed_mentions(test_db, "default")  # 2 co-rows, well under cap=50

    with caplog.at_level("WARNING", logger="brain.graph_rag.backends._age_helpers"):
        scope = backend.scope_person(test_db, "default", _A, frontier_cap=50)

    assert scope.entity_uuids == (_B, _C)
    assert scope.document_uuids == (_DOC1,)
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_scope_person_is_relational_not_age(test_db: psycopg.Connection) -> None:
    """scope_person is an indexed relational two-hop over graph_entity_mentions,
    NOT a Cypher self-join (the G2-k OOM fix).

    It returns the correct in-scope set WITHOUT any AGE materialization (no
    ``bootstrap``, no vertex upsert), and a document where the seed is mentioned
    ALONE (no co-entity) is excluded — proving the scope is purely relational.
    """
    backend = AgeBackend()
    # Deliberately NO backend.bootstrap / upsert — only relational rows exist.
    _seed_mentions(test_db, "default")  # A, B, C co-mentioned in _DOC1
    # _DOC2: the seed _A is mentioned ALONE (no co-entity) → must be excluded.
    _insert_document(test_db, _DOC2)
    _insert_mention(test_db, "default", _A, _DOC2)

    scope = backend.scope_person(test_db, "default", _A, frontier_cap=50)

    assert scope.entity_uuids == (_B, _C)
    assert scope.document_uuids == (_DOC1,)  # _DOC2 (seed-only) excluded
    assert scope.seed_entity_uuid == _A
    assert scope.tenant_id == "default"


# --------------------------------------------------------------------------- #
# drop_graph (tenant-aware)
# --------------------------------------------------------------------------- #
def test_drop_graph_is_tenant_aware(test_db: psycopg.Connection) -> None:
    backend = AgeBackend()
    backend.bootstrap(test_db)
    _seed_cooccur_chain(test_db, backend, "default")
    _insert_entity(test_db, _A, "acme")
    _insert_entity(test_db, _B, "acme")
    _insert_relationship(test_db, "acme", _A, _B, 0.7)
    backend.upsert_entities(
        test_db, "acme", [_entity(_A, "acme"), _entity(_B, "acme")]
    )
    backend.refresh_cooccur_edges(test_db, "acme")

    deleted = backend.drop_graph(test_db, "default")

    assert deleted == 4  # A, B, C, D in default
    assert _entity_count(test_db, "default") == 0
    assert _cooccur_count(test_db, "default") == 0
    # acme untouched.
    assert _entity_count(test_db, "acme") == 2
    assert _cooccur_count(test_db, "acme") == 1


# --------------------------------------------------------------------------- #
# transactional DML + error wrapping
# --------------------------------------------------------------------------- #
def test_dml_inside_transaction_persists_after_commit(
    test_db: psycopg.Connection,
) -> None:
    """Reconcile (G1) wraps backend writes in a transaction — prove it works."""
    backend = AgeBackend()
    backend.bootstrap(test_db)  # autocommit conn creates the graph

    with connect_age(TEST_DATABASE_URL) as txn:
        assert txn.autocommit is False
        backend.upsert_entities(txn, "default", [_entity(_A, "default", "Alpha")])
        txn.commit()

    # Verify on the independent autocommit connection.
    assert _entity_count(test_db, "default") == 1
    assert _entity_name(test_db, "default", _A) == "Alpha"


def test_cypher_wraps_psycopg_error(test_db: psycopg.Connection) -> None:
    """A psycopg.Error from a cypher() call surfaces as GraphBackendError."""
    backend = AgeBackend()
    backend.bootstrap(test_db)
    real_execute = test_db.execute
    boom = psycopg.OperationalError("simulated cypher failure")

    def _fail_on_cypher(query: object, *args: object, **kwargs: object) -> object:
        if "ag_catalog.cypher(" in str(query):
            raise boom
        return real_execute(query, *args, **kwargs)

    with (
        patch.object(test_db, "execute", side_effect=_fail_on_cypher),
        pytest.raises(GraphBackendError, match="Cypher execution failed") as excinfo,
    ):
        backend.upsert_entities(test_db, "default", [_entity(_A, "default")])
    assert excinfo.value.__cause__ is boom
