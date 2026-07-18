"""Task 2.10 (MCP part): ``limit >= 1`` guards on the MCP tool surface.

``brain_search`` / ``brain_list`` / ``brain_resurface`` previously accepted a
``limit < 1``: ``brain_search`` sliced ``results[:limit]`` (negative → silently
dropped the tail = wrong data), ``brain_list`` sent ``LIMIT -N`` to Postgres
(INTERNAL_ERROR), and ``brain_resurface`` relied on ``resurface_docs`` to reject
it only after opening a DB connection. Each now mirrors the fail-fast guard in
``brain_ask``: an out-of-range ``limit`` surfaces as ``INVALID_PARAMS`` BEFORE
any DB round-trip.

Installs a fresh ``_State`` (real test-DB Config + fake embedder) and calls the
tool functions directly, mirroring ``tests/test_mcp_resurface.py``.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from mcp import McpError
from mcp.types import INVALID_PARAMS

from brain import mcp_server
from brain.config import Config

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    # ``vault_path`` sandboxed to a per-test tmp dir so any mirroring MCP tool
    # writes there, never the live ``~/brain-vault`` (belt-and-suspenders atop
    # the ``_default_vault_path`` env fix + conftest ``BRAIN_VAULT_PATH`` pin).
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _seed_doc(conn: psycopg.Connection, *, title: str) -> None:
    src = conn.execute(
        "INSERT INTO sources (kind, external_id) VALUES ('manual', %s) RETURNING id",
        (str(uuid.uuid4()),),
    ).fetchone()
    assert src is not None
    conn.execute(
        "INSERT INTO documents (source_id, title, content, content_hash, "
        "content_type, tags) VALUES (%s, %s, %s, %s, 'note', %s)",
        (src[0], title, "limit guard body", str(uuid.uuid4()), []),
    )


@pytest.mark.parametrize("bad_limit", [-3, 0])
def test_brain_search_rejects_non_positive_limit(
    mcp_state: mcp_server._State, bad_limit: int
) -> None:
    """``brain_search`` with ``limit < 1`` → INVALID_PARAMS, never wrong data.

    Pre-fix ``hybrid_search`` sliced ``results[:limit]`` — a negative limit
    silently dropped the tail and returned a truncated result set with no error.
    """
    with pytest.raises(McpError) as excinfo:
        mcp_server.brain_search(query="anything", limit=bad_limit)
    assert excinfo.value.error.code == INVALID_PARAMS


@pytest.mark.parametrize("bad_limit", [-3, 0])
def test_brain_list_rejects_non_positive_limit(
    mcp_state: mcp_server._State, test_db: psycopg.Connection, bad_limit: int
) -> None:
    """``brain_list`` with ``limit < 1`` → INVALID_PARAMS, never a DB error.

    Pre-fix ``list_documents`` passed the value straight into ``LIMIT %s`` —
    ``LIMIT -3`` raised a Postgres error surfaced as INTERNAL_ERROR.
    """
    _seed_doc(test_db, title="a")
    with pytest.raises(McpError) as excinfo:
        mcp_server.brain_list(limit=bad_limit)
    assert excinfo.value.error.code == INVALID_PARAMS


@pytest.mark.parametrize("bad_limit", [-3, 0])
def test_brain_resurface_rejects_non_positive_limit_fail_fast(
    mcp_state: mcp_server._State,
    monkeypatch: pytest.MonkeyPatch,
    bad_limit: int,
) -> None:
    """``brain_resurface`` with ``limit < 1`` → INVALID_PARAMS before any DB work.

    The explicit guard mirrors ``brain_ask`` and fails fast — ``resurface_docs``
    must not run. Pre-fix the tool opened a connection and only rejected the
    value inside ``resurface_docs``; this asserts the fail-fast contract.
    """

    def _must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("resurface_docs must not run for an invalid limit")

    monkeypatch.setattr(mcp_server, "resurface_docs", _must_not_run)
    with pytest.raises(McpError) as excinfo:
        mcp_server.brain_resurface(limit=bad_limit)
    assert excinfo.value.error.code == INVALID_PARAMS
