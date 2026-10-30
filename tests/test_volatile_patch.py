"""The shared metadata-only refresh (``brain.ingest.volatile_patch``), against the 5434 database.

Used by ``ingest-calendar``, ``ingest-github`` and ``ingest-apple-notes``; their own
tests cover it end to end, these pin its contract directly.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.ingest.volatile_patch import refresh_volatile, stored_document
from tests.conftest import CountingEmbedder

KIND = "manual"
EXTERNAL_ID = "synthetic-volatile-1"


def _seed(conn: psycopg.Connection[Any], embedder: CountingEmbedder) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title="Synthetic", content="A synthetic body.", content_type="note",
            source_path=None, metadata={"state": "open", "kept": 1},
        ),
        source_kind=KIND,
        source_external_id=EXTERNAL_ID,
    )
    assert result.document_id is not None
    return result.document_id


def _chunks(conn: psycopg.Connection[Any], document_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id::text FROM chunks WHERE document_id = %s ORDER BY id", (document_id,)
    ).fetchall()
    return [str(r[0]) for r in rows]


def test_stored_document_finds_the_row_by_kind_and_external_id(
    test_db: psycopg.Connection, counting_embedder: CountingEmbedder
) -> None:
    doc_id = _seed(test_db, counting_embedder)

    assert stored_document(test_db, KIND, EXTERNAL_ID) == (doc_id, {"state": "open", "kept": 1})
    assert stored_document(test_db, KIND, "missing") is None
    assert stored_document(test_db, "github", EXTERNAL_ID) is None


def test_only_changed_keys_present_in_the_new_metadata_are_patched(
    test_db: psycopg.Connection, counting_embedder: CountingEmbedder
) -> None:
    doc_id = _seed(test_db, counting_embedder)
    chunks = _chunks(test_db, doc_id)
    embeds = counting_embedder.embed_calls

    outcome = refresh_volatile(
        test_db,
        document_id=doc_id,
        stored={"state": "open", "kept": 1},
        metadata={"state": "closed", "kept": 1, "ignored": "x"},
        keys=("state", "kept", "absent"),
        vault_root=None,
        graph_syncer=None,
    )

    assert outcome == "refreshed"
    assert stored_document(test_db, KIND, EXTERNAL_ID) == (
        doc_id, {"state": "closed", "kept": 1}
    )
    assert _chunks(test_db, doc_id) == chunks  # metadata only: no re-chunk
    assert counting_embedder.embed_calls == embeds  # and no embed


def test_nothing_changed_is_unchanged_and_writes_nothing(
    test_db: psycopg.Connection, counting_embedder: CountingEmbedder
) -> None:
    doc_id = _seed(test_db, counting_embedder)

    outcome = refresh_volatile(
        test_db,
        document_id=doc_id,
        stored={"state": "open"},
        metadata={"state": "open"},
        keys=("state", "absent"),
        vault_root=None,
        graph_syncer=None,
    )

    assert outcome == "unchanged"
