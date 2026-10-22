"""Regression: an alias merge and a concurrent document DELETE must not deadlock.

Found while closing the graph-entities deadlock plan's same-document residual
(``docs/plans/2026-10-22-graph-entities-deadlock.md``, Residuals). The cycle
had the same shape as the reconcile one. ``merge_aliases`` (``brain graphrag
aliases apply``, and the cross-type collapse that runs after watcher
reconciles) re-points a source entity's mentions in two statements:

1. ``INSERT ... SELECT`` the source rows under the target. A target row that
   already exists for the document is DO-UPDATEd, with no FK check, so no lock
   is taken on the ``documents`` row.
2. ``DELETE`` the source rows.

A ``DELETE FROM documents WHERE id = X`` between the two takes X's tuple lock.
Its cascade deletes X's source row, then waits on the target row the merge
updated. The merge's step-2 DELETE then reaches X's source row, which the
cascade deleted, and waits on the cascade. The fix locks every affected
document ``FOR KEY SHARE``, in id order, right after the graph write lock and
before the merge writes anything. The DELETE then waits for the merge.

All entities are synthetic; no PII.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.graph_rag.aliases import AliasRule, merge_aliases
from brain.graph_rag.reconcile import ReconcileConfig
from tests.graph_concurrency import (
    Worker,
    connect_worker,
    is_waiting_on_lock,
    join_workers,
)
from tests.graph_fakes import NullBackend, seed_note_doc
from tests.thread_wait import wait_for

TENANT = "default"


def _entity(conn: psycopg.Connection[Any], key: str) -> str:
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, 'topic', %s, %s) RETURNING id::text",
        (TENANT, key.title(), key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _id_of(conn: psycopg.Connection[Any], key: str) -> str:
    row = conn.execute(
        "SELECT id::text FROM graph_entities WHERE tenant_id = %s AND canonical_key = %s",
        (TENANT, key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _mention(conn: psycopg.Connection[Any], entity_id: str, document_id: str) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) "
        "VALUES (%s, %s, %s, 1, 'extractor:fake@v1')",
        (TENANT, entity_id, document_id),
    )


def test_an_alias_merge_and_a_concurrent_document_delete_do_not_deadlock(
    test_db: psycopg.Connection[Any],
) -> None:
    """The merge is parked between its two statements while X is deleted.

    The mentions are committed in this order, which is also their heap order:
    (source, Y), (source, X), (target, X). The merge's step-1 INSERT inserts
    (target, Y) and DO-UPDATEs (target, X). Its step-2 DELETE reaches
    (source, Y) first, and a gate row lock parks it there. The cascade of X's
    DELETE then deletes (source, X) and waits on (target, X). When the gate
    opens, the merge's DELETE reaches (source, X), and at the code before this
    fix the two deadlock. Neither side may raise. The end state must be
    consistent: X and its mentions are gone, and Y is mentioned only by the
    target.
    """
    # Ids are random, and both statements may walk an index rather than the
    # heap. So the roles go by id: Y's document id sorts below X's, and the
    # source's entity id below the target's. Heap order and every index order
    # then agree. Otherwise the merge's DELETE could reach (source, X) before
    # the gate, and the test would pass without exercising the cycle.
    first, second = sorted([_entity(test_db, "key-one"), _entity(test_db, "key-two")])
    source_key, target_key = (
        ("key-one", "key-two") if first == _id_of(test_db, "key-one") else ("key-two", "key-one")
    )
    source, target = first, second
    doc_y, doc_x = sorted([seed_note_doc(test_db, "doc-a"), seed_note_doc(test_db, "doc-b")])
    _mention(test_db, source, doc_y)
    _mention(test_db, source, doc_x)
    _mention(test_db, target, doc_x)
    rules = [AliasRule("topic", source_key, "topic", target_key)]
    config = ReconcileConfig(tenant_id=TENANT, generic_df_ratio=1.0)
    gate, merge_conn, delete_conn, observer = (connect_worker() for _ in range(4))
    try:
        gate.autocommit = False
        gate.execute(
            "SELECT 1 FROM graph_entity_mentions "
            "WHERE entity_id = %s AND document_id = %s FOR UPDATE",
            (source, doc_y),
        )
        merge = Worker(
            lambda: merge_aliases(merge_conn, TENANT, rules, NullBackend(), config=config)
        )
        merge.start()
        # The merge's step-2 DELETE is parked on the gate's row.
        wait_for(lambda: is_waiting_on_lock(observer, merge_conn.info.backend_pid))
        delete = Worker(
            lambda: delete_conn.execute("DELETE FROM documents WHERE id = %s", (doc_x,))
        )
        delete.start()
        wait_for(lambda: is_waiting_on_lock(observer, delete_conn.info.backend_pid))
        gate.rollback()
        join_workers(merge, delete)

        assert delete.error is None, f"the document DELETE failed: {delete.error!r}"
        assert merge.error is None, f"the alias merge failed: {merge.error!r}"
        rows = test_db.execute(
            "SELECT document_id::text, entity_id::text FROM graph_entity_mentions "
            "WHERE tenant_id = %s ORDER BY 1",
            (TENANT,),
        ).fetchall()
        assert rows == [(doc_y, target)], f"inconsistent mentions after the merge: {rows}"
        gone = test_db.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (doc_x,)
        ).fetchone()
        assert gone == (0,)
    finally:
        for conn in (gate, merge_conn, delete_conn, observer):
            conn.close()
