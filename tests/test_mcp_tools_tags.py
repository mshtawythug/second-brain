"""MCP ``brain_tags_list``, ``brain_tags_suggest_merges`` (RM-21) and ``brain_tags_summarize``
(RM-55): read-only, lens-filtered.

Pinned here: both tools are advertised; they write nothing (RM-21.D1: a corpus-wide rewrite is an
owner action at the terminal, so no MCP rename or merge exists); the F6 lens is in the predicate,
so a tag found only on confidential documents is not named and counts cover only the visible
tier unless ``include_confidential``; and ``limit`` is bounded. ``brain_tags_summarize`` lenses
its INPUT: the assertions read the prompt the fake chat was sent, not only the return value. All
data is synthetic.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

from brain import chat, mcp_server
from brain.config import Config
from brain.errors import (
    ChatOutputTruncated,
    EnrichmentError,
    OllamaContextOverflow,
    OllamaUnavailable,
)
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


_TAG_TOOLS = {"brain_tags_list", "brain_tags_suggest_merges", "brain_tags_summarize"}


def test_the_three_tools_are_advertised_and_no_tag_write_tool_exists() -> None:
    names = {tool.name for tool in asyncio.run(mcp_server.mcp_app.list_tools())}

    assert names >= _TAG_TOOLS
    assert not {n for n in names if n.startswith("brain_tags_") and n not in _TAG_TOOLS}


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


def test_read_only_is_set_on_the_transaction_and_postgres_refuses_a_write(
    mcp_state: mcp_server._State, corpus: psycopg.Connection[Any]
) -> None:
    """Pins RM-21.D1 at the database: the tools' session is a READ ONLY transaction.

    Driven through the REAL context ``mcp_server`` bound at import (no stand-in), so this is the
    connection a tool call gets.
    """
    ctx = tags_tool._CONTEXT.get()
    before = _snapshot(corpus)

    with tags_tool._read_only(ctx, mcp_state) as conn:
        mode = conn.execute("SHOW transaction_read_only").fetchone()
    with pytest.raises(MCPError) as excinfo, tags_tool._read_only(ctx, mcp_state) as conn:
        conn.execute("UPDATE documents SET tags = tags || ARRAY['written']")

    assert mode is not None and mode[0] == "on"
    assert excinfo.value.error.code == INTERNAL_ERROR  # type: ignore[attr-defined]
    assert "ReadOnlySqlTransaction" in str(excinfo.value)
    assert _snapshot(corpus) == before


# --- brain_tags_summarize (RM-55) -------------------------------------------------------------

_SECRET = "SYNTHETIC-SECRET-BODY-TEXT"


class _FakeChat:
    """Stands in for ``brain.chat.chat_json``: records prompts and kwargs, answers or raises."""

    def __init__(self, reply: object = "", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self.on_call: list[Any] = []

    def __call__(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        for check in self.on_call:
            check()
        if self.error is not None:
            raise self.error
        return {"summary": self.reply}


def _use(monkeypatch: pytest.MonkeyPatch, fake: _FakeChat) -> _FakeChat:
    monkeypatch.setattr(chat, "chat_json", fake)
    return fake


def _summarized(
    conn: psycopg.Connection[Any],
    title: str,
    summary: str,
    tags: list[str],
    sensitivity: str = "normal",
) -> None:
    conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, tags, summary,"
        " sensitivity) VALUES (%s, %s, %s, 'note', %s, %s, %s)",
        (title, f"body {title}", f"hash-{title}", tags, summary, sensitivity),
    )


@pytest.fixture
def summarized(test_db: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    _summarized(test_db, "Open plan", "zephyr open routine", ["nebula"])
    _summarized(test_db, "Closed plan", "orbit closed routine", ["nebula"], "confidential")
    _summarized(test_db, "Closed only", "hidden routine", ["covert"], "confidential")
    return test_db


def test_summarize_excludes_confidential_from_the_prompt_by_default(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _use(monkeypatch, _FakeChat("Routine [1]."))

    result = tags_tool.brain_tags_summarize("nebula")

    (prompt,) = fake.prompts
    assert "zephyr open routine" in prompt
    assert "orbit closed routine" not in prompt
    assert "Closed plan" not in prompt
    assert result["considered"] == 1
    assert [c["title"] for c in result["citations"]] == ["Open plan"]


def test_summarize_include_confidential_reaches_both_tiers(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _use(monkeypatch, _FakeChat("Routine [1][2]."))

    result = tags_tool.brain_tags_summarize("nebula", include_confidential=True)

    assert "orbit closed routine" in fake.prompts[0]
    assert "zephyr open routine" in fake.prompts[0]
    assert result["considered"] == 2


def test_hide_mode_default_call_never_sends_a_confidential_summary(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP without --include-confidential: the gate refuses the argument (see
    ``tests/test_mcp_tags_summarize_gate.py``), so the call that reaches the tool is this one."""
    hidden = dataclasses.replace(mcp_state, hide_confidential=True)
    monkeypatch.setattr(mcp_server, "_state", hidden)
    fake = _use(monkeypatch, _FakeChat("Routine [1]."))

    tags_tool.brain_tags_summarize("nebula")

    assert "orbit closed routine" not in fake.prompts[0]
    assert "zephyr open routine" in fake.prompts[0]


def test_confidential_only_tag_answers_like_an_unknown_tag(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _use(monkeypatch, _FakeChat("unused [1]."))

    confidential_only = tags_tool.brain_tags_summarize("covert")
    unknown = tags_tool.brain_tags_summarize("never-used")

    assert fake.prompts == []
    assert {**confidential_only, "tag": "x"} == {**unknown, "tag": "x"} == {
        "tag": "x", "summary": "", "citations": [], "considered": 0, "dropped": 0
    }


def test_summarize_cites_only_in_range_documents(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _FakeChat("Seen [1], invented [2] and [9]."))

    result = tags_tool.brain_tags_summarize("nebula")

    assert result["summary"] == "Seen [1], invented and."
    assert [c["ref"] for c in result["citations"]] == [1]


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (OllamaUnavailable(_SECRET), "Ollama is not available"),
        (OllamaContextOverflow(_SECRET), "did not fit the chat model's context window"),
        (ChatOutputTruncated(_SECRET, partial=_SECRET, num_predict=512), "cut off"),
        (EnrichmentError(_SECRET), "tag summary failed (EnrichmentError)"),
    ],
)
def test_summarize_failures_are_internal_error_with_fixed_sentence(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch, error: Exception, fragment: str,
) -> None:
    _use(monkeypatch, _FakeChat(error=error))

    with pytest.raises(MCPError) as excinfo:
        tags_tool.brain_tags_summarize("nebula")

    assert excinfo.value.error.code == INTERNAL_ERROR  # type: ignore[attr-defined]
    assert fragment in str(excinfo.value)
    assert _SECRET not in str(excinfo.value)


def test_summarize_limit_is_bounded(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _use(monkeypatch, _FakeChat("Routine [1]."))
    for bad in (0, 51):
        with pytest.raises(MCPError) as excinfo:
            tags_tool.brain_tags_summarize("nebula", limit=bad)
        assert excinfo.value.error.code == INVALID_PARAMS  # type: ignore[attr-defined]
    assert fake.prompts == []
    for good in (1, 50):
        assert tags_tool.brain_tags_summarize("nebula", limit=good)["considered"] == 1


@pytest.mark.parametrize("tag", ["", "---", "   "])
def test_summarize_empty_tag_is_invalid_params(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch, tag: str
) -> None:
    fake = _use(monkeypatch, _FakeChat("unused"))

    with pytest.raises(MCPError) as excinfo:
        tags_tool.brain_tags_summarize(tag)

    assert excinfo.value.error.code == INVALID_PARAMS  # type: ignore[attr-defined]
    assert fake.prompts == []


def test_summarize_writes_nothing(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, _FakeChat("Routine [1]."))
    before = _snapshot(summarized)
    interactions = summarized.execute("SELECT count(*) FROM interactions").fetchone()

    tags_tool.brain_tags_summarize("nebula", include_confidential=True)

    assert _snapshot(summarized) == before
    assert summarized.execute("SELECT count(*) FROM interactions").fetchone() == interactions


@pytest.mark.parametrize(("call_timeout_s", "expected"), [(5.0, 5.0), (1000.0, 60.0)])
def test_summarize_timeout_is_capped_by_call_timeout(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch, call_timeout_s: float, expected: float,
) -> None:
    capped = dataclasses.replace(mcp_state, call_timeout_s=call_timeout_s)
    monkeypatch.setattr(mcp_server, "_state", capped)
    fake = _use(monkeypatch, _FakeChat("Routine [1]."))

    tags_tool.brain_tags_summarize("nebula")

    assert fake.kwargs[0]["timeout"] == expected


def test_summarize_without_a_call_timeout_keeps_the_chat_default(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _use(monkeypatch, _FakeChat("Routine [1]."))

    tags_tool.brain_tags_summarize("nebula")

    assert "timeout" not in fake.kwargs[0]


def test_summarize_closes_its_connection_before_the_chat_runs(
    mcp_state: mcp_server._State, summarized: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[psycopg.Connection[Any]] = []
    real_connect = mcp_server.connect

    @contextmanager
    def recording_connect(url: str) -> Iterator[psycopg.Connection[Any]]:
        with real_connect(url) as conn:
            opened.append(conn)
            yield conn

    monkeypatch.setattr(mcp_server, "connect", recording_connect)
    fake = _use(monkeypatch, _FakeChat("Routine [1]."))
    seen: list[bool] = []
    fake.on_call.append(lambda: seen.append(bool(opened) and all(c.closed for c in opened)))

    tags_tool.brain_tags_summarize("nebula")

    assert seen == [True]


def test_summarize_db_failure_is_class_only(
    mcp_state: mcp_server._State, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def failing_connect(url: str) -> Iterator[psycopg.Connection[Any]]:
        raise psycopg.OperationalError(_SECRET)
        yield  # pragma: no cover - a generator, never reached

    monkeypatch.setattr(mcp_server, "connect", failing_connect)
    fake = _use(monkeypatch, _FakeChat("unused"))

    with pytest.raises(MCPError) as excinfo:
        tags_tool.brain_tags_summarize("nebula")

    assert excinfo.value.error.code == INTERNAL_ERROR  # type: ignore[attr-defined]
    assert "OperationalError" in str(excinfo.value)
    assert _SECRET not in str(excinfo.value)
    assert fake.prompts == []
