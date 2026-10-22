"""Tests for ``brain.graph_rag.write_lock`` and the writers that must take it.

The 2026-10-22 deadlock fix serializes every graph write transaction in a
tenant on one advisory lock (``docs/plans/2026-10-22-graph-entities-deadlock.md``).
The fix is only as good as its coverage: one writer that skips the lock can
still deadlock against the rest. So the plan's closed list of writers W1-W10
gets one test each, and each test proves that writer takes the lock.

Each writer test holds the tenant's graph write lock on a second session and
runs the writer under a short ``lock_timeout``. The holder holds that advisory
lock and nothing else, and nothing else runs concurrently, so a
``LockNotAvailable`` can only mean the writer waited on the graph write lock.
A control run of the same writer, after the holder lets go, then succeeds. That
shows the failure was the lock and not the setup.

All entities are synthetic; no PII.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.errors import GraphReconcileError
from brain.graph_rag.aggregates import refresh_aggregates
from brain.graph_rag.aliases import AliasRule, apply_aliases, merge_aliases
from brain.graph_rag.build import build_graph
from brain.graph_rag.communities import build_communities
from brain.graph_rag.communities_summary import (
    _run_embedding_phase,
    _run_summary_phase,
)
from brain.graph_rag.cross_type import _restore_winner_display_names
from brain.graph_rag.reconcile import (
    ReconcileConfig,
    ResolvedPerson,
    reconcile_document,
    remove_document,
)
from brain.graph_rag.write_lock import (
    GRAPH_WRITE_LOCK_CLASS,
    acquire_graph_write_lock,
)
from tests.graph_fakes import NullBackend, seed_note_doc, static_resolver

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

TENANT = "default"


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _Summarizer:
    model = "fake-model:1b"

    def summarize_group(
        self, *, person: str | None, entity_names: list[str], doc_titles: list[str]
    ) -> str | None:
        return "Synthetic community summary."


class _Embedder:
    """Fake embedder whose ``dim`` matches the live column (no schema change)."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        return [[0.1] * self.dim for _ in texts]

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cfg(tenant: str = TENANT) -> ReconcileConfig:
    return ReconcileConfig(tenant_id=tenant, generic_df_ratio=1.0)


def _seed_entity(
    conn: psycopg.Connection[Any], entity_type: str, key: str, tenant: str = TENANT
) -> str:
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        (tenant, entity_type, key.title(), key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_two_communities(conn: psycopg.Connection[Any]) -> None:
    """Two dense person triangles: enough for ``build_communities`` to persist."""
    for prefix in ("p", "q"):
        ids = [_seed_entity(conn, "person", f"{prefix}-{i}") for i in range(3)]
        for a, b in [(0, 1), (0, 2), (1, 2)]:
            src, dst = sorted((ids[a], ids[b]))
            conn.execute(
                "INSERT INTO graph_relationships "
                "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
                "VALUES (%s, %s, %s, 'co_occurs', 0.8, 1, 1)",
                (TENANT, src, dst),
            )


def _community_cfg() -> Config:
    return Config(database_url=TEST_DATABASE_URL, graph_tenant_id=TENANT)


@contextmanager
def _lock_held_elsewhere(tenant: str = TENANT) -> Iterator[None]:
    """Hold ``tenant``'s graph write lock on another session for the block."""
    holder = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    try:
        with holder.transaction():
            acquire_graph_write_lock(holder, tenant)
            yield
    finally:
        holder.close()


def _assert_takes_graph_lock(
    conn: psycopg.Connection[Any], run: Callable[[], object]
) -> None:
    """``run`` waits on the graph write lock; once it is free, ``run`` succeeds."""
    conn.execute("SET lock_timeout = '200ms'")
    with _lock_held_elsewhere(), pytest.raises(psycopg.errors.LockNotAvailable):
        run()
    run()


# --------------------------------------------------------------------------- #
# The lock itself
# --------------------------------------------------------------------------- #
def test_refuses_to_run_outside_a_transaction(test_db: psycopg.Connection[Any]) -> None:
    """On autocommit outside a transaction the lock would protect nothing."""
    with pytest.raises(GraphReconcileError, match="inside a transaction"):
        acquire_graph_write_lock(test_db, TENANT)


def test_lock_is_keyed_by_tenant(test_db: psycopg.Connection[Any]) -> None:
    """The held lock is ``(GRAPH_WRITE_LOCK_CLASS, hashtext(tenant))``, xact-scoped."""
    with test_db.transaction():
        acquire_graph_write_lock(test_db, "tenant-a")
        rows = test_db.execute(
            "SELECT classid::bigint, objid::bigint, objsubid "
            "FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        ).fetchall()
        expected = test_db.execute(
            "SELECT hashtext('tenant-a')::oid::bigint"
        ).fetchone()
    assert expected is not None
    assert rows == [(GRAPH_WRITE_LOCK_CLASS, expected[0], 2)]
    released = test_db.execute(
        "SELECT count(*) FROM pg_locks "
        "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
    ).fetchone()
    assert released == (0,), "the lock outlived its transaction"


def test_other_tenants_are_not_blocked(test_db: psycopg.Connection[Any]) -> None:
    doc = seed_note_doc(test_db, "doc")
    test_db.execute("SET lock_timeout = '200ms'")
    with _lock_held_elsewhere("some-other-tenant"):
        result = reconcile_document(
            test_db,
            doc,
            backend=NullBackend(),
            config=_cfg(),
            person_resolver=static_resolver([ResolvedPerson("alpha", "Alpha")]),
        )
    assert result.skipped is False


# --------------------------------------------------------------------------- #
# W1-W10: every graph write transaction takes the lock
# --------------------------------------------------------------------------- #
def test_w1_reconcile_document_takes_the_lock(test_db: psycopg.Connection[Any]) -> None:
    doc = seed_note_doc(test_db, "doc")
    _assert_takes_graph_lock(
        test_db,
        lambda: reconcile_document(
            test_db,
            doc,
            backend=NullBackend(),
            config=_cfg(),
            person_resolver=static_resolver([ResolvedPerson("alpha", "Alpha")]),
        ),
    )


def test_w2_remove_document_takes_the_lock(test_db: psycopg.Connection[Any]) -> None:
    doc = seed_note_doc(test_db, "doc")
    _assert_takes_graph_lock(
        test_db,
        lambda: remove_document(test_db, doc, backend=NullBackend(), config=_cfg()),
    )


def test_w3_refresh_aggregates_takes_the_lock(test_db: psycopg.Connection[Any]) -> None:
    _assert_takes_graph_lock(
        test_db,
        lambda: refresh_aggregates(test_db, backend=NullBackend(), config=_cfg()),
    )


def test_w4_build_graph_force_prepass_takes_the_lock(
    test_db: psycopg.Connection[Any],
) -> None:
    _assert_takes_graph_lock(
        test_db,
        lambda: build_graph(
            test_db, iter([]), backend=NullBackend(), config=_cfg(), force=True
        ),
    )


def test_w5_apply_aliases_takes_the_lock(test_db: psycopg.Connection[Any]) -> None:
    _seed_entity(test_db, "topic", "source-key")
    rules = [AliasRule("topic", "source-key", "topic", "target-key")]
    _assert_takes_graph_lock(test_db, lambda: apply_aliases(test_db, TENANT, rules))


def test_w6_merge_aliases_takes_the_lock(test_db: psycopg.Connection[Any]) -> None:
    """The source is absent, so only the outer transaction's lock can block."""
    rules = [AliasRule("topic", "absent-key", "topic", "target-key")]
    _assert_takes_graph_lock(
        test_db,
        lambda: merge_aliases(test_db, TENANT, rules, NullBackend(), config=_cfg()),
    )


def test_w7_restore_winner_display_names_takes_the_lock(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_entity(test_db, "topic", "winner-key")
    _assert_takes_graph_lock(
        test_db,
        lambda: _restore_winner_display_names(
            test_db, TENANT, NullBackend(), {("topic", "winner-key"): "WinnerKey"}
        ),
    )


def test_w8_build_communities_takes_the_lock(test_db: psycopg.Connection[Any]) -> None:
    _seed_two_communities(test_db)
    _assert_takes_graph_lock(
        test_db,
        lambda: build_communities(test_db, _community_cfg(), tenant=TENANT, force=True),
    )


def test_w9_summary_phase_write_takes_the_lock(test_db: psycopg.Connection[Any]) -> None:
    _seed_two_communities(test_db)
    build_communities(test_db, _community_cfg(), tenant=TENANT, force=True)
    _assert_takes_graph_lock(
        test_db,
        lambda: _run_summary_phase(
            test_db, tenant=TENANT, enricher=_Summarizer(), limit=None
        ),
    )


def test_w10_embedding_phase_write_takes_the_lock(
    test_db: psycopg.Connection[Any],
) -> None:
    """The embedding phase is best-effort: a lock timeout is a counted failure."""
    _seed_two_communities(test_db)
    build_communities(test_db, _community_cfg(), tenant=TENANT, force=True)
    test_db.execute(
        "UPDATE graph_communities SET summary = 'Synthetic.' WHERE tenant_id = %s",
        (TENANT,),
    )
    dim_row = test_db.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = 'graph_communities'::regclass AND attname = 'summary_embedding'"
    ).fetchone()
    assert dim_row is not None
    embedder = _Embedder(int(dim_row[0]))
    test_db.execute("SET lock_timeout = '200ms'")

    with _lock_held_elsewhere():
        blocked = _run_embedding_phase(test_db, tenant=TENANT, embedder=embedder)
    control = _run_embedding_phase(test_db, tenant=TENANT, embedder=embedder)

    assert blocked == (0, 2), "the embedding write did not wait on the graph lock"
    assert control == (2, 0)
