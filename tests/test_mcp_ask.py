"""MCP tests for ``brain_ask`` (Plan 06, Phase 2).

Installs a fresh ``_State`` pointed at the test DB and patches
``brain.chat.chat_json`` so the plan/reflect/synthesize legs never contact a
live Ollama.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp import McpError

from brain import chat, mcp_server
from brain.config import Config
from brain.errors import OllamaUnavailable

from .conftest import TEST_DATABASE_URL


def _scripted_chat(answer: str) -> Callable[..., dict[str, Any]]:
    def _chat(
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if "sub_queries" in schema:
            return {"sub_queries": ["synthetic"]}
        if "sufficient" in schema:
            return {"sufficient": True, "follow_up_queries": []}
        return {"answer": answer}

    return _chat


@pytest.fixture
def ask_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def test_brain_ask_returns_cited_answer(
    ask_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    seed_doc: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = seed_doc(
        title="Synthetic onboarding playbook",
        content="The synthetic onboarding playbook covers paired mentorship.",
    )
    monkeypatch.setattr(chat, "chat_json", _scripted_chat("Uses mentorship [1]."))

    payload = mcp_server.brain_ask(
        question="synthetic onboarding playbook", no_loop=True
    )

    assert payload["answer"]
    assert payload["fallback_used"] is True
    assert payload["iterations_used"] == 1
    assert "session_id" in payload
    assert any(c["document_id"] == doc_id for c in payload["citations"])


def test_brain_ask_logs_interactions(
    ask_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,
    seed_doc: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = seed_doc(
        title="Synthetic onboarding playbook",
        content="The synthetic onboarding playbook covers paired mentorship.",
    )
    monkeypatch.setattr(chat, "chat_json", _scripted_chat("Uses mentorship [1]."))

    payload = mcp_server.brain_ask(
        question="synthetic onboarding playbook", no_loop=True
    )

    rows = test_db.execute(
        "SELECT action, source, session_id FROM interactions WHERE document_id = %s",
        (doc_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "opened"
    assert rows[0][1] == "mcp"
    # session_id is grouped from the AskResult (UUID hex round-trips).
    assert str(rows[0][2]).replace("-", "") == payload["session_id"]


def test_brain_ask_bad_mode_invalid_params(
    ask_state: mcp_server._State,  # noqa: ARG001 — installs state
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_ask(question="q", mode="bogus")
    assert "mode must be one of" in str(exc_info.value)


def test_brain_ask_ollama_unavailable_internal_error(
    ask_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    seed_doc: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_doc(title="Synthetic doc", content="synthetic content body here.")

    def _dead_chat(*_a: object, **_k: object) -> dict[str, Any]:
        raise OllamaUnavailable("down")

    monkeypatch.setattr(chat, "chat_json", _dead_chat)

    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_ask(question="synthetic doc", no_loop=True)
    assert "Ollama is not running" in str(exc_info.value)
