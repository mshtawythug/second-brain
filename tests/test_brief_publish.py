"""``brain.brief_publish.build_brief``: the two-audience brief, called directly (RM-15 step 1).

The CLI tests (``tests/test_brief.py``, ``tests/test_cli_vault_emit_confidential.py``) prove the
extraction is behaviour-identical through ``brain brief``; these call ``build_brief`` with an
injected connection opener, the seam RM-16 and RM-19 build on. The LLM leg is a standard test
double on ``brain.chat.chat_json``. All data is synthetic.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import chat
from brain.brief_publish import build_brief
from brain.config import Config
from brain.db import connect
from brain.errors import OllamaUnavailable

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
ON_DATE = date(2026, 6, 9)


def _cfg(vault: Path) -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        vault_path=vault,
        brief_since_hours=24,
        brief_todo_since_days=7,
        brief_capture_limit=20,
        brief_pin_limit=10,
    )


@pytest.fixture(autouse=True)
def _no_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise OllamaUnavailable("down")

    monkeypatch.setattr(chat, "chat_json", _boom)


def _counting_connect(
    opened: list[str],
) -> Callable[[str], AbstractContextManager[psycopg.Connection[Any]]]:
    @contextmanager
    def _open(url: str) -> Iterator[psycopg.Connection[Any]]:
        opened.append(url)
        with connect(url) as conn:
            yield conn

    return _open


def test_build_brief_without_wiki_writes_nothing_and_uses_the_injected_connect(
    test_db: psycopg.Connection, seed_doc: Callable[..., str], tmp_path: Path
) -> None:
    seed_doc(title="Publish smoke", content="publish smoke body")
    opened: list[str] = []

    data, written = build_brief(
        _cfg(tmp_path),
        on_date=ON_DATE,
        since_hours=24,
        todo_since_days=7,
        enrich=False,
        wiki=False,
        connect=_counting_connect(opened),
    )

    assert written is None
    assert opened and set(opened) == {TEST_DATABASE_URL}
    assert [doc.title for doc in data.captures] == ["Publish smoke"]
    assert data.date == ON_DATE
    assert not (tmp_path / "daily").exists()


def test_build_brief_with_wiki_writes_the_gated_page_and_returns_the_permissive_payload(
    test_db: psycopg.Connection, seed_doc: Callable[..., str], tmp_path: Path
) -> None:
    normal = seed_doc(title="Open memo", content="open memo body")
    secret = seed_doc(title="Sealed memo", content="sealed memo body")
    test_db.execute(
        "UPDATE documents SET sensitivity = 'confidential' WHERE id = %s", (secret,)
    )

    data, written = build_brief(
        _cfg(tmp_path),
        on_date=ON_DATE,
        since_hours=24,
        todo_since_days=7,
        enrich=False,
        wiki=True,
    )

    assert {doc.id for doc in data.captures} == {normal, secret}
    assert written == tmp_path / "daily" / "2026" / "2026-06-09-brief.md"
    page = written.read_text(encoding="utf-8")
    assert "Open memo" in page
    assert "Sealed memo" not in page


def test_build_brief_enrich_off_never_calls_the_model(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("brain.brief.suggest_next_steps", lambda b, c: calls.append(b) or [])

    data, _ = build_brief(
        _cfg(tmp_path),
        on_date=ON_DATE,
        since_hours=24,
        todo_since_days=7,
        enrich=False,
        wiki=True,
    )

    assert calls == []
    assert data.suggestions == []
