"""``brain tags summarize`` through the real CLI, with a fake chat (RM-55).

Real-DB tests on the 5434 test database. The chat is replaced at the ``brain.cli._build_chat``
patch point ``brain ask``'s tests use, so no Ollama runs. Every tag, title and summary is
synthetic.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli_tags
from brain.cli import app
from brain.errors import (
    ChatOutputTruncated,
    EnrichmentError,
    OllamaContextOverflow,
    OllamaUnavailable,
)

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
_SECRET = "SYNTHETIC-SECRET-BODY-TEXT"

runner = CliRunner()


class _FakeChat:
    """Records each prompt; answers with ``reply`` or raises ``error``."""

    def __init__(self, reply: object = "", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.prompts: list[str] = []
        self.on_call: list[Any] = []

    def __call__(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.prompts.append(prompt)
        for check in self.on_call:
            check()
        if self.error is not None:
            raise self.error
        return {"summary": self.reply}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, fake_embedder: object, tmp_path: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)


def _use(monkeypatch: pytest.MonkeyPatch, chat: _FakeChat) -> _FakeChat:
    monkeypatch.setattr("brain.cli._build_chat", lambda cfg: chat)
    return chat


def _doc(
    conn: psycopg.Connection[Any],
    title: str,
    *,
    summary: str | None = None,
    tags: tuple[str, ...] = ("nebula-notes",),
    sensitivity: str = "normal",
    day: int = 1,
) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, tags, summary,"
        " sensitivity, sent_at) VALUES (%s, %s, %s, 'note', %s, %s, %s, %s) RETURNING id::text",
        (title, f"body {title}", f"hash-{title}", list(tags), summary, sensitivity,
         datetime(2026, 1, day, tzinfo=UTC)),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _run(*args: str) -> Any:
    return runner.invoke(app, ["tags", "summarize", *args])


@pytest.mark.usefixtures("env")
def test_prints_synthesis_and_only_cited_sources(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _doc(test_db, "Orbit plan", summary="zephyr routine", day=2)
    _doc(test_db, "Older note", summary="earlier routine", day=1)
    _use(monkeypatch, _FakeChat("Routines recur [1]."))

    result = _run("nebula-notes")

    assert result.exit_code == 0, result.output
    assert "Routines recur [1]." in result.stdout
    assert f"[1]  Orbit plan ({first[:8]}, 2026-01-02)" in result.stdout
    assert "Older note" not in result.stdout
    assert "2 document(s) considered" in result.stdout


@pytest.mark.usefixtures("env")
def test_json_shape(test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id = _doc(test_db, "Orbit plan", summary="zephyr routine")
    _use(monkeypatch, _FakeChat("Routines [1] and [5]."))

    result = _run("nebula-notes", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "tag": "nebula-notes",
        "summary": "Routines [1] and.",
        "citations": [
            {"ref": 1, "document_id": doc_id, "title": "Orbit plan", "date": "2026-01-01"}
        ],
        "considered": 1,
        "dropped": 0,
    }


@pytest.mark.usefixtures("env")
def test_cli_includes_confidential_documents(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _doc(test_db, "Open note", summary="open routine")
    _doc(test_db, "Closed note", summary="closed routine", sensitivity="confidential")
    chat = _use(monkeypatch, _FakeChat("Both [1][2]."))

    result = _run("nebula-notes")

    assert result.exit_code == 0, result.output
    assert "closed routine" in chat.prompts[0]
    assert "open routine" in chat.prompts[0]


@pytest.mark.usefixtures("env")
@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (OllamaUnavailable(_SECRET), "Ollama is not available"),
        (OllamaContextOverflow(_SECRET), "did not fit the chat model's context window"),
        (ChatOutputTruncated(_SECRET, partial=_SECRET, num_predict=512), "cut off"),
        (EnrichmentError(_SECRET), "tag summary failed (EnrichmentError)"),
    ],
)
def test_each_chat_failure_prints_its_sentence_and_exits_1(
    test_db: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    fragment: str,
) -> None:
    _doc(test_db, "Orbit plan", summary="zephyr routine")
    _use(monkeypatch, _FakeChat(error=error))

    result = _run("nebula-notes")

    assert result.exit_code == 1
    assert result.stderr.startswith("tags summarize: ")
    assert fragment in result.stderr
    assert _SECRET not in result.output


@pytest.mark.usefixtures("env")
def test_unknown_tag_prints_notice_exit_0_no_chat(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _doc(test_db, "Orbit plan", tags=("orbit",))
    chat = _use(monkeypatch, _FakeChat("unused [1]."))

    result = _run("nebula-notes")

    assert result.exit_code == 0
    assert "no documents tagged 'nebula-notes'" in result.stderr
    assert result.stdout == ""
    assert chat.prompts == []


@pytest.mark.usefixtures("env")
def test_unknown_tag_with_json_emits_the_empty_result(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _use(monkeypatch, _FakeChat("unused [1]."))

    result = _run("nebula-notes", "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["considered"] == 0
    assert chat.prompts == []


@pytest.mark.usefixtures("env", "test_db")
@pytest.mark.parametrize("tag", ["---", "   "])
def test_empty_tag_after_normalization_is_refused(
    monkeypatch: pytest.MonkeyPatch, tag: str
) -> None:
    chat = _use(monkeypatch, _FakeChat("unused"))

    result = _run("--", tag)

    assert result.exit_code == 1
    assert "empty once normalized" in result.stderr
    assert chat.prompts == []


@pytest.mark.usefixtures("env")
def test_rich_markup_in_reply_stays_literal(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _doc(test_db, "Orbit plan", summary="zephyr routine")
    _use(monkeypatch, _FakeChat("[bold red]loud[/bold red] [1]"))

    result = _run("nebula-notes")

    assert "[bold red]loud[/bold red] [1]" in result.stdout


@pytest.mark.usefixtures("env")
def test_escape_sequences_never_reach_stdout(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _doc(test_db, "Orbit\x1b]8;;https://example.invalid/x\x07plan", summary="zephyr")
    _use(monkeypatch, _FakeChat("Hi \x1b[2J\x1b]0;title\x07there [1]\x9b."))

    result = _run("nebula-notes")

    assert result.exit_code == 0, result.output
    for bad in ("\x1b", "\x07", "\x9b", "example.invalid"):
        assert bad not in result.output
    assert "Hi there [1]." in result.stdout


@pytest.mark.usefixtures("env", "test_db")
@pytest.mark.parametrize("limit", ["0", "51"])
def test_limit_outside_1_to_50_is_refused(monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    chat = _use(monkeypatch, _FakeChat("unused"))

    result = _run("nebula-notes", "--limit", limit)

    assert result.exit_code == 2
    assert chat.prompts == []


@pytest.mark.usefixtures("env")
def test_limit_bounds_what_reaches_the_prompt(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    for day in range(1, 6):
        _doc(test_db, f"Note {day}", day=day)
    chat = _use(monkeypatch, _FakeChat("Notes [1]."))

    result = _run("nebula-notes", "--limit", "2")

    assert result.exit_code == 0, result.output
    assert "Note 5" in chat.prompts[0]
    assert "Note 4" in chat.prompts[0]
    assert "Note 3" not in chat.prompts[0]


@pytest.mark.usefixtures("env")
def test_tag_is_normalized_before_lookup(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _doc(test_db, "Orbit plan", summary="zephyr routine")
    chat = _use(monkeypatch, _FakeChat("Seen [1]."))

    result = _run("Nebula Notes")

    assert result.exit_code == 0, result.output
    assert len(chat.prompts) == 1


@pytest.mark.usefixtures("env")
def test_the_database_connection_is_closed_before_the_chat_runs(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _doc(test_db, "Orbit plan", summary="zephyr routine")
    opened: list[psycopg.Connection[Any]] = []
    real_connect = cli_tags.connect

    @contextmanager
    def recording_connect(url: str) -> Iterator[psycopg.Connection[Any]]:
        with real_connect(url) as conn:
            opened.append(conn)
            yield conn

    monkeypatch.setattr(cli_tags, "connect", recording_connect)
    chat = _use(monkeypatch, _FakeChat("Seen [1]."))
    seen: list[bool] = []
    chat.on_call.append(lambda: seen.append(bool(opened) and all(c.closed for c in opened)))

    result = _run("nebula-notes")

    assert result.exit_code == 0, result.output
    assert seen == [True]


@pytest.mark.usefixtures("env")
def test_a_database_failure_is_class_only_and_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def failing_connect(url: str) -> Iterator[psycopg.Connection[Any]]:
        raise psycopg.OperationalError(_SECRET)
        yield  # pragma: no cover

    monkeypatch.setattr(cli_tags, "connect", failing_connect)
    chat = _use(monkeypatch, _FakeChat("unused"))

    result = _run("nebula-notes")

    assert result.exit_code == 1
    assert "tags summarize: OperationalError" in result.stderr
    assert _SECRET not in result.output
    assert chat.prompts == []


def test_summarize_is_listed_in_tags_help() -> None:
    result = runner.invoke(app, ["tags", "--help"])
    assert "summarize" in result.stdout
