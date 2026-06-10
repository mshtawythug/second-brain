"""Tests for the ``brain_resurface`` MCP tool (Plan 02 Phase 2).

Installs a fresh ``_State`` (real test-DB Config + fake embedder) and calls the
tool function directly, mirroring ``tests/test_mcp_server.py``.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from mcp import McpError

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
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _insert_doc(
    conn: psycopg.Connection,
    *,
    title: str,
    age_days: float = 200.0,
    source_kind: str = "manual",
) -> str:
    src = conn.execute(
        "INSERT INTO sources (kind, external_id) VALUES (%s, %s) RETURNING id::text",
        (source_kind, str(uuid.uuid4())),
    ).fetchone()
    assert src is not None
    row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type, tags,
             ingested_at)
        VALUES (%s, %s, %s, %s, %s, %s, now() - (%s * interval '1 day'))
        RETURNING id::text
        """,
        (
            src[0],
            title,
            "resurface mcp body text",
            str(uuid.uuid4()),
            "note",
            [],
            age_days,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_brain_resurface_returns_items(
    mcp_state: mcp_server._State, test_db: psycopg.Connection
) -> None:
    """Happy path: returns an ``items`` list ordered oldest-first."""
    old = _insert_doc(test_db, title="old", age_days=300.0)
    mid = _insert_doc(test_db, title="mid", age_days=150.0)

    out = mcp_server.brain_resurface(min_age_days=14)

    assert list(out.keys()) == ["items"]
    items = out["items"]
    assert [it["id"] for it in items] == [old, mid]
    assert items[0]["last_access_days"] is None
    assert items[0]["title"] == "old"


def test_brain_resurface_limit_and_source(
    mcp_state: mcp_server._State, test_db: psycopg.Connection
) -> None:
    """``limit`` and ``source_kind`` params are honored."""
    _insert_doc(test_db, title="manual a", source_kind="manual", age_days=200.0)
    _insert_doc(test_db, title="manual b", source_kind="manual", age_days=180.0)
    _insert_doc(test_db, title="krisp a", source_kind="krisp", age_days=400.0)

    out = mcp_server.brain_resurface(limit=1, min_age_days=14, source_kind="manual")

    items = out["items"]
    assert len(items) == 1
    assert items[0]["source_kind"] == "manual"


def test_brain_resurface_empty(
    mcp_state: mcp_server._State, test_db: psycopg.Connection
) -> None:
    """Empty corpus → empty items list, no error."""
    out = mcp_server.brain_resurface()
    assert out == {"items": []}


def test_brain_resurface_uses_config_defaults(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Omitting limit/min_age_days falls back to the server's cfg values.

    Regression for the bug where the tool hard-coded 7/14 and ignored
    BRAIN_RESURFACE_LIMIT / BRAIN_RESURFACE_MIN_AGE_DAYS.
    """
    import dataclasses

    state = mcp_server._State(
        cfg=dataclasses.replace(
            Config(database_url=TEST_DATABASE_URL),
            resurface_limit=2,
            resurface_min_age_days=14,
        ),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    for i in range(5):
        _insert_doc(test_db, title=f"doc {i}", age_days=200.0 + i)

    out = mcp_server.brain_resurface()  # no explicit limit

    assert len(out["items"]) == 2  # cfg.resurface_limit, not the old hard-coded 7


def test_brain_resurface_invalid_limit_raises_invalid_params(
    mcp_state: mcp_server._State, test_db: psycopg.Connection
) -> None:
    """limit < 1 surfaces as an McpError (INVALID_PARAMS), not a raw ValueError."""
    _insert_doc(test_db, title="a", age_days=200.0)
    with pytest.raises(McpError):
        mcp_server.brain_resurface(limit=0)


def test_brain_resurface_wraps_db_error(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A psycopg error surfaces as an McpError, not a raw DB exception."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise psycopg.OperationalError("simulated outage")

    monkeypatch.setattr(mcp_server, "resurface_docs", _boom)
    with pytest.raises(McpError):
        mcp_server.brain_resurface()
