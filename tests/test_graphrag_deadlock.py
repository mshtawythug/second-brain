"""Regression tests: concurrent graph writers must not deadlock (2026-10-22).

``brain graphrag build --backfill --concepts`` died with ``DeadlockDetected``
on an index-tuple insert into ``graph_entities``
(``build_graph`` -> ``reconcile_document`` -> ``upsert_concept_entities``)
while the vault watcher was reconciling other documents' graph entities. The
plan with the root cause is ``docs/plans/2026-10-22-graph-entities-deadlock.md``.

Each test forces one interleaving with two real connections against the test
Postgres, so the outcome is deterministic, not a race. The pause points are the
production DI seams: the ``person_resolver`` and the ``extractor`` are injected
fakes, and a third "gate" connection holds a row lock where the interleaving
needs a writer parked. The tests never sleep for a fixed time and assume it is
long enough: they poll ``pg_stat_activity`` until a backend is waiting on a
lock, through the shared backstop ``tests.thread_wait.wait_for``. Every worker
connection has a ``statement_timeout``, so a regression fails within seconds
instead of hanging the suite.

* :func:`test_backfill_concept_upsert_vs_watcher_recompute_does_not_deadlock`
  is the reported crash: a deferred backfill reconcile vs a non-deferred
  (watcher) reconcile that extracted the same new concept.
* :func:`test_two_full_reconciles_do_not_deadlock_in_the_tenant_recompute`
  is the watcher-vs-watcher (or watcher-vs-MCP) form: two non-deferred
  reconciles, each holding its own entity row, meet in the tenant-wide
  ``doc_count`` UPDATE.
* :func:`test_concept_extraction_holds_no_graph_lock` pins that the slow LLM
  extraction runs before the reconcile takes any graph lock, so a concurrent
  graph write is never stuck behind a model call.

All entities are synthetic (alpha / bravo / gamma / gate); no PII.
"""
from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from brain.graph_rag.extract import ExtractedEntity
from brain.graph_rag.reconcile import (
    ReconcileConfig,
    ResolvedPerson,
    reconcile_document,
)
from tests.graph_concurrency import (
    TEST_DATABASE_URL,
    WAIT_S,
    Worker,
    connect_worker,
    is_waiting_on_lock,
    join_workers,
)
from tests.graph_fakes import NullBackend, seed_note_doc, static_resolver
from tests.thread_wait import wait_for

TENANT = "default"

# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _HookExtractor:
    """Fake concept extractor that runs ``hook`` before returning ``entities``."""

    version = "fake-model@deadlock-v1"

    def __init__(
        self,
        entities: list[ExtractedEntity],
        hook: Callable[[], None] | None = None,
    ) -> None:
        self._entities = entities
        self._hook = hook

    def extract(self, text: str) -> list[ExtractedEntity]:
        if self._hook is not None:
            self._hook()
        return list(self._entities)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cfg(*, concepts: bool = False) -> ReconcileConfig:
    # generic_df_ratio 1.0 disables generic suppression for the tiny corpus.
    return ReconcileConfig(
        tenant_id=TENANT, generic_df_ratio=1.0, concepts_enabled=concepts
    )


def _seed_persons(conn: psycopg.Connection[Any], keys: Sequence[str]) -> None:
    """Commit person entities IN THIS ORDER (heap order), each with a mention.

    The mention (on an anchor document) keeps them from being GC'd as orphans
    by whichever reconcile commits first.
    """
    anchor = seed_note_doc(conn, "anchor")
    for key in keys:
        row = conn.execute(
            "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'person', %s, %s) RETURNING id::text",
            (TENANT, key.title(), key),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO graph_entity_mentions "
            "(tenant_id, entity_id, document_id, mention_count, source) "
            "VALUES (%s, %s, %s, 1, 'people')",
            (TENANT, row[0], anchor),
        )


def _mentioned_keys(conn: psycopg.Connection[Any], document_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT ge.canonical_key FROM graph_entity_mentions m "
        "JOIN graph_entities ge ON ge.tenant_id = m.tenant_id AND ge.id = m.entity_id "
        "WHERE m.tenant_id = %s AND m.document_id = %s",
        (TENANT, document_id),
    ).fetchall()
    return {str(row[0]) for row in rows}


_GAMMA = ExtractedEntity(
    entity_type="topic", canonical_key="gamma", display_name="Gamma", positions=(0,)
)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_backfill_concept_upsert_vs_watcher_recompute_does_not_deadlock(
    test_db: psycopg.Connection[Any],
) -> None:
    """The reported crash: backfill concept upsert vs watcher tenant recompute.

    The interleaving at ``94dd1cb``:

    1. Backfill (T1, ``defer_tenant_refresh=True``) upserts its person
       ``alpha`` (an existing row: ON CONFLICT DO UPDATE row-locks it), then
       calls the extractor -- inside the same transaction.
    2. Watcher (T2, non-deferred) inserts the new concept ``gamma``, then runs
       the tenant-wide ``UPDATE graph_entities SET doc_count`` and blocks on
       ``alpha``.
    3. T1's extraction returns ``gamma``; T1's insert of ``gamma`` waits on
       T2's uncommitted index tuple. T1 waits on T2 and T2 on T1:
       ``DeadlockDetected`` in ``upsert_concept_entities``.

    The hook parks T1 inside its extraction until T2 is either waiting on a
    lock or done, so step 2 always happens inside step 1's window. The watcher's
    longer ``deadlock_timeout`` makes the backfill the side that runs the
    deadlock check, so at ``94dd1cb`` the backfill is the victim, with the
    reported context (``while inserting index tuple``).
    """
    _seed_persons(test_db, ["alpha"])
    backfill_doc = seed_note_doc(test_db, "backfill-doc")
    watcher_doc = seed_note_doc(test_db, "watcher-doc")
    backfill_conn, watcher_conn, observer = connect_worker(), connect_worker(), connect_worker()
    try:
        watcher_conn.execute("SET deadlock_timeout = '10s'")
        watcher_pid = watcher_conn.info.backend_pid
        in_extract = threading.Event()
        watcher: Worker | None = None

        def park_until_watcher_waits_or_ends() -> None:
            in_extract.set()
            wait_for(lambda: watcher is not None)  # the watcher has started
            assert watcher is not None
            # The watcher is parked on a lock, or has already committed.
            wait_for(
                lambda: watcher.finished.is_set()
                or is_waiting_on_lock(observer, watcher_pid)
            )

        backfill = Worker(
            lambda: reconcile_document(
                backfill_conn,
                backfill_doc,
                backend=NullBackend(),
                config=_cfg(concepts=True),
                person_resolver=static_resolver([ResolvedPerson("alpha", "Alpha")]),
                extractor=_HookExtractor([_GAMMA], park_until_watcher_waits_or_ends),
                defer_tenant_refresh=True,
            )
        )
        backfill.start()
        assert in_extract.wait(WAIT_S), "backfill never reached its extraction"
        watcher = Worker(
            lambda: reconcile_document(
                watcher_conn,
                watcher_doc,
                backend=NullBackend(),
                config=_cfg(concepts=True),
                person_resolver=static_resolver([]),
                extractor=_HookExtractor([_GAMMA]),
            )
        )
        watcher.start()
        join_workers(backfill, watcher)

        assert backfill.error is None, f"backfill failed: {backfill.error!r}"
        assert watcher.error is None, f"watcher failed: {watcher.error!r}"
        assert _mentioned_keys(test_db, backfill_doc) == {"alpha", "gamma"}
        assert _mentioned_keys(test_db, watcher_doc) == {"gamma"}
    finally:
        for conn in (backfill_conn, watcher_conn, observer):
            conn.close()


def test_two_full_reconciles_do_not_deadlock_in_the_tenant_recompute(
    test_db: psycopg.Connection[Any],
) -> None:
    """Two non-deferred reconciles each hold one entity, then meet in the recompute.

    ``gate``, ``alpha`` and ``bravo`` are committed in that order, so the
    tenant-wide ``doc_count`` UPDATE reaches ``gate`` first (in heap order and
    in key order alike). A gate connection row-locks ``gate``:

    1. R1 upserts ``alpha`` (row lock), then its recompute parks on ``gate``.
    2. R2 upserts ``bravo`` (row lock), then its recompute parks on ``gate``.
    3. The gate releases. R1 takes ``gate`` and moves on to ``bravo`` (held
       by R2); R2 now waits on R1's ``gate``. That is a deadlock at ``94dd1cb``.

    With the graph write lock, R2 parks on the lock before it writes anything
    (step 2 never row-locks ``bravo``), so the two simply serialize.
    """
    _seed_persons(test_db, ["aaa-gate", "p-alpha", "p-bravo"])
    doc_one = seed_note_doc(test_db, "doc-one")
    doc_two = seed_note_doc(test_db, "doc-two")
    gate, conn_one, conn_two, observer = (connect_worker() for _ in range(4))
    try:
        gate.autocommit = False
        gate.execute(
            "SELECT 1 FROM graph_entities WHERE tenant_id = %s AND canonical_key = %s "
            "FOR UPDATE",
            (TENANT, "aaa-gate"),
        )

        def reconcile(conn: psycopg.Connection[Any], doc: str, key: str) -> Worker:
            return Worker(
                lambda: reconcile_document(
                    conn,
                    doc,
                    backend=NullBackend(),
                    config=_cfg(),
                    person_resolver=static_resolver([ResolvedPerson(key, key.title())]),
                )
            )

        first = reconcile(conn_one, doc_one, "p-alpha")
        first.start()
        # The first reconcile is parked on the gate.
        wait_for(lambda: is_waiting_on_lock(observer, conn_one.info.backend_pid))
        second = reconcile(conn_two, doc_two, "p-bravo")
        second.start()
        # The second reconcile is parked (on the gate at 94dd1cb, on the lock now).
        wait_for(lambda: is_waiting_on_lock(observer, conn_two.info.backend_pid))
        gate.rollback()
        join_workers(first, second)

        assert first.error is None, f"first reconcile failed: {first.error!r}"
        assert second.error is None, f"second reconcile failed: {second.error!r}"
        assert _mentioned_keys(test_db, doc_one) == {"p-alpha"}
        assert _mentioned_keys(test_db, doc_two) == {"p-bravo"}
    finally:
        for conn in (gate, conn_one, conn_two, observer):
            conn.close()


def test_concept_extraction_holds_no_graph_lock(
    test_db: psycopg.Connection[Any],
) -> None:
    """A graph write completes while another reconcile is inside its extraction.

    The extraction is a model call (seconds to a minute per document). It must
    run before the reconcile takes the graph write lock or any row lock, or
    every concurrent graph write waits behind the model. The hook does not
    return until the concurrent reconcile has finished, and that reconcile
    runs with a short ``lock_timeout``: if the extracting reconcile held any
    lock it needs, it fails with ``LockNotAvailable`` instead of finishing.
    """
    _seed_persons(test_db, ["alpha"])
    extracting_doc = seed_note_doc(test_db, "extracting-doc")
    writer_doc = seed_note_doc(test_db, "writer-doc")
    extracting_conn, writer_conn = connect_worker(), connect_worker()
    try:
        writer_conn.execute("SET lock_timeout = '3s'")
        writer = Worker(
            lambda: reconcile_document(
                writer_conn,
                writer_doc,
                backend=NullBackend(),
                config=_cfg(),
                person_resolver=static_resolver([ResolvedPerson("alpha", "Alpha")]),
            )
        )

        def run_the_writer_to_completion() -> None:
            writer.start()
            assert writer.finished.wait(WAIT_S), "the concurrent writer hung"

        extracting = Worker(
            lambda: reconcile_document(
                extracting_conn,
                extracting_doc,
                backend=NullBackend(),
                config=_cfg(concepts=True),
                person_resolver=static_resolver([ResolvedPerson("alpha", "Alpha")]),
                extractor=_HookExtractor([_GAMMA], run_the_writer_to_completion),
                defer_tenant_refresh=True,
            )
        )
        extracting.start()
        join_workers(extracting)

        assert writer.error is None, (
            f"a graph write waited behind a model call: {writer.error!r}"
        )
        assert extracting.error is None, f"extracting failed: {extracting.error!r}"
        assert _mentioned_keys(test_db, writer_doc) == {"alpha"}
        assert _mentioned_keys(test_db, extracting_doc) == {"alpha", "gamma"}
    finally:
        for conn in (extracting_conn, writer_conn):
            conn.close()



# --------------------------------------------------------------------------- #
# The plan read before the lock must still be current when the writes happen
# --------------------------------------------------------------------------- #
_DELTA = ExtractedEntity(
    entity_type="topic", canonical_key="delta", display_name="Delta", positions=(0,)
)


def _edit_body(conn: psycopg.Connection[Any], document_id: str, body: str) -> str:
    content_hash = hashlib.sha256(body.encode()).hexdigest()
    conn.execute(
        "UPDATE documents SET content = %s, content_hash = %s WHERE id = %s",
        (body, content_hash, document_id),
    )
    return content_hash


def _watermark_hashes(conn: psycopg.Connection[Any], document_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT content_hash FROM graph_index_state "
        "WHERE tenant_id = %s AND document_id = %s",
        (TENANT, document_id),
    ).fetchall()
    return {str(row[0]) for row in rows}


def test_a_stale_reconcile_never_overwrites_a_newer_edit(
    test_db: psycopg.Connection[Any],
) -> None:
    """Adopted from the PR #53 audit's probe (finding F1).

    The backfill (``older``) plans against body v1 and is still in its model
    call when the document is edited to v2 and the watcher's reconcile
    (``newer``) indexes v2 and commits. At ``d8baa50`` the older reconcile then
    committed v1's entities over v2's, with a watermark naming a body the
    document no longer has, and it stayed stale until the next build. It must
    notice the edit once it holds the lock, and write nothing.
    """
    doc = seed_note_doc(test_db, "edited-doc")
    older_conn, newer_conn, edit_conn = connect_worker(), connect_worker(), connect_worker()
    try:
        newer = Worker(
            lambda: reconcile_document(
                newer_conn,
                doc,
                backend=NullBackend(),
                config=_cfg(concepts=True),
                person_resolver=static_resolver([ResolvedPerson("beta", "Beta")]),
                extractor=_HookExtractor([_DELTA]),
            )
        )

        def edit_then_run_the_newer_reconcile() -> None:
            _edit_body(edit_conn, doc, "edited body v2")
            newer.start()
            assert newer.finished.wait(WAIT_S), "the newer reconcile hung"

        older = Worker(
            lambda: reconcile_document(
                older_conn,
                doc,
                backend=NullBackend(),
                config=_cfg(concepts=True),
                person_resolver=static_resolver([ResolvedPerson("alpha", "Alpha")]),
                extractor=_HookExtractor([_GAMMA], edit_then_run_the_newer_reconcile),
                defer_tenant_refresh=True,
            )
        )
        older.start()
        join_workers(older)

        assert newer.error is None, f"newer reconcile failed: {newer.error!r}"
        assert older.error is None, f"older reconcile failed: {older.error!r}"
        doc_hash = test_db.execute(
            "SELECT content_hash FROM documents WHERE id = %s", (doc,)
        ).fetchone()
        assert doc_hash is not None
        assert _mentioned_keys(test_db, doc) == {"beta", "delta"}, (
            "the stale reconcile overwrote the newer edit's graph"
        )
        assert _watermark_hashes(test_db, doc) == {doc_hash[0]}
    finally:
        for conn in (older_conn, newer_conn, edit_conn):
            conn.close()


def test_a_reconcile_whose_document_was_deleted_writes_nothing(
    test_db: psycopg.Connection[Any],
) -> None:
    """The document is deleted while its reconcile is in the model call.

    The reconcile must return ``skipped`` and write nothing. At ``d8baa50`` it
    went on to insert mentions of a document that no longer existed and failed
    on the foreign key.
    """
    doc = seed_note_doc(test_db, "deleted-doc")
    reconcile_conn, delete_conn = connect_worker(), connect_worker()
    try:

        def delete_the_document() -> None:
            delete_conn.execute("DELETE FROM documents WHERE id = %s", (doc,))

        result = reconcile_document(
            reconcile_conn,
            doc,
            backend=NullBackend(),
            config=_cfg(concepts=True),
            person_resolver=static_resolver([ResolvedPerson("alpha", "Alpha")]),
            extractor=_HookExtractor([_GAMMA], delete_the_document),
            defer_tenant_refresh=True,
        )

        assert result.skipped is True
        written = test_db.execute(
            "SELECT count(*) FROM graph_entities WHERE tenant_id = %s", (TENANT,)
        ).fetchone()
        assert written == (0,), "a reconcile of a deleted document wrote entities"
    finally:
        for conn in (reconcile_conn, delete_conn):
            conn.close()


def test_extraction_runs_with_no_transaction_or_lock_open(
    test_db: psycopg.Connection[Any],
) -> None:
    """During the model call the reconciling backend holds no lock of any kind.

    Not even an ``AccessShareLock`` from the pre-lock reads: one held for a
    model call stalls DDL behind it (migration 028's ``lock_timeout`` is 3s).
    The hook inspects the reconciling backend from an observer connection.
    """
    doc = seed_note_doc(test_db, "doc")
    reconcile_conn, observer = connect_worker(), connect_worker()
    seen: dict[str, Any] = {}
    try:
        pid = reconcile_conn.info.backend_pid

        def inspect_the_reconciling_backend() -> None:
            seen["locks"] = observer.execute(
                "SELECT locktype, mode FROM pg_locks WHERE pid = %s ORDER BY 1, 2",
                (pid,),
            ).fetchall()
            state = observer.execute(
                "SELECT state FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone()
            seen["state"] = state[0] if state is not None else None

        reconcile_document(
            reconcile_conn,
            doc,
            backend=NullBackend(),
            config=_cfg(concepts=True),
            person_resolver=static_resolver([ResolvedPerson("alpha", "Alpha")]),
            extractor=_HookExtractor([_GAMMA], inspect_the_reconciling_backend),
        )

        assert seen["locks"] == [], f"locks held during the model call: {seen['locks']}"
        assert seen["state"] == "idle", (
            f"a transaction was open during the model call: {seen['state']}"
        )
    finally:
        for conn in (reconcile_conn, observer):
            conn.close()


class _BodyExtractor:
    """Fake extractor that names ``gamma`` for the v1 body and ``delta`` otherwise."""

    version = "fake-model@deadlock-v1"

    def __init__(self, v1_body: str, hook: Callable[[], None]) -> None:
        self._v1_body = v1_body
        self._hook = hook

    def extract(self, text: str) -> list[ExtractedEntity]:
        self._hook()
        return [_GAMMA] if text == self._v1_body else [_DELTA]


def test_the_plan_never_pairs_one_edits_hash_with_another_edits_body(
    test_db: psycopg.Connection[Any],
) -> None:
    """An edit between the plan's hash read and body read, then a revert.

    The person resolver runs between the plan's two reads at ``0012a66``: the
    hash (v1) first, the body after it. Here the resolver edits the body to v2,
    and the extractor reverts it to v1 (byte-identical, so the same hash).
    At ``0012a66`` the plan paired v1's hash with v2's body. The re-check under
    the lock passed, because the hash matched, and v2's concepts were committed
    under v1's watermark. Nothing re-indexes that: the watermark matches the
    document. Now the body read is pinned to the hash read, so the plan sees
    that v1's body is gone and writes nothing. The edit's own reconcile
    indexes v2. Pairing v1's hash with v1's body would also be correct, so the
    test asserts only the claim: v2's concepts are never indexed under v1's
    hash.
    """
    doc = seed_note_doc(test_db, "torn-doc")
    v1 = test_db.execute("SELECT content FROM documents WHERE id = %s", (doc,)).fetchone()
    assert v1 is not None
    v1_body = str(v1[0])
    edit_conn, reconcile_conn = connect_worker(), connect_worker()
    try:

        def edit_to_v2(
            conn: Any,
            document_id: str,
            *,
            owner_keys: frozenset[str],
            sender_denylist: frozenset[str] = frozenset(),
        ) -> list[ResolvedPerson]:
            _edit_body(edit_conn, doc, v1_body + "\nedited to v2")
            return []

        def revert_to_v1() -> None:
            _edit_body(edit_conn, doc, v1_body)

        reconcile_document(
            reconcile_conn,
            doc,
            backend=NullBackend(),
            config=_cfg(concepts=True),
            person_resolver=edit_to_v2,
            extractor=_BodyExtractor(v1_body, revert_to_v1),
        )

        assert "delta" not in _mentioned_keys(test_db, doc), (
            "v2's concepts were indexed under v1's hash"
        )
    finally:
        for conn in (edit_conn, reconcile_conn):
            conn.close()


class _RecordingConnection(psycopg.Connection[Any]):
    """A psycopg connection that records every statement passed to ``execute``.

    A test double by subclassing; production code is untouched.
    """

    statements: list[str]

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        self.statements.append(str(query))
        return super().execute(query, params, **kwargs)


#: Any SELECT of the ``content`` column from ``documents``, whatever else it
#: selects (``\b`` keeps ``content_hash`` / ``content_type`` out).
_BODY_READ = re.compile(r"^\s*SELECT\b(?:(?!\bFROM\b).)*\bcontent\b.*\bFROM\s+documents\b", re.S)


def _body_reads(statements: list[str]) -> list[str]:
    return [query for query in statements if _BODY_READ.search(query)]


def test_an_indexed_document_is_planned_without_reading_its_body(
    test_db: psycopg.Connection[Any],
) -> None:
    """A re-run over an indexed corpus must not load every body for nothing.

    The first reconcile reads the body once, to extract. The second finds both
    watermarks current, so it must not read the body at all. Reading it eagerly
    on every plan would make ``build --backfill --concepts`` over an indexed
    corpus read every document's body for no work (PR #53 audit advisory).
    """
    doc = seed_note_doc(test_db, "indexed-doc")
    conn = _RecordingConnection.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    conn.statements = []
    try:

        def reconcile() -> Any:
            return reconcile_document(
                conn,
                doc,
                backend=NullBackend(),
                config=_cfg(concepts=True),
                person_resolver=static_resolver([]),
                extractor=_HookExtractor([_GAMMA]),
            )

        first = reconcile()
        first_reads = _body_reads(conn.statements)
        conn.statements.clear()
        second = reconcile()
        second_reads = _body_reads(conn.statements)
    finally:
        conn.close()

    assert first.skipped is False
    assert len(first_reads) == 1, f"expected one body read to extract: {first_reads}"
    assert second.skipped is True
    assert second_reads == [], f"an up-to-date document's body was read: {second_reads}"


# --------------------------------------------------------------------------- #
# A reconcile's rewrite vs a concurrent DELETE of the same document
# --------------------------------------------------------------------------- #
def test_a_rewrite_and_a_concurrent_document_delete_do_not_deadlock(
    test_db: psycopg.Connection[Any],
) -> None:
    """The PR #54 reviewer's cycle, and the plan's same-document residual.

    At ``8711ff0``:

    1. The reconcile (A) rewrites X: it DELETEs X's old mention rows. A gate
       row lock on X's contribution row parks A right after that, inside
       ``rewrite_doc_relational``.
    2. The worker (B) runs ``DELETE FROM documents WHERE id = X``. It takes X's
       tuple lock, and its ON DELETE CASCADE waits on A's deleted mention rows.
    3. The gate opens. A's INSERT of X's new mentions runs its FK check
       (``FOR KEY SHARE`` on X) and waits on B. ``DeadlockDetected`` follows;
       the reproduction aborted B, the worker.

    Now A takes ``FOR KEY SHARE`` on X as the first statement after the graph
    write lock (``ReconcilePlan.is_current``), before it touches a graph row.
    B's DELETE waits on A while holding nothing A needs. A commits, then B
    deletes X and its cascade. Neither side may raise, and the end state must
    be consistent: X is gone, and so is every graph row of X.
    """
    doc = seed_note_doc(test_db, "deleted-while-rewritten")
    persons = static_resolver([ResolvedPerson("alpha", "Alpha"), ResolvedPerson("bravo", "Bravo")])
    reconcile_document(test_db, doc, backend=NullBackend(), config=_cfg(), person_resolver=persons)
    gate, lane_conn, worker_conn, observer = (connect_worker() for _ in range(4))
    try:
        gate.autocommit = False
        locked = gate.execute(
            "SELECT 1 FROM graph_edge_contributions WHERE document_id = %s FOR UPDATE",
            (doc,),
        ).fetchall()
        assert len(locked) == 1, "the setup must leave X exactly one contribution row"

        lane = Worker(
            lambda: reconcile_document(
                lane_conn,
                doc,
                backend=NullBackend(),
                config=_cfg(),
                person_resolver=persons,
                force=True,
            )
        )
        lane.start()
        # The rewrite deleted X's mentions and is parked on the gate.
        wait_for(lambda: is_waiting_on_lock(observer, lane_conn.info.backend_pid))
        worker = Worker(
            lambda: worker_conn.execute("DELETE FROM documents WHERE id = %s", (doc,))
        )
        worker.start()
        wait_for(lambda: is_waiting_on_lock(observer, worker_conn.info.backend_pid))
        gate.rollback()
        join_workers(lane, worker)

        assert worker.error is None, f"the document DELETE failed: {worker.error!r}"
        assert lane.error is None, f"the reconcile failed: {lane.error!r}"
        remaining = test_db.execute(
            "SELECT (SELECT count(*) FROM documents WHERE id = %(d)s),"
            " (SELECT count(*) FROM graph_entity_mentions WHERE document_id = %(d)s),"
            " (SELECT count(*) FROM graph_edge_contributions WHERE document_id = %(d)s),"
            " (SELECT count(*) FROM graph_index_state WHERE document_id = %(d)s)",
            {"d": doc},
        ).fetchone()
        assert remaining == (0, 0, 0, 0), f"graph rows of the deleted doc remain: {remaining}"
    finally:
        for conn in (gate, lane_conn, worker_conn, observer):
            conn.close()
