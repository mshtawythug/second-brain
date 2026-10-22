"""Per-tenant graph write lock: every graph write transaction takes it first.

**Why it exists (2026-10-22).** ``brain graphrag build --backfill --concepts``
died with ``DeadlockDetected`` in ``upsert_concept_entities`` while the vault
watcher reconciled other documents. The cause is the tenant-wide recompute that
every non-deferred :func:`~brain.graph_rag.reconcile.reconcile_document` runs
(:func:`~brain.graph_rag.aggregates._recompute_aggregates`):
``UPDATE graph_entities SET doc_count ... WHERE tenant_id = %s`` row-locks
EVERY entity in the tenant, in scan order, while the transaction still holds the
entity rows it upserted. Any concurrent graph writer holding one of those rows
and then needing one of the recompute's closes a cycle. Sorting the upsert keys
cannot fix that: a scan-order sweep never agrees with every writer's key order,
and the reported interleaving involved a single key. Full analysis, the closed
list of writers and the mutation proofs:
``docs/plans/2026-10-22-graph-entities-deadlock.md``.

**The rule.** A transaction that writes ``graph_entities``,
``graph_entity_mentions``, ``graph_edge_contributions``,
``graph_relationships``, ``graph_index_state``, ``graph_communities``,
``graph_community_members`` or the tenant's AGE graph calls
:func:`acquire_graph_write_lock` BEFORE its first such write. Graph writes in a
tenant then serialize on one lock that is taken while the transaction holds no
graph row, so waiting can never become a deadlock. It changes no outcome: the
recompute already made concurrent writers wait on each other's rows.

**Properties.**

* Transaction-scoped (``pg_advisory_xact_lock``): released at COMMIT or
  ROLLBACK, so no code path can leak it.
* Re-entrant within a transaction, so nested writers are fine
  (``merge_aliases`` -> ``refresh_aggregates``).
* Per tenant: the key is ``(GRAPH_WRITE_LOCK_CLASS, hashtext(tenant_id))``.
  The two-``int4`` form never collides with the single-``bigint`` advisory keys
  in :mod:`brain.db` and :mod:`brain.maintenance`; Postgres keeps the forms
  apart. A ``hashtext`` collision between two tenants only over-serializes.
* Take it AFTER any slow work (the concept extractor's model call), never
  before: the lock is held to the end of the transaction.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.pq import TransactionStatus

from ..errors import GraphReconcileError

__all__ = ["GRAPH_WRITE_LOCK_CLASS", "acquire_graph_write_lock"]

#: ``classid`` of the advisory lock: ASCII ``"brgw"`` (brain graph write) as a
#: big-endian ``int4``. Fixed forever; changing it lets an old and a new process
#: write the same tenant concurrently.
GRAPH_WRITE_LOCK_CLASS = 0x62726777


def acquire_graph_write_lock(conn: psycopg.Connection[Any], tenant_id: str) -> None:
    """Block until this transaction holds ``tenant_id``'s graph write lock.

    Must be called inside a transaction (``with conn.transaction()``): outside
    one, on an autocommit connection, the lock would be released at the end of
    its own statement and protect nothing, so that is refused with
    :class:`~brain.errors.GraphReconcileError`.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise GraphReconcileError(
            "acquire_graph_write_lock must run inside a transaction "
            "(with conn.transaction()); outside one the lock is released at once"
        )
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (GRAPH_WRITE_LOCK_CLASS, tenant_id),
    )
