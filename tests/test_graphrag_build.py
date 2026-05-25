"""Cross-cutting tests for the GraphRAG corpus build/refresh (wave G1-d).

Covers, against the live AGE test instance (port 5434):

* ``brain.queries.iter_all_document_ids`` / ``count_documents`` — the build
  driver's document iterator.
* ``brain.graph_rag.build.build_graph`` — the batch backfill driver:
  (a) batched build is byte-for-byte equivalent to per-doc incremental reconcile
  of the same docs; (b) multi-tenant build isolation; (c) idempotency (re-run is
  all-skip / stable); (d) resume after a simulated interruption completes the
  rest.
* ``brain.graph_rag.reconcile.refresh_aggregates`` — corpus-wide weight/edge
  recompute (propagates a suppression-ratio change; idempotent; raises before
  build when AGE vertices are missing).
* The ``brain graphrag build`` / ``brain graphrag refresh`` CLI surfaces
  (output, ``--backfill`` / ``--tenant`` / ``--limit``, AGE-absent gating).

All people are synthetic (alice / bob / carol / dave / erin); no PII. The schema
+ AGE graph reset per test via the ``test_db`` fixture.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from brain.cli import app
from brain.db import DEFAULT_GRAPH_NAME
from brain.errors import GraphBackendError, GraphReconcileError
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.build import BuildResult, build_graph
from brain.graph_rag.extract import ExtractedEntity
from brain.graph_rag.reconcile import (
    ReconcileConfig,
    ReconcileResult,
    reconcile_document,
    refresh_aggregates,
)
from brain.graph_rag.weighting import normalized_lift
from brain.queries import count_documents, iter_all_document_ids
from brain.vault.derived_links.directory import DirectoryStore
from tests.graphrag.benchmark_fixture import BenchmarkSpec, generate_benchmark_graph

TEST_DATABASE_URL = "postgresql://brain:brain@localhost:5434/second_brain_test"

# Suppression-disabled ratio (cap = round(N * 1.0) = N) so the tiny corpora
# materialize edges. Mirrors ``_NO_SUPPRESS`` in test_graphrag_reconcile.
_NO_SUPPRESS = 1.0


def _cfg(
    tenant_id: str = "default", generic_df_ratio: float = _NO_SUPPRESS
) -> ReconcileConfig:
    return ReconcileConfig(tenant_id=tenant_id, generic_df_ratio=generic_df_ratio)


# --------------------------------------------------------------------------- #
# Seeding helpers (mirror test_graphrag_reconcile)
# --------------------------------------------------------------------------- #
def _seed_directory(
    conn: psycopg.Connection[Any], pairs: Sequence[tuple[str, str]]
) -> None:
    store = DirectoryStore(conn)
    for name, email in pairs:
        store.upsert_pair(display_name=name, email=email, source="gmail")


def _seed_gmail_doc(
    conn: psycopg.Connection[Any],
    *,
    external_id: str,
    participants: Sequence[tuple[str, str]],
) -> str:
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('gmail', %s, '{}'::jsonb) RETURNING id",
        (external_id,),
    ).fetchone()
    assert src_row is not None
    from_hdr = f"{participants[0][0]} <{participants[0][1]}>"
    to_hdr = ", ".join(f"{n} <{e}>" for n, e in participants[1:])
    metadata = {"from": from_hdr, "to": to_hdr, "thread_id": external_id}
    salted = f"body\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type, metadata)
        VALUES (%s, %s, %s, %s, 'email', %s::jsonb)
        RETURNING id::text
        """,
        (src_row[0], external_id, salted, content_hash, json.dumps(metadata)),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _seed_three_docs(conn: psycopg.Connection[Any]) -> list[str]:
    """alice-bob, alice-carol, bob-carol → 3 persons, complete triangle."""
    _seed_directory(
        conn,
        [("alice", "alice@x.com"), ("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    return [
        _seed_gmail_doc(
            conn,
            external_id="m1",
            participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
        ),
        _seed_gmail_doc(
            conn,
            external_id="m2",
            participants=[("alice", "alice@x.com"), ("carol", "carol@x.com")],
        ),
        _seed_gmail_doc(
            conn,
            external_id="m3",
            participants=[("bob", "bob@x.com"), ("carol", "carol@x.com")],
        ),
    ]


def _backend(conn: psycopg.Connection[Any]) -> AgeBackend:
    backend = AgeBackend()
    backend.bootstrap(conn)
    return backend


# --------------------------------------------------------------------------- #
# Relational + AGE assertions
# --------------------------------------------------------------------------- #
def _person_keys(conn: psycopg.Connection[Any], tenant: str) -> set[str]:
    rows = conn.execute(
        "SELECT canonical_key FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = 'person'",
        (tenant,),
    ).fetchall()
    return {str(r[0]) for r in rows}


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


def _entity_id(conn: psycopg.Connection[Any], tenant: str, canonical_key: str) -> str:
    row = conn.execute(
        "SELECT id::text FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = 'person' AND canonical_key = %s",
        (tenant, canonical_key),
    ).fetchone()
    assert row is not None, f"no entity for {canonical_key!r}"
    return str(row[0])


def _watermark_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_index_state "
        "WHERE tenant_id = %s AND aspect = 'people'",
        (tenant,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _relational_entity_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_entities WHERE tenant_id = %s", (tenant,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _doc_count_by_key(
    conn: psycopg.Connection[Any], tenant: str
) -> dict[str, int]:
    """Map each entity's ``canonical_key`` → its derived ``graph_entities.doc_count``.

    ``doc_count`` is refreshed by ``_recompute_aggregates`` — which the deferred
    bulk build hoists to a SINGLE post-loop ``refresh_aggregates`` instead of
    per-document — so comparing this map across the batched and per-doc tenants
    proves the deferral does not drift the derived per-entity doc frequency.
    """
    rows = conn.execute(
        "SELECT canonical_key, doc_count FROM graph_entities WHERE tenant_id = %s",
        (tenant,),
    ).fetchall()
    return {str(k): int(c) for k, c in rows}


def _relational_relationship_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_relationships WHERE tenant_id = %s", (tenant,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _seed_orphan_entity_and_relationship(
    conn: psycopg.Connection[Any], tenant: str
) -> tuple[str, str]:
    """Insert two orphan (zero-mention) entities + a stale relationship between them.

    Models stale RELATIONAL state left behind by deleted documents: catalog rows
    with no remaining ``graph_entity_mentions`` and a ``graph_relationships`` row
    not backed by any current ``graph_edge_contributions``.
    """
    ids: list[str] = []
    for name, key in (("Ghost One", "ghost one"), ("Ghost Two", "ghost two")):
        row = conn.execute(
            "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'person', %s, %s) RETURNING id::text",
            (tenant, name, key),
        ).fetchone()
        assert row is not None
        ids.append(str(row[0]))
    src, dst = sorted(ids)  # graph_relationships CHECK requires src_id < dst_id
    conn.execute(
        "INSERT INTO graph_relationships "
        "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
        "VALUES (%s, %s, %s, 'co_occurs', 0.5, 1, 1)",
        (tenant, src, dst),
    )
    return src, dst


def _rels_by_key(
    conn: psycopg.Connection[Any], tenant: str
) -> dict[frozenset[str], float]:
    """Map each relationship to its canonical-key pair → weight (tenant-agnostic)."""
    rows = conn.execute(
        "SELECT s.canonical_key, d.canonical_key, r.weight "
        "FROM graph_relationships r "
        "JOIN graph_entities s ON s.tenant_id = r.tenant_id AND s.id = r.src_id "
        "JOIN graph_entities d ON d.tenant_id = r.tenant_id AND d.id = r.dst_id "
        "WHERE r.tenant_id = %s",
        (tenant,),
    ).fetchall()
    return {frozenset((str(a), str(b))): round(float(w), 6) for a, b, w in rows}


def _cypher_scalar(
    conn: psycopg.Connection[Any], query: str, params: Mapping[str, Any]
) -> list[tuple[Any, ...]]:
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


def _age_cooccur_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn,
        "MATCH ()-[r:CO_OCCURS {tenant_id: $t}]->() RETURN count(r)",
        {"t": tenant},
    )
    return int(str(rows[0][0]))


def _age_document_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn, "MATCH (d:Document {tenant_id: $t}) RETURN count(d)", {"t": tenant}
    )
    return int(str(rows[0][0]))


def _age_mentioned_in_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn,
        "MATCH ()-[r:MENTIONED_IN {tenant_id: $t}]->() RETURN count(r)",
        {"t": tenant},
    )
    return int(str(rows[0][0]))


# --------------------------------------------------------------------------- #
# 1. Document iterator helpers
# --------------------------------------------------------------------------- #
def test_iter_all_document_ids_yields_every_id_in_order(
    test_db: psycopg.Connection[Any],
) -> None:
    ids = _seed_three_docs(test_db)
    batches = list(iter_all_document_ids(test_db, batch_size=2))
    flat = [doc_id for batch in batches for doc_id in batch]
    assert set(flat) == set(ids)
    assert flat == sorted(flat)  # ascending id order
    # batch_size honoured: first batch full, second the remainder.
    assert [len(b) for b in batches] == [2, 1]


def test_iter_all_document_ids_empty(test_db: psycopg.Connection[Any]) -> None:
    assert list(iter_all_document_ids(test_db)) == []


def test_count_documents(test_db: psycopg.Connection[Any]) -> None:
    assert count_documents(test_db) == 0
    _seed_three_docs(test_db)
    assert count_documents(test_db) == 3


# --------------------------------------------------------------------------- #
# 2. Batched build == incremental reconcile (a)
# --------------------------------------------------------------------------- #
def test_batched_build_equals_incremental(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    # Batched backfill into tenant "batch".
    bres = build_graph(test_db, all_ids, backend=backend, config=_cfg("batch"))
    assert bres == BuildResult(
        processed=3, reconciled=3, skipped=0, orphans_removed=0, relationship_count=3
    )

    # Incremental per-doc reconcile of the SAME docs into tenant "incr".
    for doc_id in all_ids:
        reconcile_document(test_db, doc_id, backend=backend, config=_cfg("incr"))

    # Relational source-of-truth matches.
    assert _person_keys(test_db, "batch") == _person_keys(test_db, "incr")
    assert _mention_count(test_db, "batch") == _mention_count(test_db, "incr") == 6
    assert (
        _contribution_count(test_db, "batch")
        == _contribution_count(test_db, "incr")
        == 3
    )
    # Weights (by canonical-key pair) match exactly.
    assert _rels_by_key(test_db, "batch") == _rels_by_key(test_db, "incr")
    # Derived per-entity doc_count matches too — the deferred build recomputes it
    # ONCE post-loop (not per doc), so this guards against doc_count drift under
    # deferral. Triangle: each of alice/bob/carol appears in 2 docs → all 2.
    assert _doc_count_by_key(test_db, "batch") == _doc_count_by_key(test_db, "incr")
    assert set(_doc_count_by_key(test_db, "batch").values()) == {2}
    # Spot-check the weights are the real normalized lift (triangle: each pair
    # co-occurs in 1 doc; each person appears in 2 docs → lift 0.5).
    assert set(_rels_by_key(test_db, "batch").values()) == {
        round(normalized_lift(1, 2, 2), 6)
    }
    # AGE mirror matches.
    assert _age_entity_count(test_db, "batch") == _age_entity_count(test_db, "incr") == 3
    assert (
        _age_cooccur_count(test_db, "batch")
        == _age_cooccur_count(test_db, "incr")
        == 3
    )
    assert (
        _age_document_count(test_db, "batch")
        == _age_document_count(test_db, "incr")
        == 3
    )
    # MENTIONED_IN edge parity: the triangle is 3 docs × 2 persons each = 6
    # person->document mention edges per tenant. A batched backfill must produce
    # the identical MENTIONED_IN topology as the per-doc incremental path, not
    # just matching entity/cooccur/document vertex counts.
    assert (
        _age_mentioned_in_count(test_db, "batch")
        == _age_mentioned_in_count(test_db, "incr")
        == 6
    )


# --------------------------------------------------------------------------- #
# 3. Multi-tenant build isolation (b)
# --------------------------------------------------------------------------- #
def test_multitenant_build_isolation(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    build_graph(test_db, all_ids, backend=backend, config=_cfg("tenant-a"))

    # tenant-a fully built; tenant-b untouched.
    assert _person_keys(test_db, "tenant-a") == {"alice", "bob", "carol"}
    assert _person_keys(test_db, "tenant-b") == set()
    assert _age_entity_count(test_db, "tenant-a") == 3
    assert _age_entity_count(test_db, "tenant-b") == 0

    # Building tenant-b does not perturb tenant-a.
    build_graph(test_db, all_ids, backend=backend, config=_cfg("tenant-b"))
    assert _person_keys(test_db, "tenant-b") == {"alice", "bob", "carol"}
    assert _age_entity_count(test_db, "tenant-a") == 3
    assert _age_cooccur_count(test_db, "tenant-a") == 3


# --------------------------------------------------------------------------- #
# 4. Idempotency (c)
# --------------------------------------------------------------------------- #
def test_build_is_idempotent(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    first = build_graph(test_db, all_ids, backend=backend, config=_cfg())
    assert first.reconciled == 3 and first.skipped == 0
    rels_before = _rels_by_key(test_db, "default")
    cooccur_before = _age_cooccur_count(test_db, "default")

    second = build_graph(test_db, all_ids, backend=backend, config=_cfg())
    # Every doc short-circuits on the unchanged watermark.
    assert second == BuildResult(processed=3, reconciled=0, skipped=3, orphans_removed=0)
    # Graph unchanged.
    assert _rels_by_key(test_db, "default") == rels_before
    assert _age_cooccur_count(test_db, "default") == cooccur_before
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}


def test_build_progress_callback_invoked(test_db: psycopg.Connection[Any]) -> None:
    """``build_graph(progress=...)`` is called once per processed document."""
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    seen: list[tuple[int, str]] = []

    def _record(processed: int, document_id: str, result: ReconcileResult) -> None:
        seen.append((processed, document_id))
        assert isinstance(result, ReconcileResult)

    build_graph(test_db, all_ids, backend=backend, config=_cfg(), progress=_record)
    assert [p for p, _ in seen] == [1, 2, 3]
    assert {d for _, d in seen} == set(all_ids)


# --------------------------------------------------------------------------- #
# 4b. Deferred whole-tenant refresh (perf): the O(R) AGE CO_OCCURS rebuild is
#     hoisted out of the per-document loop and run ONCE after it, not per doc —
#     while keeping the end state identical to the per-document path.
# --------------------------------------------------------------------------- #
class _CountingRefreshBackend(AgeBackend):
    """AgeBackend that counts ``refresh_cooccur_edges`` calls (defer-perf probe).

    The whole-tenant CO_OCCURS rebuild is the O(R) cost the deferral hoists out of
    the per-document loop; counting its invocations proves a corpus build pays it
    exactly once (via the post-loop refresh), not once per document.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refresh_cooccur_calls = 0

    def refresh_cooccur_edges(self, conn: Any, tenant_id: str) -> int:
        self.refresh_cooccur_calls += 1
        return super().refresh_cooccur_edges(conn, tenant_id)


def test_build_refreshes_cooccur_once_not_per_doc(
    test_db: psycopg.Connection[Any],
) -> None:
    """A 3-doc build rebuilds AGE CO_OCCURS ONCE (post-loop), not per document."""
    backend = _CountingRefreshBackend()
    backend.bootstrap(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    result = build_graph(test_db, all_ids, backend=backend, config=_cfg())

    # The expensive whole-tenant CO_OCCURS rematerialization ran exactly once
    # (the post-loop refresh), NOT once per processed document (which would be 3).
    assert backend.refresh_cooccur_calls == 1
    assert result.reconciled == 3
    assert result.relationship_count == 3
    # End state is still the full triangle in AGE (identical to the per-doc path).
    assert _age_cooccur_count(test_db, "default") == 3
    assert _age_entity_count(test_db, "default") == 3


def test_build_all_skip_runs_no_refresh(test_db: psycopg.Connection[Any]) -> None:
    """An idempotent (all-skip) re-build does NO whole-tenant refresh at all."""
    backend = _CountingRefreshBackend()
    backend.bootstrap(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    first = build_graph(test_db, all_ids, backend=backend, config=_cfg())
    assert first.reconciled == 3 and backend.refresh_cooccur_calls == 1

    # Re-run: every doc short-circuits on its watermark, so no work is done and
    # the post-loop refresh is gated OFF (the derived layers are already current).
    second = build_graph(test_db, all_ids, backend=backend, config=_cfg())
    assert second.reconciled == 0 and second.skipped == 3
    assert backend.refresh_cooccur_calls == 1  # unchanged — no second refresh
    assert second.relationship_count == 0
    # Graph is unchanged + intact.
    assert _age_cooccur_count(test_db, "default") == 3
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}


def test_build_final_refresh_prunes_orphan_absent_from_all_docs(
    test_db: psycopg.Connection[Any],
) -> None:
    """An entity mentioned by no document is GC'd by the single post-loop refresh."""
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    # Seed a stale orphan person (no mentions) in BOTH stores before the build.
    row = test_db.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES ('default', 'person', 'Ghost', 'ghost') RETURNING id::text"
    ).fetchone()
    assert row is not None
    orphan_id = str(row[0])
    _cypher_scalar(
        test_db,
        "MERGE (e:Entity {entity_uuid: $u, tenant_id: $t}) RETURN 1",
        {"u": orphan_id, "t": "default"},
    )
    # Only the orphan exists pre-build — the 3 real persons are created DURING
    # the build (reconcile derives them from the seeded documents' participants).
    assert _relational_entity_count(test_db, "default") == 1  # just the orphan
    assert _age_entity_count(test_db, "default") == 1  # just the orphan vertex

    result = build_graph(test_db, all_ids, backend=backend, config=_cfg())

    # The post-loop refresh GC'd the orphan from BOTH stores; only the triangle
    # survives. (Per-document reconcile deferred the GC, so this is the single
    # final pass doing it.)
    assert result.orphans_removed == 1
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}
    assert _relational_entity_count(test_db, "default") == 3
    assert _age_entity_count(test_db, "default") == 3
    assert _age_cooccur_count(test_db, "default") == 3


def test_incremental_reconcile_materializes_cooccur_per_call(
    test_db: psycopg.Connection[Any],
) -> None:
    """A single ``reconcile_document`` (defer_tenant_refresh defaults False) stays
    fully consistent immediately — CO_OCCURS + relationships materialize in that
    one call, as the incremental ingest hook (``sync.py``) requires (it never
    defers)."""
    backend = _backend(test_db)
    doc_ids = _seed_three_docs(test_db)

    # alice-bob, then alice-carol — each reconcile leaves a consistent derived
    # layer with NO post-loop refresh (this is not a build).
    res1 = reconcile_document(test_db, doc_ids[0], backend=backend, config=_cfg())
    assert not res1.skipped and res1.relationship_count == 1
    assert _age_cooccur_count(test_db, "default") == 1  # consistent after ONE call

    res2 = reconcile_document(test_db, doc_ids[1], backend=backend, config=_cfg())
    assert not res2.skipped and res2.relationship_count == 2
    assert _relational_relationship_count(test_db, "default") == 2
    assert _age_cooccur_count(test_db, "default") == 2


# --------------------------------------------------------------------------- #
# 4c. Directory index hoist (perf Fix B): the corpus-wide People-Hub directory
#     index is built ONCE for the whole batch, not once per document — while
#     person resolution stays identical to the per-document path.
# --------------------------------------------------------------------------- #
def test_build_builds_directory_index_once_not_per_doc(
    test_db: psycopg.Connection[Any], mocker: MockerFixture
) -> None:
    """A multi-doc batch build builds the People-Hub directory index ONCE (in
    build_graph), not once per document, while person resolution is unchanged."""
    import brain.wiki.build_people as build_people_mod

    spy = mocker.spy(build_people_mod, "_build_directory_index")

    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    result = build_graph(test_db, all_ids, backend=backend, config=_cfg())

    # Built exactly once for the whole 3-doc batch (pre-Fix-B this was 3 — one
    # rebuild per per-document reconcile via default_person_resolver).
    assert spy.call_count == 1
    # Resolution is unchanged: the full triangle still materializes.
    assert result.reconciled == 3
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}


def test_incremental_reconcile_builds_its_own_directory_index(
    test_db: psycopg.Connection[Any], mocker: MockerFixture
) -> None:
    """Fix B regression: the incremental path (default resolver, no prebuilt
    directory) STILL builds the directory itself — the hoist is batch-only and
    must not break single-document reconcile (sync.py's ingest hook)."""
    import brain.wiki.build_people as build_people_mod

    spy = mocker.spy(build_people_mod, "_build_directory_index")

    backend = _backend(test_db)
    doc_ids = _seed_three_docs(test_db)
    reconcile_document(test_db, doc_ids[0], backend=backend, config=_cfg())

    # The single incremental reconcile built its own one-document index.
    assert spy.call_count == 1
    assert _person_keys(test_db, "default") == {"alice", "bob"}


# --------------------------------------------------------------------------- #
# 5. Resume after interruption (d)
# --------------------------------------------------------------------------- #
def test_build_resumes_after_interruption(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    # Simulate an interruption: only the first 2 docs get indexed.
    partial = build_graph(test_db, all_ids, backend=backend, config=_cfg(), limit=2)
    assert partial.processed == 2 and partial.reconciled == 2
    assert _watermark_count(test_db, "default") == 2

    # Resume (no limit): the first 2 skip on their watermark, the 3rd is new.
    resumed = build_graph(test_db, all_ids, backend=backend, config=_cfg())
    assert resumed == BuildResult(
        processed=3, reconciled=1, skipped=2, orphans_removed=0, relationship_count=3
    )
    assert _watermark_count(test_db, "default") == 3

    # Final state equals a one-shot full build in a fresh tenant.
    build_graph(test_db, all_ids, backend=backend, config=_cfg("oneshot"))
    assert _person_keys(test_db, "default") == _person_keys(test_db, "oneshot")
    assert _rels_by_key(test_db, "default") == _rels_by_key(test_db, "oneshot")


# --------------------------------------------------------------------------- #
# 5b. Force rebuild (authoritative recovery for a dropped/corrupted AGE mirror)
# --------------------------------------------------------------------------- #
def test_build_force_rebuilds_dropped_age_mirror(
    test_db: psycopg.Connection[Any],
) -> None:
    """`build --force` recovers a dropped AGE mirror; a plain build cannot."""
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    # Initial build: full triangle in AGE + relational.
    build_graph(test_db, all_ids, backend=backend, config=_cfg())
    assert _age_entity_count(test_db, "default") == 3
    assert _age_cooccur_count(test_db, "default") == 3
    assert _age_document_count(test_db, "default") == 3
    assert _age_mentioned_in_count(test_db, "default") == 6
    rels_before = _rels_by_key(test_db, "default")

    # Simulate a dropped / corrupted AGE mirror: tear down the tenant's AGE
    # vertices (+ their edges) while the relational source-of-truth AND the
    # per-aspect watermark stay intact (docs + config unchanged).
    backend.drop_graph(test_db, "default")
    assert _age_entity_count(test_db, "default") == 0
    assert _age_cooccur_count(test_db, "default") == 0
    assert _age_document_count(test_db, "default") == 0
    assert _age_mentioned_in_count(test_db, "default") == 0
    assert _watermark_count(test_db, "default") == 3  # watermark survived the drop

    # A plain build CANNOT recover: every doc short-circuits on its unchanged
    # watermark, so the AGE mirror stays empty.
    plain = build_graph(test_db, all_ids, backend=backend, config=_cfg())
    assert plain == BuildResult(processed=3, reconciled=0, skipped=3, orphans_removed=0)
    assert _age_entity_count(test_db, "default") == 0
    assert _age_cooccur_count(test_db, "default") == 0

    # `--force` bypasses the watermark and rebuilds EVERYTHING from the
    # relational source-of-truth: entities + MENTIONED_IN + Document + CO_OCCURS.
    forced = build_graph(test_db, all_ids, backend=backend, config=_cfg(), force=True)
    assert forced == BuildResult(
        processed=3, reconciled=3, skipped=0, orphans_removed=0, relationship_count=3
    )
    assert _age_entity_count(test_db, "default") == 3
    assert _age_cooccur_count(test_db, "default") == 3
    assert _age_document_count(test_db, "default") == 3
    assert _age_mentioned_in_count(test_db, "default") == 6
    # Deterministic full recompute: the rebuilt weights match the original build.
    assert _rels_by_key(test_db, "default") == rels_before


def test_cli_build_force_rebuilds_dropped_age_mirror(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`brain graphrag build --force` is the CLI recovery path for a dropped mirror."""
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    build_graph(test_db, all_ids, backend=backend, config=_cfg())

    # Drop the AGE mirror (relational + watermark intact).
    backend.drop_graph(test_db, "default")
    assert _age_entity_count(test_db, "default") == 0

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    res = CliRunner().invoke(app, ["graphrag", "build", "--force"])
    assert res.exit_code == 0, res.output
    assert "graphrag build: 3 processed" in res.output
    assert "reconciled 3" in res.output
    assert "force: ignoring watermark" in res.output
    # AGE mirror fully rebuilt.
    assert _age_entity_count(test_db, "default") == 3
    assert _age_cooccur_count(test_db, "default") == 3
    assert _age_document_count(test_db, "default") == 3
    assert _age_mentioned_in_count(test_db, "default") == 6


def test_build_force_clears_stale_age_only_state(
    test_db: psycopg.Connection[Any],
) -> None:
    """`build --force` is a TRUE clear-then-rebuild: stale AGE-only state is gone."""
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    build_graph(test_db, all_ids, backend=backend, config=_cfg())
    assert _age_entity_count(test_db, "default") == 3
    assert _age_document_count(test_db, "default") == 3

    # Inject stale AGE-only state with NO relational counterpart: a Document
    # vertex for a doc no longer in the relational source + an AGE-only Entity
    # vertex (no graph_entities row), joined by a stale MENTIONED_IN edge.
    _cypher_scalar(
        test_db,
        "MERGE (d:Document {document_uuid: $u, tenant_id: $t}) RETURN 1",
        {"u": "stale-doc-uuid", "t": "default"},
    )
    _cypher_scalar(
        test_db,
        "MERGE (e:Entity {entity_uuid: $u, tenant_id: $t}) RETURN 1",
        {"u": "stale-ent-uuid", "t": "default"},
    )
    _cypher_scalar(
        test_db,
        "MATCH (e:Entity {entity_uuid: $e, tenant_id: $t}) "
        "MATCH (d:Document {document_uuid: $d, tenant_id: $t}) "
        "CREATE (e)-[:MENTIONED_IN {tenant_id: $t}]->(d) RETURN 1",
        {"e": "stale-ent-uuid", "d": "stale-doc-uuid", "t": "default"},
    )
    assert _age_entity_count(test_db, "default") == 4  # 3 real + 1 stale
    assert _age_document_count(test_db, "default") == 4  # 3 real + 1 stale
    assert _age_mentioned_in_count(test_db, "default") == 7  # 6 real + 1 stale

    # Force = clear-then-rebuild: clears the tenant's whole AGE mirror first, then
    # rebuilds ONLY from the relational source-of-truth.
    result = build_graph(test_db, all_ids, backend=backend, config=_cfg(), force=True)
    assert result == BuildResult(
        processed=3, reconciled=3, skipped=0, orphans_removed=0, relationship_count=3
    )
    # Stale Document + stale AGE-only Entity (+ their edge) are GONE; the mirror
    # equals the relational source exactly.
    assert _age_entity_count(test_db, "default") == 3
    assert _age_document_count(test_db, "default") == 3
    assert _age_mentioned_in_count(test_db, "default") == 6
    assert _age_cooccur_count(test_db, "default") == 3
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}
    rel_row = test_db.execute(
        "SELECT count(*) FROM graph_entities WHERE tenant_id = 'default'"
    ).fetchone()
    assert rel_row is not None and int(rel_row[0]) == 3  # mirror == relational


def test_build_force_empty_corpus_clears_to_empty(
    test_db: psycopg.Connection[Any],
) -> None:
    """`build --force` on a zero-doc tenant clears stale state in BOTH stores."""
    backend = _backend(test_db)
    tenant = "empty-t"
    # Stale RELATIONAL state for a zero-doc tenant: two orphan entities (no
    # mentions) + a graph_relationships row not backed by any contribution.
    src, _dst = _seed_orphan_entity_and_relationship(test_db, tenant)
    assert _relational_entity_count(test_db, tenant) == 2
    assert _relational_relationship_count(test_db, tenant) == 1
    # ... plus lingering AGE vertices (one mirrors a relational orphan, one is a
    # stale Document for a doc no longer present).
    _cypher_scalar(
        test_db,
        "MERGE (e:Entity {entity_uuid: $u, tenant_id: $t}) RETURN 1",
        {"u": src, "t": tenant},
    )
    _cypher_scalar(
        test_db,
        "MERGE (d:Document {document_uuid: $u, tenant_id: $t}) RETURN 1",
        {"u": "ghost-doc", "t": tenant},
    )
    assert _age_entity_count(test_db, tenant) == 1
    assert _age_document_count(test_db, tenant) == 1

    # Force with an empty corpus cleans BOTH stores to empty: the relational
    # orphans + stale relationship are GC'd/recomputed away, and the AGE mirror
    # is cleared.
    result = build_graph(test_db, [], backend=backend, config=_cfg(tenant), force=True)
    assert result == BuildResult(processed=0, reconciled=0, skipped=0, orphans_removed=0)
    # Relational tables: stale orphans + relationship GONE.
    assert _relational_entity_count(test_db, tenant) == 0
    assert _relational_relationship_count(test_db, tenant) == 0
    # AGE mirror: empty.
    assert _age_entity_count(test_db, tenant) == 0
    assert _age_document_count(test_db, tenant) == 0
    assert _age_cooccur_count(test_db, tenant) == 0
    assert _age_mentioned_in_count(test_db, tenant) == 0


def test_build_force_cleans_stale_relational_state_with_docs(
    test_db: psycopg.Connection[Any],
) -> None:
    """`build --force` with live docs still purges stale relational orphans/edges."""
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    build_graph(test_db, all_ids, backend=backend, config=_cfg())
    assert _relational_entity_count(test_db, "default") == 3
    assert _relational_relationship_count(test_db, "default") == 3

    # Inject stale relational state alongside the live triangle: two orphan
    # entities + a stale relationship not backed by any contribution.
    _seed_orphan_entity_and_relationship(test_db, "default")
    assert _relational_entity_count(test_db, "default") == 5
    assert _relational_relationship_count(test_db, "default") == 4

    result = build_graph(test_db, all_ids, backend=backend, config=_cfg(), force=True)
    assert result == BuildResult(
        processed=3, reconciled=3, skipped=0, orphans_removed=0, relationship_count=3
    )
    # The triangle survives; the stale orphans + relationship are purged from
    # BOTH stores.
    assert _relational_entity_count(test_db, "default") == 3
    assert _relational_relationship_count(test_db, "default") == 3
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}
    assert _age_entity_count(test_db, "default") == 3
    assert _age_cooccur_count(test_db, "default") == 3
    assert _age_document_count(test_db, "default") == 3
    assert _age_mentioned_in_count(test_db, "default") == 6


class _FailRestoreBackend(AgeBackend):
    """AgeBackend whose ``upsert_entities`` always raises (force-atomicity probe).

    Inherits the real ``clear_tenant`` + relational interactions; only the
    restore step (``upsert_entities``) fails, simulating a crash DURING the force
    restore AFTER the AGE mirror was cleared.
    """

    def upsert_entities(
        self, conn: Any, tenant_id: str, entities: Sequence[Any]
    ) -> int:
        raise GraphBackendError("boom during restore pre-pass")


def test_build_force_pre_pass_atomic_on_restore_failure(
    test_db: psycopg.Connection[Any],
) -> None:
    """A failure during the force restore (after clear) rolls BOTH stores back."""
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    build_graph(test_db, all_ids, backend=backend, config=_cfg())

    # Snapshot the pre-force state of both stores.
    rels_by_key_before = _rels_by_key(test_db, "default")
    assert _relational_entity_count(test_db, "default") == 3
    assert _relational_relationship_count(test_db, "default") == 3
    assert _age_entity_count(test_db, "default") == 3
    assert _age_cooccur_count(test_db, "default") == 3
    assert _age_document_count(test_db, "default") == 3
    assert _age_mentioned_in_count(test_db, "default") == 6

    # Force with a backend that fails DURING the restore (after clear_tenant ran).
    failing = _FailRestoreBackend()
    with pytest.raises(GraphBackendError, match="boom during restore pre-pass"):
        build_graph(test_db, all_ids, backend=failing, config=_cfg(), force=True)

    # The whole pre-pass rolled back atomically: the AGE mirror was NOT left
    # cleared/partial and the relational state is unchanged — exactly the
    # pre-force state in BOTH stores.
    assert _relational_entity_count(test_db, "default") == 3
    assert _relational_relationship_count(test_db, "default") == 3
    assert _rels_by_key(test_db, "default") == rels_by_key_before
    assert _age_entity_count(test_db, "default") == 3
    assert _age_cooccur_count(test_db, "default") == 3
    assert _age_document_count(test_db, "default") == 3
    assert _age_mentioned_in_count(test_db, "default") == 6


def test_build_graph_force_with_limit_raises(
    test_db: psycopg.Connection[Any],
) -> None:
    """`build_graph(force=True, limit=...)` is incoherent and raises (library guard)."""
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    with pytest.raises(GraphReconcileError, match="cannot be combined with limit"):
        build_graph(test_db, all_ids, backend=backend, config=_cfg(), limit=1, force=True)


def test_cli_build_force_with_limit_rejected() -> None:
    """`brain graphrag build --force --limit N` is rejected as a BadParameter."""
    # The flag conflict is checked before any DB/config work, so no seeding/env.
    # COLUMNS keeps Typer's Rich error panel from wrapping the message mid-line;
    # we still normalize the panel border char defensively before matching.
    res = CliRunner().invoke(
        app, ["graphrag", "build", "--force", "--limit", "1"], env={"COLUMNS": "200"}
    )
    assert res.exit_code == 2  # Typer/Click BadParameter exit code
    normalized = " ".join(res.output.replace("│", " ").split())
    assert "cannot be combined with --limit" in normalized


# --------------------------------------------------------------------------- #
# 6. refresh_aggregates
# --------------------------------------------------------------------------- #
def _seed_suppression_corpus(conn: psycopg.Connection[Any]) -> list[str]:
    """5 docs: alice in 4 (generic at ratio 0.3), bob/carol in 2, dave/erin in 1."""
    people = [
        ("alice", "alice@x.com"),
        ("bob", "bob@x.com"),
        ("carol", "carol@x.com"),
        ("dave", "dave@x.com"),
        ("erin", "erin@x.com"),
    ]
    _seed_directory(conn, people)
    addr = dict(people)
    pairs = [
        ("alice", "bob"),
        ("alice", "carol"),
        ("alice", "dave"),
        ("alice", "erin"),
        ("bob", "carol"),
    ]
    return [
        _seed_gmail_doc(
            conn, external_id=f"m{i}", participants=[(a, addr[a]), (b, addr[b])]
        )
        for i, (a, b) in enumerate(pairs)
    ]


def test_refresh_propagates_suppression_ratio_change(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    _seed_suppression_corpus(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]

    # Build with suppression OFF → all 5 pairs materialize.
    build_graph(test_db, all_ids, backend=backend, config=_cfg(generic_df_ratio=1.0))
    assert len(_rels_by_key(test_db, "default")) == 5
    assert _age_cooccur_count(test_db, "default") == 5

    # Refresh with the default suppressing ratio (0.3): cap = round(0.3*5) = 2,
    # so generic alice (df=4) drops; only bob-carol survives.
    result = refresh_aggregates(
        test_db, backend=backend, config=_cfg(generic_df_ratio=0.3)
    )
    assert result.relationship_count == 1
    rels_after = _rels_by_key(test_db, "default")
    assert rels_after == {
        frozenset({"bob", "carol"}): round(normalized_lift(1, 2, 2), 6)
    }
    # AGE CO_OCCURS re-materialized to match the suppressed mirror.
    assert _age_cooccur_count(test_db, "default") == 1
    # All five persons retain mentions (suppression only drops edges).
    assert _person_keys(test_db, "default") == {
        "alice",
        "bob",
        "carol",
        "dave",
        "erin",
    }


def test_refresh_is_idempotent(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    build_graph(test_db, all_ids, backend=backend, config=_cfg())

    first = refresh_aggregates(test_db, backend=backend, config=_cfg())
    rels_after = _rels_by_key(test_db, "default")
    cooccur_after = _age_cooccur_count(test_db, "default")

    second = refresh_aggregates(test_db, backend=backend, config=_cfg())
    assert second.relationship_count == first.relationship_count == 3
    assert second.orphans_removed == 0
    assert _rels_by_key(test_db, "default") == rels_after
    assert _age_cooccur_count(test_db, "default") == cooccur_after


def test_refresh_gcs_orphaned_entities(test_db: psycopg.Connection[Any]) -> None:
    """refresh GCs a person whose last mention/contributions vanished out-of-band."""
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    build_graph(test_db, all_ids, backend=backend, config=_cfg())
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}

    # Simulate carol's last connecting doc disappearing: drop her source rows.
    carol = _entity_id(test_db, "default", "carol")
    test_db.execute(
        "DELETE FROM graph_entity_mentions WHERE tenant_id = 'default' "
        "AND entity_id = %s",
        (carol,),
    )
    test_db.execute(
        "DELETE FROM graph_edge_contributions WHERE tenant_id = 'default' "
        "AND (src_id = %s OR dst_id = %s)",
        (carol, carol),
    )

    result = refresh_aggregates(test_db, backend=backend, config=_cfg())
    assert result.orphans_removed == 1
    assert result.relationship_count == 1  # only alice-bob survives
    assert _person_keys(test_db, "default") == {"alice", "bob"}
    # carol's AGE vertex was DETACH DELETEd; only the alice-bob edge remains.
    assert _age_entity_count(test_db, "default") == 2
    assert _age_cooccur_count(test_db, "default") == 1


class _TwoTopicExtractor:
    """Deterministic concept extractor: always emits two adjacent topics.

    No Ollama, no PII. The two topics sit at adjacent word positions so they
    co-occur within the default window — giving each concept a ``graph_entities``
    row + an AGE ``Entity`` vertex + a concept-concept ``CO_OCCURS`` edge.
    """

    @property
    def version(self) -> str:
        return "fake-extractor@concepts-v1"

    def extract(self, text: str) -> list[ExtractedEntity]:
        return [
            ExtractedEntity(
                entity_type="topic",
                canonical_key="pricing",
                display_name="Pricing",
                positions=(0,),
            ),
            ExtractedEntity(
                entity_type="topic",
                canonical_key="billing",
                display_name="Billing",
                positions=(1,),
            ),
        ]


def _concept_entity_id(
    conn: psycopg.Connection[Any], tenant: str, canonical_key: str
) -> str | None:
    """Look up a non-person (concept) entity id, or ``None`` when GC'd away."""
    row = conn.execute(
        "SELECT id::text FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type <> 'person' AND canonical_key = %s",
        (tenant, canonical_key),
    ).fetchone()
    return str(row[0]) if row is not None else None


def test_refresh_gcs_orphaned_concept(test_db: psycopg.Connection[Any]) -> None:
    """REGRESSION (G2 review FIX B): refresh GCs a now-zero-mention CONCEPT row +
    its AGE vertex, not just persons.

    Before the fix ``refresh_aggregates`` called only ``_gc_orphan_persons``, so a
    concept catalog row whose last mention vanished — and its AGE ``Entity``
    vertex — survived ``brain graphrag refresh``. It now GCs BOTH aspects,
    matching ``remove_document`` / ``build --force``.
    """
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com")])
    doc = _seed_gmail_doc(
        test_db, external_id="m1", participants=[("alice", "alice@x.com")]
    )
    rcfg = ReconcileConfig(generic_df_ratio=_NO_SUPPRESS, concepts_enabled=True)
    reconcile_document(
        test_db, doc, backend=backend, config=rcfg, extractor=_TwoTopicExtractor()
    )
    billing = _concept_entity_id(test_db, "default", "billing")
    assert billing is not None
    assert _concept_entity_id(test_db, "default", "pricing") is not None
    # alice (person) + pricing + billing = 3 Entity vertices in AGE.
    assert _age_entity_count(test_db, "default") == 3

    # Simulate billing's last mention/contributions vanishing out-of-band.
    test_db.execute(
        "DELETE FROM graph_entity_mentions WHERE tenant_id = 'default' "
        "AND entity_id = %s",
        (billing,),
    )
    test_db.execute(
        "DELETE FROM graph_edge_contributions WHERE tenant_id = 'default' "
        "AND (src_id = %s OR dst_id = %s)",
        (billing, billing),
    )

    result = refresh_aggregates(test_db, backend=backend, config=_cfg())

    # The orphaned CONCEPT catalog row is GC'd...
    assert _concept_entity_id(test_db, "default", "billing") is None
    # ...the still-mentioned concept survives...
    assert _concept_entity_id(test_db, "default", "pricing") is not None
    # ...and the orphan's AGE vertex is DETACH DELETEd (alice + pricing = 2).
    assert _age_entity_count(test_db, "default") == 2
    assert result.orphans_removed == 1


def test_refresh_before_build_raises_when_vertices_missing(
    test_db: psycopg.Connection[Any],
) -> None:
    """refresh assumes vertices exist; a relational-only graph surfaces a loud error."""
    backend = _backend(test_db)  # labels exist, but no vertices for the bench tenant
    # Relational source-of-truth WITHOUT AGE materialization.
    generate_benchmark_graph(
        test_db,
        BenchmarkSpec(entities=4, cooccur_edges=2, mentions=4, tenants=1, documents=2),
        materialize_age=False,
    )
    with pytest.raises(GraphBackendError):
        refresh_aggregates(
            test_db, backend=backend, config=_cfg("bench-t0", generic_df_ratio=1.0)
        )


# --------------------------------------------------------------------------- #
# 7. CLI surfaces
# --------------------------------------------------------------------------- #
def test_cli_build_backfill(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three_docs(test_db)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    res = CliRunner().invoke(app, ["graphrag", "build", "--backfill"])
    assert res.exit_code == 0, res.output
    assert "graphrag build: 3 processed" in res.output
    assert "reconciled 3" in res.output
    assert _person_keys(test_db, "default") == {"alice", "bob", "carol"}
    assert _age_entity_count(test_db, "default") == 3
    assert _age_cooccur_count(test_db, "default") == 3


def test_cli_build_requires_backfill_flag(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three_docs(test_db)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["graphrag", "build"])
    assert res.exit_code == 0, res.output
    assert "pass --backfill" in res.output
    # Nothing was reconciled.
    assert _person_keys(test_db, "default") == set()


def test_cli_build_tenant_and_limit(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three_docs(test_db)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    res = CliRunner().invoke(
        app, ["graphrag", "build", "--backfill", "--tenant", "custom", "--limit", "1"]
    )
    assert res.exit_code == 0, res.output
    assert "graphrag build: 1 processed" in res.output
    assert "tenant 'custom'" in res.output
    # Exactly one document indexed into the custom tenant; default untouched.
    assert _watermark_count(test_db, "custom") == 1
    assert _watermark_count(test_db, "default") == 0


def test_cli_build_exits_when_age_absent(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_three_docs(test_db)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli.age_extension_available", lambda conn: False)
    res = CliRunner().invoke(app, ["graphrag", "build", "--backfill"])
    assert res.exit_code == 1
    assert "Apache AGE is not available" in res.output
    assert _person_keys(test_db, "default") == set()


def test_cli_refresh(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend(test_db)
    _seed_three_docs(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    build_graph(test_db, all_ids, backend=backend, config=_cfg())

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    res = CliRunner().invoke(app, ["graphrag", "refresh"])
    assert res.exit_code == 0, res.output
    assert "graphrag refresh: 3 relationship(s)" in res.output
    assert _age_cooccur_count(test_db, "default") == 3


def test_cli_refresh_exits_when_age_absent(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli.age_extension_available", lambda conn: False)
    res = CliRunner().invoke(app, ["graphrag", "refresh"])
    assert res.exit_code == 1
    assert "Apache AGE is not available" in res.output
