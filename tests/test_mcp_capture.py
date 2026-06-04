"""Tests for the ``brain_capture`` MCP tool (Plan 09 Phase 3).

Each test installs a fresh ``_State`` (real test-DB Config sandboxed to a
``tmp_path`` vault + fake embedder) and calls the tool function directly,
mirroring ``tests/test_mcp_server.py``. ``enricher``/``graph_syncer`` are left
``None`` so the suite never depends on a live Ollama. All content is synthetic.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
def capture_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    """Install a server state pointing at the test DB + a sandboxed vault.

    ``vault_path`` is forced to ``tmp_path`` so the capture's ``_ingested``
    mirror write never touches the real vault. ``enricher``/``graph_syncer``
    stay ``None`` (no Ollama / graph round-trip)."""
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _doc_tags(doc_id: str) -> list[str]:
    """Read the current tag list for a document directly from Postgres."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT tags FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return list(row[0] or [])


def _doc_title(doc_id: str) -> str:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT title FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_brain_capture_creates_inbox_document(
    capture_state: mcp_server._State,  # noqa: ARG001 — fixture installs state
) -> None:
    """A non-empty capture creates a document tagged ``inbox`` → status ingested."""
    payload = mcp_server.brain_capture(
        content="a quick thought about project-ko follow-ups",
    )
    assert payload["status"] == "ingested"
    assert payload["document_id"] is not None
    assert _doc_tags(payload["document_id"]) == ["inbox"]


def test_brain_capture_merges_extra_tags_with_inbox(
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Caller tags are normalized + unioned with the always-on ``inbox`` tag."""
    payload = mcp_server.brain_capture(
        content="capture body with extra routing tags",
        tags=["Interview", "inbox"],  # mixed case + explicit dup of inbox
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    assert sorted(_doc_tags(doc_id)) == ["inbox", "interview"]


def test_brain_capture_recapture_same_content_skips(
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Re-capturing identical content is a content-hash no-op → status skipped."""
    body = "a repeated capture body for the dedup path"
    first = mcp_server.brain_capture(content=body)
    second = mcp_server.brain_capture(content=body)
    assert first["status"] == "ingested"
    assert second["status"] == "skipped"
    assert second["document_id"] == first["document_id"]
    # The dedup'd doc still carries inbox exactly once.
    assert _doc_tags(first["document_id"]) == ["inbox"]


@pytest.mark.parametrize("empty", ["", "   ", "\n\n", "\t  \n"])
def test_brain_capture_empty_content_errors(
    empty: str,
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Empty / whitespace-only content is rejected as an MCP INVALID_PARAMS error."""
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_capture(content=empty)
    assert "content is empty" in exc_info.value.error.message


def test_brain_capture_auto_titles_when_title_blank(
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A blank title falls back to the deterministic date-stamped auto-title."""
    payload = mcp_server.brain_capture(
        title="   ",
        content="remember to draft the q3 planning note",
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    title = _doc_title(doc_id)
    # Shape: ``<iso date>-capture-<slug>`` (see brain.capture.make_capture_title).
    assert "-capture-" in title
    assert title.endswith("remember-to-draft-the-q3-planning")


def test_brain_capture_uses_explicit_title_verbatim(
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A non-blank title is used as-is (stripped), not auto-generated."""
    payload = mcp_server.brain_capture(
        title="My Deliberate Title",
        content="body under an explicit title",
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    assert _doc_title(doc_id) == "My Deliberate Title"


def test_brain_capture_writes_vault_mirror(
    capture_state: mcp_server._State,
) -> None:
    """The capture materializes an ``_ingested/manual`` mirror under the vault."""
    payload = mcp_server.brain_capture(
        content="mirror this capture via the MCP tool",
    )
    assert payload["status"] == "ingested"
    mirror_dir = capture_state.cfg.vault_path / "_ingested" / "manual"
    assert mirror_dir.is_dir(), f"missing mirror dir: {mirror_dir}"
    mirrors = list(mirror_dir.glob("*.md"))
    assert len(mirrors) == 1, f"expected one mirror file, got {mirrors}"
    assert "mirror this capture via the MCP tool" in mirrors[0].read_text(
        encoding="utf-8"
    )


class _BoomConnect:
    """Stub mimicking ``connect()``: entering the context raises immediately."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc or psycopg.OperationalError("simulated outage")

    def __call__(self, _url: str) -> _BoomConnect:
        return self

    def __enter__(self) -> Any:
        raise self._exc

    def __exit__(self, *_: object) -> None:
        return None


def test_brain_capture_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A Postgres failure surfaces as McpError, never a raw psycopg.Error."""
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_capture(content="content that never reaches the db")
    assert "database error" in exc_info.value.error.message
