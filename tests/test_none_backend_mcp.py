"""MCP-layer proofs for the FTS-only ``BRAIN_EMBEDDER=none`` backend.

No coercion code lives in ``mcp_server`` — degradation happens inside
``hybrid_search`` (duck-typed on ``produces_embeddings``) and inside the ingest
pipeline (``_embed_chunks``). These tests lock that the MCP tools inherit the
degradation for free:

- ``brain_search`` degrades to the FTS leg and never calls ``embed()`` (A8).
- ``brain_edit`` re-chunks a body edit and stores NULL embeddings (A6).
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.embeddings import NullEmbedder
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def null_mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    """Install an MCP ``_State`` running the FTS-only ``NullEmbedder`` backend.

    ``db_conn`` is left ``None`` so ``_mcp_conn`` opens its own per-call
    connection against the test DB (committed seed rows are visible). ``cfg``
    pins ``vault_path`` to a tmp dir so any mirror write stays hermetic.
    """
    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            embedder="none",
            vault_path=tmp_path / "vault",
        ),
        embedder=NullEmbedder(),
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _seed_null_doc(conn: psycopg.Connection, *, title: str, content: str) -> str:
    """Ingest one manual doc under the null backend and return its id."""
    result = ingest_document(
        conn,
        embedder=NullEmbedder(),
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=[],
    )
    assert result.document_id is not None
    return result.document_id


def _non_null_embedding_count(conn: psycopg.Connection, document_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM chunks "
        "WHERE document_id = %s AND embedding IS NOT NULL",
        (document_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# A8 — brain_search degrades through the library coercion (no embed call)
# ---------------------------------------------------------------------------


def test_brain_search_degrades_to_fts_under_null_backend(
    null_mcp_state: mcp_server._State,  # noqa: ARG001 — installs the state
    test_db: psycopg.Connection,
) -> None:
    """``brain_search`` returns FTS results and never raises under the null backend.

    The MCP tool adds NO coercion of its own — degradation lives inside
    ``hybrid_search``. If it did not apply to the library caller,
    ``NullEmbedder.embed()`` would raise; a non-empty, exception-free result set
    is the proof.
    """
    doc_id = _seed_null_doc(
        test_db,
        title="Quarterly planning",
        content="Synthetic body about the quarterly planning roadmap and goals.",
    )
    payload = mcp_server.brain_search(query="quarterly planning roadmap")
    ids = [r["id"] for r in payload["results"]]
    assert doc_id in ids


# ---------------------------------------------------------------------------
# A6 — brain_edit content edit under the null backend leaves NULL embeddings
# ---------------------------------------------------------------------------


def test_brain_edit_body_under_null_backend_leaves_null_embeddings(
    null_mcp_state: mcp_server._State,
    test_db: psycopg.Connection,
) -> None:
    """A body edit via ``brain_edit`` re-chunks and stores NULL embeddings, no raise."""
    doc_id = _seed_null_doc(
        test_db,
        title="Editable note",
        content="Original synthetic body mentioning the alpha milestone.",
    )
    result = mcp_server.brain_edit(
        id_prefix=doc_id,
        content="Rewritten synthetic body mentioning the beta milestone.",
    )
    assert result["rechunked"] is True
    assert "content" in result["fields_changed"]
    assert _non_null_embedding_count(test_db, doc_id) == 0
