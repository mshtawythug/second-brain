"""MCP ``brain_tags_list`` and ``brain_tags_suggest_merges`` (RM-21): read-only, lens-filtered.

Pinned here: both tools are advertised; they write nothing (RM-21.D1: a corpus-wide rewrite is an
owner action at the terminal, so no MCP rename or merge exists); the F6 lens is in the predicate,
so a tag found only on confidential documents is not named and counts cover only the visible
tier unless ``include_confidential``; and ``limit`` is bounded. All data is synthetic.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from brain import mcp_server
from brain.config import Config
from brain.mcp_compat import MCPError
from brain.mcp_tools import tags as tags_tool

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the data fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _doc(
    conn: psycopg.Connection[Any], title: str, tags: list[str], sensitivity: str = "normal"
) -> None:
    conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, tags, sensitivity) "
        "VALUES (%s, %s, %s, 'note', %s, %s)",
        (title, f"body {title}", f"hash-{title}", tags, sensitivity),
    )


def _snapshot(conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    docs = conn.execute(
        "SELECT id, tags, updated_at FROM documents ORDER BY id"
    ).fetchall()
    watches = conn.execute("SELECT id, filters FROM saved_searches ORDER BY id").fetchall()
    return [*docs, *watches]


@pytest.fixture
def corpus(test_db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    _doc(test_db, "a", ["widget", "roadmap", "planning"])
    _doc(test_db, "b", ["widget", "roadmap", "planning"])
    _doc(test_db, "c", ["widgets", "roadmap", "planning"])
    _doc(test_db, "secret-1", ["covert", "covertly", "widget"], sensitivity="confidential")
    _doc(test_db, "secret-2", ["covert", "covertly"], sensitivity="confidential")
    test_db.execute(
        "INSERT INTO saved_searches (name, query, filters) VALUES ('w', 'q', %s::jsonb)",
        ('{"tag": "widget"}',),
    )
    return test_db


def test_both_tools_are_advertised_and_no_tag_write_tool_exists() -> None:
    names = {tool.name for tool in asyncio.run(mcp_server.mcp_app.list_tools())}

    assert {"brain_tags_list", "brain_tags_suggest_merges"} <= names
    assert not {n for n in names if n.startswith("brain_tags_") and n not in {
        "brain_tags_list", "brain_tags_suggest_merges"
    }}


def test_list_excludes_confidential_by_default_in_the_counts_and_the_names(
    mcp_state: mcp_server._State, corpus: psycopg.Connection[Any]
) -> None:
    default = tags_tool.brain_tags_list()
    both = tags_tool.brain_tags_list(include_confidential=True)

    assert default == {
        "tags": [
            {"tag": "planning", "count": 3},
            {"tag": "roadmap", "count": 3},
            {"tag": "widget", "count": 2},
            {"tag": "widgets", "count": 1},
        ],
        "truncated": False,
    }
    assert {"tag": "widget", "count": 3} in both["tags"]
    assert {"tag": "covert", "count": 2} in both["tags"]


def test_list_limit_truncates_and_is_bounded(
    mcp_state: mcp_server._State, corpus: psycopg.Connection[Any]
) -> None:
    capped = tags_tool.brain_tags_list(limit=2)

    assert [t["tag"] for t in capped["tags"]] == ["planning", "roadmap"]
    assert capped["truncated"] is True
    for bad in (0, tags_tool.LIST_LIMIT_CEILING + 1):
        with pytest.raises(MCPError) as excinfo:
            tags_tool.brain_tags_list(limit=bad)
        assert excinfo.value.error.code == INVALID_PARAMS  # type: ignore[attr-defined]


def test_suggest_merges_reports_evidence_and_never_names_a_confidential_only_tag(
    mcp_state: mcp_server._State, corpus: psycopg.Connection[Any]
) -> None:
    default = tags_tool.brain_tags_suggest_merges()
    both = tags_tool.brain_tags_suggest_merges(include_confidential=True)

    pairs = {(s["keep"], s["merge"]): s for s in default["suggestions"]}
    plural = pairs[("widget", "widgets")]
    assert "singular-plural" in plural["reasons"]
    assert (plural["keep_count"], plural["merge_count"]) == (2, 1)
    assert pairs[("roadmap", "planning")]["reasons"] == ["co-occurrence"]
    named = {name for s in default["suggestions"] for name in (s["keep"], s["merge"])}
    assert not any("covert" in name for name in named)
    assert any(s["keep"] == "covert" for s in both["suggestions"])


def test_suggest_merges_limit_is_bounded(mcp_state: mcp_server._State) -> None:
    for bad in (0, tags_tool.SUGGEST_LIMIT_CEILING + 1):
        with pytest.raises(MCPError) as excinfo:
            tags_tool.brain_tags_suggest_merges(limit=bad)
        assert excinfo.value.error.code == INVALID_PARAMS  # type: ignore[attr-defined]


def test_neither_tool_writes(
    mcp_state: mcp_server._State, corpus: psycopg.Connection[Any]
) -> None:
    before = _snapshot(corpus)

    tags_tool.brain_tags_list(include_confidential=True)
    tags_tool.brain_tags_suggest_merges(include_confidential=True)

    assert _snapshot(corpus) == before


def test_a_database_failure_is_a_class_only_internal_error(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_detail = "synthetic-detail-4b2d"

    @contextmanager
    def failing_connect(url: str) -> Iterator[psycopg.Connection[Any]]:
        raise psycopg.OperationalError(secret_detail)
        yield  # pragma: no cover - a generator, never reached

    monkeypatch.setattr(mcp_server, "connect", failing_connect)

    for call in (tags_tool.brain_tags_list, tags_tool.brain_tags_suggest_merges):
        with pytest.raises(MCPError) as excinfo:
            call()
        assert excinfo.value.error.code == INTERNAL_ERROR  # type: ignore[attr-defined]
        assert "OperationalError" in str(excinfo.value)
        assert secret_detail not in str(excinfo.value)
