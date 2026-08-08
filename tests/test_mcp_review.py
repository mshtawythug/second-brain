"""MCP tests for ``brain_review_weekly`` (Plan 10).

Installs a fresh ``_State`` pointed at the test DB with a tmp vault, then drives
the tool directly. ``enricher`` stays ``None`` (no Ollama round-trip).
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.mcp_compat import MCPError

from .conftest import TEST_DATABASE_URL


@pytest.fixture
def review_state(
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


def _interact_now(conn: psycopg.Connection, doc_id: str) -> None:
    conn.execute(
        "INSERT INTO interactions (document_id, action, source) "
        "VALUES (%s, 'opened', 'cli')",
        (doc_id,),
    )


def test_brain_review_weekly_no_emit_returns_sections(
    review_state: mcp_server._State,  # noqa: ARG001 — installs state
    test_db: psycopg.Connection,
    seed_doc: Callable[..., str],
) -> None:
    doc = seed_doc(title="Doc", content="body", tags=["topic-alpha"])
    _interact_now(test_db, doc)

    payload = mcp_server.brain_review_weekly(no_graph=True, emit=False)

    assert "week" in payload
    assert "sections" in payload
    assert payload["vault_path"].startswith("reviews/")
    assert payload["graph_used"] is False


def test_brain_review_weekly_emit_writes_page(
    review_state: mcp_server._State,
    test_db: psycopg.Connection,
    seed_doc: Callable[..., str],
) -> None:
    doc = seed_doc(title="Doc", content="body", tags=["topic-alpha"])
    _interact_now(test_db, doc)

    payload = mcp_server.brain_review_weekly(
        week="2026-W23", no_graph=True, emit=True
    )

    assert payload["week"] == "2026-W23"
    written = review_state.cfg.vault_path / "reviews" / "2026-W23.md"
    assert written.is_file()
    assert written.read_text(encoding="utf-8").startswith("---\n")


def test_brain_review_weekly_bad_week_raises_mcp_error(
    review_state: mcp_server._State,  # noqa: ARG001 — installs state
) -> None:
    with pytest.raises(MCPError):
        mcp_server.brain_review_weekly(week="nope", no_graph=True, emit=False)
