"""Tests for T6 perf improvements: DB-08 persistent DB connection + F7 warm-embed.

DB-08
-----
:class:`~brain.db.PersistentConnection` is opened once in :func:`~brain.mcp_server.main`
and reused across all MCP tool calls via :func:`~brain.mcp_server._mcp_conn`.
Tests verify:

* connection object identity — the *same* psycopg connection is returned across
  multiple calls within one server session;
* transparent reconnect — if the connection is closed mid-session the next
  :meth:`~brain.db.PersistentConnection.get` opens a fresh one;
* :func:`~brain.mcp_server._wrap_db_error` triggers
  :meth:`~brain.db.PersistentConnection.reconnect` on
  :class:`psycopg.OperationalError` but NOT on other error sub-classes;
* a failed reconnect is logged at ERROR and swallowed (does not re-raise);
* the ``db_conn=None`` fallback path is safe.

F7
--
:func:`~brain.mcp_server._warmup_embed` fires at server startup via
:func:`~brain.mcp_server.main` so the embedding model is in VRAM before the
first real query.  Tests verify:

* at least one embed call is made on success;
* :class:`~brain.embeddings.OllamaEmbedError` is swallowed and a WARNING is
  logged (server must remain up).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.db import PersistentConnection
from brain.embeddings import OllamaEmbedError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


# ---------------------------------------------------------------------------
# Local test helpers / doubles
# ---------------------------------------------------------------------------


class _CountingEmbedder:
    """Embedder that counts how many embed() calls it receives."""

    def __init__(self, dim: int = 4096) -> None:
        self.dim = dim
        self.embed_calls = 0

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        self.embed_calls += 1
        return [[0.0] * self.dim for _ in texts]

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


class _AlwaysFailEmbedder:
    """Embedder that always raises OllamaEmbedError — used to simulate Ollama cold."""

    def __init__(self, dim: int = 4096) -> None:
        self.dim = dim
        self.embed_calls = 0

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        self.embed_calls += 1
        raise OllamaEmbedError("simulated Ollama unavailable")

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_pconn() -> Iterator[PersistentConnection]:
    """Open a real PersistentConnection against the test DB; close on teardown."""
    pc = PersistentConnection(TEST_DATABASE_URL)
    yield pc
    pc.close()


@pytest.fixture()
def mcp_state_with_pconn(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps schema fresh for each test
    fake_embedder: Any,
    real_pconn: PersistentConnection,
) -> Iterator[mcp_server._State]:
    """Install a _State with a real PersistentConnection into the MCP global."""
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,
        db_conn=real_pconn,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


# ---------------------------------------------------------------------------
# DB-08 — PersistentConnection unit tests (no live DB needed)
# ---------------------------------------------------------------------------


class TestPersistentConnectionUnit:
    """Unit tests for PersistentConnection using a real test-DB connection."""

    def test_get_returns_open_connection(self, real_pconn: PersistentConnection) -> None:
        conn = real_pconn.get()
        assert conn.closed == 0, "connection should be open"

    def test_get_reuses_same_object(self, real_pconn: PersistentConnection) -> None:
        """Same object is returned on successive get() calls (DB-08 core guarantee)."""
        first = real_pconn.get()
        second = real_pconn.get()
        assert first is second, "PersistentConnection must reuse the same connection object"

    def test_get_reconnects_after_close(self, real_pconn: PersistentConnection) -> None:
        """If the connection is closed externally, get() opens a fresh one."""
        original = real_pconn.get()
        original.close()  # simulate a broken pipe / server restart
        assert original.closed != 0, "sanity: connection should now be closed"

        fresh = real_pconn.get()
        assert fresh.closed == 0, "get() must return an open connection after close"
        # It must be a *different* object (the old one is closed)
        assert fresh is not original

    def test_reconnect_replaces_connection(self, real_pconn: PersistentConnection) -> None:
        """reconnect() opens a fresh connection and replaces the old one."""
        old = real_pconn.get()
        real_pconn.reconnect()
        new = real_pconn.get()
        assert new.closed == 0
        assert new is not old

    def test_close_is_idempotent(self, real_pconn: PersistentConnection) -> None:
        """Calling close() twice must not raise."""
        real_pconn.get()  # open it
        real_pconn.close()
        real_pconn.close()  # second close should be a safe no-op

    def test_connection_is_autocommit(self, real_pconn: PersistentConnection) -> None:
        """DB-08: the persistent connection must always be autocommit=True."""
        conn = real_pconn.get()
        assert conn.autocommit is True


# ---------------------------------------------------------------------------
# DB-08 — connection reuse through the MCP tool layer
# ---------------------------------------------------------------------------


class TestConnectionReuseViaTool:
    """Verify that repeated tool calls share the same underlying psycopg connection."""

    def test_brain_status_reuses_connection(
        self, mcp_state_with_pconn: mcp_server._State
    ) -> None:
        """Two brain_status() calls within one session must use the same connection."""
        pconn = mcp_state_with_pconn.db_conn
        assert pconn is not None

        # Capture the connection object identity before and after two tool calls.
        mcp_server.brain_status()
        conn_after_first = pconn._conn

        mcp_server.brain_status()
        conn_after_second = pconn._conn

        assert conn_after_first is not None
        assert conn_after_first is conn_after_second, (
            "PersistentConnection._conn must be the same object across tool calls"
        )


# ---------------------------------------------------------------------------
# DB-08 — _wrap_db_error reconnect logic
# ---------------------------------------------------------------------------


class TestWrapDbErrorReconnect:
    """Unit tests for the reconnect side-effect inside _wrap_db_error."""

    def _make_mock_pconn(self) -> MagicMock:
        return MagicMock(spec=PersistentConnection)

    def test_reconnect_called_on_operational_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OperationalError must trigger a reconnect attempt."""
        mock_pconn = self._make_mock_pconn()
        state = mcp_server._State(
            cfg=Config(database_url=TEST_DATABASE_URL),
            embedder=_CountingEmbedder(),
            db_conn=mock_pconn,
        )
        monkeypatch.setattr(mcp_server, "_state", state)

        exc = psycopg.OperationalError("simulated broken pipe")
        mcp_server._wrap_db_error(exc)

        mock_pconn.reconnect.assert_called_once()

    def test_no_reconnect_on_integrity_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-OperationalError subclasses must NOT trigger a reconnect."""
        mock_pconn = self._make_mock_pconn()
        state = mcp_server._State(
            cfg=Config(database_url=TEST_DATABASE_URL),
            embedder=_CountingEmbedder(),
            db_conn=mock_pconn,
        )
        monkeypatch.setattr(mcp_server, "_state", state)

        exc = psycopg.errors.UniqueViolation("duplicate key")
        mcp_server._wrap_db_error(exc)

        mock_pconn.reconnect.assert_not_called()

    def test_reconnect_failure_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed reconnect must be logged at ERROR and NOT re-raised."""
        mock_pconn = self._make_mock_pconn()
        mock_pconn.reconnect.side_effect = psycopg.OperationalError("still down")
        state = mcp_server._State(
            cfg=Config(database_url=TEST_DATABASE_URL),
            embedder=_CountingEmbedder(),
            db_conn=mock_pconn,
        )
        monkeypatch.setattr(mcp_server, "_state", state)

        exc = psycopg.OperationalError("broken pipe")
        with caplog.at_level(logging.ERROR, logger="brain.mcp_server"):
            result = mcp_server._wrap_db_error(exc)  # must not raise

        assert result is not None, "_wrap_db_error must return an MCPError"
        assert any(
            "reconnect failed" in r.message for r in caplog.records
        ), "failed reconnect must be logged at ERROR"

    def test_no_reconnect_when_db_conn_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When db_conn is None (test/fallback path) _wrap_db_error must not blow up."""
        state = mcp_server._State(
            cfg=Config(database_url=TEST_DATABASE_URL),
            embedder=_CountingEmbedder(),
            db_conn=None,
        )
        monkeypatch.setattr(mcp_server, "_state", state)

        exc = psycopg.OperationalError("broken pipe")
        result = mcp_server._wrap_db_error(exc)  # must not raise
        assert result is not None


# ---------------------------------------------------------------------------
# F7 — _warmup_embed unit tests
# ---------------------------------------------------------------------------


class TestWarmupEmbed:
    """Direct unit tests for _warmup_embed() — no server startup needed."""

    def test_fires_at_least_one_embed_call(self) -> None:
        """On a healthy embedder, at least one embed() call must be made."""
        embedder = _CountingEmbedder()
        mcp_server._warmup_embed(embedder)
        assert embedder.embed_calls >= 1

    def test_ollama_cold_does_not_raise(
        self, caplog: pytest.LogCaptureFixture, mocker: Any
    ) -> None:
        """OllamaEmbedError (both attempts) must be swallowed + WARN logged."""
        mocker.patch.object(mcp_server, "_WARMUP_RETRY_DELAY_SECONDS", 0)
        embedder = _AlwaysFailEmbedder()

        with caplog.at_level(logging.WARNING, logger="brain.mcp_server"):
            mcp_server._warmup_embed(embedder)  # must NOT raise

        assert embedder.embed_calls == 2, "should attempt twice (initial + retry)"
        assert any(
            "warmup embed failed" in r.message for r in caplog.records
        ), "failure must be logged at WARNING"

    def test_retry_succeeds_on_second_attempt(self, mocker: Any) -> None:
        """First attempt raises OllamaEmbedError; second succeeds — no warning."""
        mocker.patch.object(mcp_server, "_WARMUP_RETRY_DELAY_SECONDS", 0)
        call_count = 0

        class _OnceFailEmbedder:
            dim = 4096

            def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise OllamaEmbedError("first-call cold start")
                return [[0.0] * self.dim for _ in texts]

            def count_tokens(self, text: str) -> int:
                return 1

        mcp_server._warmup_embed(_OnceFailEmbedder())
        assert call_count == 2
