"""MCP tests for ``brain_brief`` (Plan 01).

Installs a fresh ``_State`` pointed at the test DB; patches
``brain.chat.chat_json`` so the suggestion leg never contacts Ollama.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

from brain import chat, mcp_server
from brain.brief import BriefData
from brain.config import Config
from brain.errors import OllamaUnavailable

from .conftest import TEST_DATABASE_URL


def _raise_unavailable(*_a: object, **_k: object) -> dict[str, object]:
    raise OllamaUnavailable("down")


@pytest.fixture
def brief_state(
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
    # Treat Ollama as down so the tool returns empty suggestions deterministically.
    monkeypatch.setattr(chat, "chat_json", _raise_unavailable)
    yield state


def test_brain_brief_returns_sections(
    brief_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    seed_doc: Callable[..., str],
) -> None:
    seed_doc(title="Brief doc", content="brief body")
    payload = mcp_server.brain_brief()
    assert "date" in payload
    assert "captures" in payload
    assert len(payload["captures"]) >= 1
    # Ollama is down → no suggestions, but the brief still renders.
    assert payload["suggestions"] == []


def test_brain_brief_no_enrich_skips_suggestions(
    brief_state: mcp_server._State,  # noqa: ARG001 — installs state
) -> None:
    payload = mcp_server.brain_brief(no_enrich=True)
    assert payload["suggestions"] == []


def test_brain_brief_resolves_windows_from_config(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    # Omitted MCP params must resolve from cfg (not the old hard-coded 24/7),
    # so config overrides propagate — mirroring `brain brief --json`.
    captured: dict[str, int] = {}

    def _spy(
        conn: object,
        cfg: object,
        *,
        since_hours: int,
        todo_since_days: int,
        on_date: object,
    ) -> object:
        captured["since_hours"] = since_hours
        captured["todo_since_days"] = todo_since_days
        return BriefData(
            date=on_date,  # type: ignore[arg-type]
            captures=[],
            open_todos=[],
            pinned=[],
            suggestions=[],
        )

    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            vault_path=tmp_path,
            brief_since_hours=72,
            brief_todo_since_days=30,
        ),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    monkeypatch.setattr("brain.brief.assemble_brief", _spy)

    mcp_server.brain_brief(no_enrich=True)
    assert captured == {"since_hours": 72, "todo_since_days": 30}
