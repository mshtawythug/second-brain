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
import os
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
from tests.graph_fakes import NullBackend, seed_note_doc, static_resolver
from tests.thread_wait import wait_for

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

TENANT = "default"

#: Upper bound on any single statement in a worker. A regression that turns a
#: deadlock into an unbounded wait fails here, not by hanging CI.
_STATEMENT_TIMEOUT = "20s"

#: Deadline for every condition wait and thread join, in seconds.
_WAIT_S = 15.0


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


class _Worker(threading.Thread):
    """Run ``fn`` on a thread; keep its result or exception for the test."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._fn = fn
        self.error: BaseException | None = None
        self.finished = threading.Event()

    def run(self) -> None:
        try:
            self._fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            self.error = exc
        finally:
            self.finished.set()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cfg(*, concepts: bool = False) -> ReconcileConfig:
    # generic_df_ratio 1.0 disables generic suppression for the tiny corpus.
    return ReconcileConfig(
        tenant_id=TENANT, generic_df_ratio=1.0, concepts_enabled=concepts
    )


def _connect() -> psycopg.Connection[Any]:
    conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5)
    conn.execute(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'")
    return conn


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


def _is_waiting_on_lock(observer: psycopg.Connection[Any], pid: int) -> bool:
    row = observer.execute(
        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
    ).fetchone()
    return row is not None and row[0] == "Lock"


def _join(*workers: _Worker) -> None:
    for worker in workers:
        worker.join(_WAIT_S * 2)
        assert not worker.is_alive(), "a reconcile worker hung"


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
    backfill_conn, watcher_conn, observer = _connect(), _connect(), _connect()
    try:
        watcher_conn.execute("SET deadlock_timeout = '10s'")
        watcher_pid = watcher_conn.info.backend_pid
        in_extract = threading.Event()
        watcher: _Worker | None = None

        def park_until_watcher_waits_or_ends() -> None:
            in_extract.set()
            wait_for(lambda: watcher is not None)  # the watcher has started
            assert watcher is not None
            # The watcher is parked on a lock, or has already committed.
            wait_for(
                lambda: watcher.finished.is_set()
                or _is_waiting_on_lock(observer, watcher_pid)
            )

        backfill = _Worker(
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
        assert in_extract.wait(_WAIT_S), "backfill never reached its extraction"
        watcher = _Worker(
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
        _join(backfill, watcher)

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
    gate, conn_one, conn_two, observer = _connect(), _connect(), _connect(), _connect()
    try:
        gate.autocommit = False
        gate.execute(
            "SELECT 1 FROM graph_entities WHERE tenant_id = %s AND canonical_key = %s "
            "FOR UPDATE",
            (TENANT, "aaa-gate"),
        )

        def reconcile(conn: psycopg.Connection[Any], doc: str, key: str) -> _Worker:
            return _Worker(
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
        wait_for(lambda: _is_waiting_on_lock(observer, conn_one.info.backend_pid))
        second = reconcile(conn_two, doc_two, "p-bravo")
        second.start()
        # The second reconcile is parked (on the gate at 94dd1cb, on the lock now).
        wait_for(lambda: _is_waiting_on_lock(observer, conn_two.info.backend_pid))
        gate.rollback()
        _join(first, second)

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
    extracting_conn, writer_conn = _connect(), _connect()
    try:
        writer_conn.execute("SET lock_timeout = '3s'")
        writer = _Worker(
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
            assert writer.finished.wait(_WAIT_S), "the concurrent writer hung"

        extracting = _Worker(
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
        _join(extracting)

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
    older_conn, newer_conn, edit_conn = _connect(), _connect(), _connect()
    try:
        newer = _Worker(
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
            assert newer.finished.wait(_WAIT_S), "the newer reconcile hung"

        older = _Worker(
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
        _join(older)

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
    reconcile_conn, delete_conn = _connect(), _connect()
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
    reconcile_conn, observer = _connect(), _connect()
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


def test_the_plan_reads_hash_and_body_from_one_row_version(
    test_db: psycopg.Connection[Any],
) -> None:
    """An edit between the plan's hash read and body read, then a revert.

    The person resolver runs between the plan's two reads at ``0012a66``: the
    hash (v1) first, the body after it. Here the resolver edits the body to v2,
    and the extractor reverts it to v1 (byte-identical, so the same hash).
    At ``0012a66`` the plan paired v1's hash with v2's body. The re-check under
    the lock passed, because the hash matched, and v2's concepts were committed
    under v1's watermark. Nothing re-indexes that: the watermark matches the
    document. The plan must index the body that goes with the hash it read.
    """
    doc = seed_note_doc(test_db, "torn-doc")
    v1 = test_db.execute("SELECT content FROM documents WHERE id = %s", (doc,)).fetchone()
    assert v1 is not None
    v1_body = str(v1[0])
    edit_conn, reconcile_conn = _connect(), _connect()
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

        current = test_db.execute("SELECT content FROM documents WHERE id = %s", (doc,))
        assert current.fetchone() == (v1_body,)
        assert _mentioned_keys(test_db, doc) == {"gamma"}, (
            "v2's concepts were indexed under v1's hash"
        )
    finally:
        for conn in (edit_conn, reconcile_conn):
            conn.close()
