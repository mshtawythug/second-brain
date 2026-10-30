"""The shared metadata-only refresh (``brain.ingest.volatile_patch``), against the 5434 database.

Used by ``ingest-calendar``, ``ingest-github`` and ``ingest-apple-notes``; their own
tests cover it end to end, these pin its contract directly.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.ingest import ExtractedDoc, IngestResult, ingest_document
from brain.ingest.volatile_patch import (
    rare_outcomes,
    refresh_volatile,
    settle,
    stored_document,
)
from tests.conftest import CountingEmbedder

KIND = "manual"
EXTERNAL_ID = "synthetic-volatile-1"


def _seed(conn: psycopg.Connection[Any], embedder: CountingEmbedder) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title="Synthetic", content="A synthetic body.", content_type="note",
            source_path=None, metadata={"state": "open", "kept": 1, "absent": "stays"},
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

    assert stored_document(test_db, KIND, EXTERNAL_ID) == (
        doc_id, {"state": "open", "kept": 1, "absent": "stays"}
    )
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
        stored={"state": "open", "kept": 1, "absent": "stays"},
        metadata={"state": "closed", "kept": 1, "ignored": "x"},
        keys=("state", "kept", "absent"),
        vault_root=None,
        graph_syncer=None,
    )

    assert outcome == "refreshed"
    # "absent" is not in the new metadata, so it is left alone, never patched to null.
    assert stored_document(test_db, KIND, EXTERNAL_ID) == (
        doc_id, {"state": "closed", "kept": 1, "absent": "stays"}
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


# --- settle (BL-34): one rule for every sourced CLI ---------------------------------------

FOREIGN = "00000000-0000-4000-8000-0000000000aa"


def _settle(conn: psycopg.Connection[Any], result: IngestResult, stored: Any) -> str:
    return settle(
        conn,
        result,
        stored=stored,
        metadata={"state": "closed"},
        keys=("state",),
        vault_root=None,
        graph_syncer=None,
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (IngestResult(document_id=FOREIGN, created=True, body_changed=True), "ingested"),
        (IngestResult(document_id=FOREIGN, created=False, body_changed=True), "updated"),
        (IngestResult(document_id=None, created=False), "empty"),
    ],
)
def test_written_and_empty_results_settle_without_a_refresh(
    result: IngestResult, expected: str, test_db: psycopg.Connection
) -> None:
    assert _settle(test_db, result, stored=None) == expected


def test_a_foreign_row_with_no_own_row_is_same_body_and_untouched(
    test_db: psycopg.Connection, counting_embedder: CountingEmbedder
) -> None:
    """The content-hash fallback case: no own row, and another document's id came back."""
    other = _seed(test_db, counting_embedder)

    outcome = _settle(test_db, IngestResult(document_id=other, created=False), stored=None)

    assert outcome == "same_body"
    assert stored_document(test_db, KIND, EXTERNAL_ID) == (
        other, {"state": "open", "kept": 1, "absent": "stays"}
    )


def test_a_returned_row_that_is_not_the_own_row_is_same_body(
    test_db: psycopg.Connection, counting_embedder: CountingEmbedder
) -> None:
    other = _seed(test_db, counting_embedder)

    outcome = _settle(
        test_db,
        IngestResult(document_id=other, created=False),
        stored=(FOREIGN, {"state": "open"}),
    )

    assert outcome == "same_body"
    assert stored_document(test_db, KIND, EXTERNAL_ID)[1]["state"] == "open"  # type: ignore[index]


@pytest.mark.parametrize(("state", "expected"), [("open", "refreshed"), ("closed", "unchanged")])
def test_the_own_row_is_refreshed_from_its_stored_metadata(
    state: str, expected: str, test_db: psycopg.Connection,
    counting_embedder: CountingEmbedder,
) -> None:
    own = _seed(test_db, counting_embedder)

    outcome = _settle(
        test_db, IngestResult(document_id=own, created=False), stored=(own, {"state": state})
    )

    assert outcome == expected
    assert stored_document(test_db, KIND, EXTERNAL_ID)[1]["state"] == (  # type: ignore[index]
        "closed" if expected == "refreshed" else "open"
    )


@pytest.mark.parametrize(
    ("counts", "suffix"),
    [
        ({}, ""),
        ({"same_body": 0, "empty": 0}, ""),
        ({"same_body": 2, "empty": 0}, ", 2 same body as another event"),
        ({"same_body": 0, "empty": 1}, ", 1 empty"),
        ({"same_body": 1, "empty": 3}, ", 1 same body as another event, 3 empty"),
    ],
)
def test_rare_outcomes_appear_only_when_non_zero(counts: dict[str, int], suffix: str) -> None:
    assert rare_outcomes(counts, "event") == suffix
