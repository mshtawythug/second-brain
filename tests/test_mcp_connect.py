"""Tests for the ``brain_connect_*`` MCP tools (Plan 07 Phase 3).

Each test installs a fresh ``_State`` (test-DB Config sandboxed to a ``tmp_path``
vault + fake embedder) and calls the tool function directly, mirroring
``tests/test_mcp_capture.py``. ``enricher`` / ``graph_syncer`` stay ``None`` so
the suite never depends on a live Ollama. All content is synthetic.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from mcp.types import INVALID_PARAMS

from brain import mcp_server
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document
from brain.mcp_compat import MCPError

from .conftest import FakeEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def connect_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: FakeEmbedder,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    """Install a server state pointing at the test DB + a tmp vault."""
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _make_doc(
    conn: psycopg.Connection,
    embedder: FakeEmbedder,
    *,
    title: str,
    content: str,
    vault_path: str,
) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,
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
    conn.execute(
        "UPDATE documents SET vault_path = %s WHERE id = %s",
        (vault_path, result.document_id),
    )
    return result.document_id


def _insert_suggestion(
    conn: psycopg.Connection, *, source: str, target: str, status: str = "pending"
) -> str:
    row = conn.execute(
        "INSERT INTO link_suggestions "
        "(source_doc_id, target_doc_id, score, graph_score, embed_score, status) "
        "VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s) RETURNING id::text",
        (source, target, 0.7, 0.8, 0.6, status),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _status(conn: psycopg.Connection, sid: str) -> str:
    row = conn.execute(
        "SELECT status FROM link_suggestions WHERE id = %s::uuid", (sid,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_brain_connect_list(
    connect_state: mcp_server._State,
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    a = _make_doc(test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md")
    b = _make_doc(test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md")
    _insert_suggestion(test_db, source=a, target=b)
    rows = mcp_server.brain_connect_list(limit=20, status="pending")
    assert len(rows) == 1
    assert rows[0]["source_title"] == "Doc A"
    assert rows[0]["target_title"] == "Doc B"
    for key in ("id", "score", "graph_score", "embed_score"):
        assert key in rows[0]


def test_brain_connect_list_all_status(
    connect_state: mcp_server._State,
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    a = _make_doc(test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md")
    b = _make_doc(test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md")
    _insert_suggestion(test_db, source=a, target=b, status="rejected")
    assert mcp_server.brain_connect_list(status="pending") == []
    assert len(mcp_server.brain_connect_list(status="all")) == 1


def test_brain_connect_accept_status_only(
    connect_state: mcp_server._State,
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    a = _make_doc(test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md")
    b = _make_doc(test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md")
    sid = _insert_suggestion(test_db, source=a, target=b)
    result = mcp_server.brain_connect_accept(id=sid[:8])
    assert result == {"status": "accepted", "wikilink_written": False}
    assert _status(test_db, sid) == "accepted"


def test_brain_connect_accept_write(
    connect_state: mcp_server._State,
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
    tmp_path: Path,
) -> None:
    src = _make_doc(
        test_db, fake_embedder, title="Source", content="s", vault_path="notes/src.md"
    )
    tgt = _make_doc(
        test_db, fake_embedder, title="Target Doc", content="t", vault_path="notes/tgt.md"
    )
    sid = _insert_suggestion(test_db, source=src, target=tgt)
    source_file = tmp_path / "notes" / "src.md"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("# Source\n\nbody\n", encoding="utf-8")

    result = mcp_server.brain_connect_accept(id=sid[:8], write=True)
    assert result == {"status": "accepted", "wikilink_written": True}
    text = source_file.read_text(encoding="utf-8")
    assert "## See Also" in text
    assert "[[notes/tgt|Target Doc]]" in text

    # Idempotent second write.
    again = mcp_server.brain_connect_accept(id=sid[:8], write=True)
    assert again == {"status": "accepted", "wikilink_written": False}


def test_brain_connect_reject(
    connect_state: mcp_server._State,
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    a = _make_doc(test_db, fake_embedder, title="Doc A", content="a", vault_path="n/a.md")
    b = _make_doc(test_db, fake_embedder, title="Doc B", content="b", vault_path="n/b.md")
    sid = _insert_suggestion(test_db, source=a, target=b)
    result = mcp_server.brain_connect_reject(id=sid[:8])
    assert result == {"status": "rejected"}
    assert _status(test_db, sid) == "rejected"


def test_brain_connect_list_invalid_status_raises(
    connect_state: mcp_server._State,
) -> None:
    # Codex R1 #3: a typo'd status must raise with INVALID_PARAMS, not silently
    # return []. Assert the specific error code (Codex R2 #2) — the fix IS the
    # code mapping, so a wrong code must fail this test.
    with pytest.raises(MCPError) as exc:
        mcp_server.brain_connect_list(status="pendign")
    assert exc.value.error.code == INVALID_PARAMS


def test_brain_connect_accept_write_missing_file_leaves_pending(
    connect_state: mcp_server._State,
    test_db: psycopg.Connection,
    fake_embedder: FakeEmbedder,
) -> None:
    # Codex R1 #1 (MCP parity): a failed write keeps the row pending.
    src = _make_doc(
        test_db, fake_embedder, title="Source", content="s", vault_path="notes/src.md"
    )
    tgt = _make_doc(
        test_db, fake_embedder, title="Target", content="t", vault_path="notes/tgt.md"
    )
    sid = _insert_suggestion(test_db, source=src, target=tgt)
    # No source file created under the tmp vault → write must fail.
    with pytest.raises(MCPError) as exc:
        mcp_server.brain_connect_accept(id=sid[:8], write=True)
    assert exc.value.error.code == INVALID_PARAMS
    assert _status(test_db, sid) == "pending"


def test_brain_connect_accept_unknown_id_raises(
    connect_state: mcp_server._State,
) -> None:
    with pytest.raises(MCPError) as exc:
        mcp_server.brain_connect_accept(id="abcdef")
    assert exc.value.error.code == INVALID_PARAMS


def test_brain_connect_reject_unknown_id_raises(
    connect_state: mcp_server._State,
) -> None:
    with pytest.raises(MCPError) as exc:
        mcp_server.brain_connect_reject(id="abcdef")
    assert exc.value.error.code == INVALID_PARAMS
