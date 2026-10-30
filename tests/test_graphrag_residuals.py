"""RM-27: graph-lock residuals 2 and 3 (H-3), closed.

The residuals are recorded in ``docs/plans/2026-10-22-graph-entities-deadlock.md``
("Residuals"), and this ticket's plan is
``docs/plans/2026-10-30-rm-27-graph-lock-residuals.md``.

* **Residual 2.** ``build_graph`` raised ``GraphReconcileError`` for a document
  deleted between id enumeration and its read phase. A deleted document needs no
  graph, so it is now counted as ``skipped_deleted`` and the build goes on.
* **Residual 3.** ``ReconcilePlan.is_current`` compared ``content_hash`` only,
  so an edit that changed a document's participants without touching its body
  could be overtaken by an older reconcile. The plan now records an
  ``inputs_fingerprint`` over every column it read, and the write re-checks it.

Each concurrency test forces one interleaving with real connections against the
AGE test instance (port 5434). The barriers are production DI seams, never a
fixed sleep:

* the build's ``progress`` callback, for a delete that lands mid-build;
* the concept extractor, which runs between plan and write with no transaction
  open, for an edit that lands in that window;
* the person resolver, for an edit that lands inside the plan's own read
  transaction.

All people are synthetic (alice / bob / carol at example.com); no PII.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import psycopg
import pytest

from brain.errors import GraphReconcileError
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.build import BuildResult, build_graph
from brain.graph_rag.extract import ExtractedEntity
from brain.graph_rag.reconcile import (
    ReconcileConfig,
    ReconcileResult,
    ResolvedPerson,
    default_person_resolver,
    reconcile_document,
)
from brain.graph_rag.reconcile_plan import DocumentNotFound, compute_inputs_fingerprint
from brain.graph_rag.relational import DocInputs
from brain.queries import iter_all_document_ids
from brain.vault.derived_links.directory import DirectoryStore
from tests.graph_concurrency import WAIT_S, Worker, connect_worker, join_workers

TENANT = "default"

_ALICE = ("Alice", "alice@example.com")
_BOB = ("Bob", "bob@example.com")
_CAROL = ("Carol", "carol@example.com")

_TOPIC = ExtractedEntity(
    entity_type="topic", canonical_key="roadmap", display_name="Roadmap", positions=(0,)
)


# --------------------------------------------------------------------------- #
# Test doubles and helpers
# --------------------------------------------------------------------------- #
class _HookExtractor:
    """Fake concept extractor that runs ``hook`` before returning ``entities``.

    The concept extractor runs between the plan's read transaction and the write
    transaction, with no transaction open, so its hook is the window residual 3
    is about.
    """

    version = "fake-model@residuals-v1"

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


def _cfg(*, concepts: bool = False) -> ReconcileConfig:
    # generic_df_ratio 1.0 disables generic suppression for the tiny corpus.
    return ReconcileConfig(
        tenant_id=TENANT, generic_df_ratio=1.0, concepts_enabled=concepts
    )


def _backend(conn: psycopg.Connection[Any]) -> AgeBackend:
    backend = AgeBackend()
    backend.bootstrap(conn)
    return backend


def _seed_directory(conn: psycopg.Connection[Any]) -> None:
    store = DirectoryStore(conn)
    for name, email in (_ALICE, _BOB, _CAROL):
        store.upsert_pair(display_name=name, email=email, source="gmail")


def _headers(participants: Sequence[tuple[str, str]]) -> dict[str, str]:
    return {
        "from": f"{participants[0][0]} <{participants[0][1]}>",
        "to": ", ".join(f"{name} <{email}>" for name, email in participants[1:]),
    }


def _seed_gmail_doc(
    conn: psycopg.Connection[Any],
    external_id: str,
    participants: Sequence[tuple[str, str]],
) -> str:
    src = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('gmail', %s, '{}'::jsonb) RETURNING id",
        (external_id,),
    ).fetchone()
    assert src is not None
    metadata = {**_headers(participants), "thread_id": external_id}
    body = f"synthetic thread {external_id}\n<!-- {uuid.uuid4()} -->"
    row = conn.execute(
        "INSERT INTO documents "
        "(source_id, title, content, content_hash, content_type, metadata) "
        "VALUES (%s, %s, %s, %s, 'email', %s::jsonb) RETURNING id::text",
        (
            src[0],
            external_id,
            body,
            hashlib.sha256(body.encode()).hexdigest(),
            json.dumps(metadata),
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _set_participants(
    conn: psycopg.Connection[Any],
    document_id: str,
    participants: Sequence[tuple[str, str]],
    *,
    extra: dict[str, str] | None = None,
) -> None:
    """A metadata-only edit: the headers (and ``extra``) change, the body does not."""
    patch = {**_headers(participants), **(extra or {})}
    conn.execute(
        "UPDATE documents SET metadata = metadata || %s::jsonb WHERE id = %s",
        (json.dumps(patch), document_id),
    )


def _content_hash(conn: psycopg.Connection[Any], document_id: str) -> str:
    row = conn.execute(
        "SELECT content_hash FROM documents WHERE id = %s", (document_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def _mentioned_person_keys(conn: psycopg.Connection[Any], document_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT ge.canonical_key FROM graph_entity_mentions m "
        "JOIN graph_entities ge ON ge.tenant_id = m.tenant_id AND ge.id = m.entity_id "
        "WHERE m.tenant_id = %s AND m.document_id = %s AND ge.entity_type = 'person'",
        (TENANT, document_id),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _watermark_count(conn: psycopg.Connection[Any], document_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_index_state WHERE tenant_id = %s AND document_id = %s",
        (TENANT, document_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _graph_rows_naming(conn: psycopg.Connection[Any], document_id: str) -> int:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM graph_entity_mentions WHERE document_id = %s)"
        " + (SELECT count(*) FROM graph_edge_contributions WHERE document_id = %s)"
        " + (SELECT count(*) FROM graph_index_state WHERE document_id = %s)",
        (document_id, document_id, document_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


# --------------------------------------------------------------------------- #
# Residual 2 — a document deleted mid-build is skipped, not raised (AC1)
# --------------------------------------------------------------------------- #
def test_a_document_deleted_mid_build_is_skipped_deleted(
    test_db: psycopg.Connection[Any],
) -> None:
    """AC1: B is deleted after its id was enumerated and before its read phase.

    The ids are enumerated up front, exactly as ``brain graphrag build`` does.
    Once A is processed, the progress callback has a worker delete B on its own
    connection and waits for that commit, so the build reaches B after it is
    gone. Before RM-27 the build died there with ``GraphReconcileError``.
    """
    backend = _backend(test_db)
    _seed_directory(test_db)
    expected_keys = {
        _seed_gmail_doc(test_db, external_id, participants): keys
        for external_id, participants, keys in (
            ("t1", (_ALICE, _BOB), {"alice", "bob"}),
            ("t2", (_ALICE, _CAROL), {"alice", "carol"}),
            ("t3", (_BOB, _CAROL), {"bob", "carol"}),
        )
    }
    # Ascending-id order (the build's resumable order), not insertion order.
    ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    doc_a, doc_b, doc_c = ids
    delete_conn = connect_worker()
    seen: list[tuple[int, str, bool]] = []
    try:
        deleter = Worker(
            lambda: delete_conn.execute("DELETE FROM documents WHERE id = %s", (doc_b,))
        )

        def delete_b_after_a(processed: int, document_id: str, result: ReconcileResult) -> None:
            seen.append((processed, document_id, result.skipped))
            if document_id == doc_a:
                deleter.start()
                assert deleter.finished.wait(WAIT_S), "the delete hung"

        result = build_graph(
            test_db, ids, backend=backend, config=_cfg(), progress=delete_b_after_a
        )
        join_workers(deleter)

        assert deleter.error is None, f"the delete failed: {deleter.error!r}"
        assert result == BuildResult(
            processed=3,
            reconciled=2,
            skipped=0,
            skipped_deleted=1,
            orphans_removed=result.orphans_removed,
            relationship_count=result.relationship_count,
        )
        assert result.reconciled + result.skipped + result.skipped_deleted == result.processed
        assert seen == [(1, doc_a, False), (2, doc_b, True), (3, doc_c, False)]
        assert _mentioned_person_keys(test_db, doc_a) == expected_keys[doc_a]
        assert _mentioned_person_keys(test_db, doc_c) == expected_keys[doc_c]
        assert _graph_rows_naming(test_db, doc_b) == 0
    finally:
        delete_conn.close()


def test_a_missing_document_raises_document_not_found_from_reconcile(
    test_db: psycopg.Connection[Any],
) -> None:
    """The single-document path still raises, now with a precise subclass.

    ``DocumentNotFound`` subclasses ``GraphReconcileError``, so every existing
    ``except GraphReconcileError`` keeps catching it.
    """
    missing = str(uuid.uuid4())
    with pytest.raises(DocumentNotFound) as excinfo:
        reconcile_document(test_db, missing, backend=_backend(test_db), config=_cfg())
    assert isinstance(excinfo.value, GraphReconcileError)


def test_build_still_raises_any_other_reconcile_error(
    test_db: psycopg.Connection[Any],
) -> None:
    """Only a deleted document is tolerated: a config error still stops the build."""
    _seed_directory(test_db)
    doc = _seed_gmail_doc(test_db, "t1", (_ALICE, _BOB))
    with pytest.raises(GraphReconcileError) as excinfo:
        build_graph(test_db, [doc], backend=_backend(test_db), config=_cfg(concepts=True))
    assert not isinstance(excinfo.value, DocumentNotFound)


# --------------------------------------------------------------------------- #
# Residual 3 — the write re-checks every input the plan read (AC2, AC3)
# --------------------------------------------------------------------------- #
def test_a_participants_only_edit_between_plan_and_write_invalidates_the_plan(
    test_db: psycopg.Connection[Any],
) -> None:
    """AC2: Carol is added to the thread while the reconcile is in its model call.

    The body, and so ``content_hash``, does not change. Before RM-27 the write's
    re-check compared ``content_hash`` only, so the reconcile committed the
    person set it resolved before the edit ({alice, bob}) with a watermark that
    said it was current. Now the plan is dropped, and the next reconcile indexes
    the edited participants.
    """
    backend = _backend(test_db)
    _seed_directory(test_db)
    doc = _seed_gmail_doc(test_db, "t1", (_ALICE, _BOB))
    hash_before = _content_hash(test_db, doc)
    edit_conn = connect_worker()
    try:
        result = reconcile_document(
            test_db,
            doc,
            backend=backend,
            config=_cfg(concepts=True),
            extractor=_HookExtractor(
                [_TOPIC], lambda: _set_participants(edit_conn, doc, (_ALICE, _BOB, _CAROL))
            ),
        )

        assert _content_hash(test_db, doc) == hash_before, "the edit must be metadata-only"
        assert result.skipped is True
        assert _mentioned_person_keys(test_db, doc) == set()
        assert _watermark_count(test_db, doc) == 0

        again = reconcile_document(
            test_db,
            doc,
            backend=backend,
            config=_cfg(concepts=True),
            extractor=_HookExtractor([_TOPIC]),
        )
        assert again.skipped is False
        assert _mentioned_person_keys(test_db, doc) == {"alice", "bob", "carol"}
    finally:
        edit_conn.close()


def test_a_no_op_recheck_still_passes(test_db: psycopg.Connection[Any]) -> None:
    """AC3: an edit that leaves every input the plan read unchanged is not a reason to drop it.

    The hook rewrites the same participants and changes a metadata key the plan
    never reads. The plan is still current and is applied.
    """
    backend = _backend(test_db)
    _seed_directory(test_db)
    doc = _seed_gmail_doc(test_db, "t1", (_ALICE, _BOB))
    edit_conn = connect_worker()
    try:
        result = reconcile_document(
            test_db,
            doc,
            backend=backend,
            config=_cfg(concepts=True),
            extractor=_HookExtractor(
                [_TOPIC],
                lambda: _set_participants(
                    edit_conn, doc, (_ALICE, _BOB), extra={"label": "synthetic-label"}
                ),
            ),
        )

        assert result.skipped is False
        assert _mentioned_person_keys(test_db, doc) == {"alice", "bob"}
        assert _watermark_count(test_db, doc) == 2  # people + concepts
    finally:
        edit_conn.close()


def test_the_fingerprint_is_read_before_the_resolver(
    test_db: psycopg.Connection[Any],
) -> None:
    """An edit that commits inside the plan's own read transaction is still caught.

    The plan transaction is READ COMMITTED, so each statement sees a fresh
    snapshot. The resolver below returns the persons of the OLDER participants
    and then lets an edit commit. A fingerprint read after the resolver would
    describe the newer row, match it at write time, and commit the older persons.
    Read first, it describes the older row and the write drops the plan.
    """
    backend = _backend(test_db)
    _seed_directory(test_db)
    doc = _seed_gmail_doc(test_db, "t1", (_ALICE, _BOB))
    edit_conn = connect_worker()
    try:

        def resolve_then_edit(
            conn: psycopg.Connection[Any],
            document_id: str,
            *,
            owner_keys: frozenset[str],
            sender_denylist: frozenset[str] = frozenset(),
        ) -> list[ResolvedPerson]:
            persons = default_person_resolver(
                conn, document_id, owner_keys=owner_keys, sender_denylist=sender_denylist
            )
            _set_participants(edit_conn, document_id, (_ALICE, _BOB, _CAROL))
            return persons

        result = reconcile_document(
            test_db, doc, backend=backend, config=_cfg(), person_resolver=resolve_then_edit
        )

        assert result.skipped is True
        assert _mentioned_person_keys(test_db, doc) == set()
        assert _watermark_count(test_db, doc) == 0
    finally:
        edit_conn.close()


# --------------------------------------------------------------------------- #
# compute_inputs_fingerprint — pure
# --------------------------------------------------------------------------- #
def _inputs(**overrides: Any) -> DocInputs:
    fields: dict[str, Any] = {
        "content_hash": "a" * 64,
        "content_type": "email",
        "source_kind": "gmail",
        "metadata": {**_headers((_ALICE, _BOB)), "thread_id": "t1"},
    }
    fields.update(overrides)
    return DocInputs(**fields)


def test_the_fingerprint_is_a_stable_sha256() -> None:
    first = compute_inputs_fingerprint(_inputs())
    assert first == compute_inputs_fingerprint(_inputs())
    assert len(first) == 64
    int(first, 16)


def test_the_fingerprint_ignores_metadata_the_plan_never_reads() -> None:
    base = compute_inputs_fingerprint(_inputs())
    noisy = _inputs(metadata={**_headers((_ALICE, _BOB)), "thread_id": "t9", "label": "x"})
    assert compute_inputs_fingerprint(noisy) == base


def test_the_fingerprint_ignores_participant_order() -> None:
    alice_first = _inputs(metadata=_headers((_ALICE, _BOB)))
    bob_first = _inputs(metadata=_headers((_BOB, _ALICE)))
    assert compute_inputs_fingerprint(alice_first) == compute_inputs_fingerprint(bob_first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"content_hash": "b" * 64},
        {"content_type": "note"},
        {"source_kind": "krisp"},
        {"source_kind": None},
        {"metadata": _headers((_ALICE, _BOB, _CAROL))},
        {"metadata": {}},
    ],
    ids=["content_hash", "content_type", "source_kind", "no_source", "participants", "none"],
)
def test_each_input_moves_the_fingerprint(overrides: dict[str, Any]) -> None:
    assert compute_inputs_fingerprint(_inputs(**overrides)) != compute_inputs_fingerprint(
        _inputs()
    )


def test_krisp_participant_keys_move_the_fingerprint() -> None:
    def krisp(keys: list[str]) -> DocInputs:
        return _inputs(
            content_type="transcript",
            source_kind="krisp",
            metadata={"_participant_keys": keys},
        )

    assert compute_inputs_fingerprint(krisp(["Alice", "Bob"])) == compute_inputs_fingerprint(
        krisp(["Bob", "Alice"])
    )
    assert compute_inputs_fingerprint(krisp(["Alice", "Bob"])) != compute_inputs_fingerprint(
        krisp(["Alice", "Bob", "Carol"])
    )
