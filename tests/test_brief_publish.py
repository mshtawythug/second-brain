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
from brain.saved_search import MODE_LEXICAL, MODE_SEMANTIC, WatchFilters, WatchNote
from brain.saved_search_check import SKIP_EMBEDDER, Matcher, add_watch
from tests.saved_search_fakes import DownEmbedder, WordEmbedder, ingest

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


# ---------------------------------------------------------------------------
# Watches (RM-15): the brief checks first, lists, gates and marks
# ---------------------------------------------------------------------------


def _lexical_matcher() -> Matcher:
    def _never() -> Any:
        raise AssertionError("a lexical check must not build an embedder")

    return Matcher(embedder=_never, vector_sim_floor=0.25, min_cosine=0.45)


def _watch(conn: psycopg.Connection, name: str, query: str, **kwargs: Any) -> None:
    mode = kwargs.pop("mode", MODE_LEXICAL)
    matcher = kwargs.pop("matcher", None) or _lexical_matcher()
    add_watch(
        conn, name=name, query=query, filters=WatchFilters(**kwargs), mode=mode, matcher=matcher
    )


def _word_matcher() -> Matcher:
    words = WordEmbedder()
    return Matcher(embedder=lambda: words, vector_sim_floor=0.25, min_cosine=0.45)


def _build(vault: Path, *, wiki: bool, **kwargs: Any) -> tuple[Any, Path | None]:
    return build_brief(
        _cfg(vault),
        on_date=ON_DATE,
        since_hours=24,
        todo_since_days=7,
        enrich=kwargs.pop("enrich", False),
        wiki=wiki,
        **kwargs,
    )


def _page_watch_lines(page: str) -> list[str]:
    if "## Watch hits" not in page:
        return []
    section = page.split("## Watch hits", 1)[1].split("\n## ", 1)[0]
    return [line for line in section.splitlines() if line.startswith("- ")]


def test_no_watches_means_no_section(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    data, written = _build(tmp_path, wiki=True)

    assert data.watch_hits == [] and data.watch_notes == []
    assert data.to_dict()["watch_hits"] == [] and data.to_dict()["watch_notes"] == []
    assert written is not None and _page_watch_lines(written.read_text()) == []


def test_a_brief_checks_first_and_lists_the_new_hit(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    words = WordEmbedder()
    _watch(test_db, "zephyr", "zephyr")
    doc = ingest(test_db, words, title="Zephyr launch", content="zephyr launch plan")

    data, _ = _build(tmp_path, wiki=False)

    assert [(h.watch, h.document_id, h.notified) for h in data.watch_hits] == [
        ("zephyr", doc, False)
    ]
    unmarked = test_db.execute(
        "SELECT count(*) FROM saved_search_hits WHERE notified_at IS NULL"
    ).fetchone()[0]
    assert unmarked == 1


def test_wiki_lists_five_gated_hits_marks_all_and_a_second_run_lists_none(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    words = WordEmbedder()
    _watch(test_db, "zephyr", "zephyr")
    for i in range(7):
        ingest(test_db, words, title=f"Zephyr open {i}", content=f"zephyr open {i}")
    ingest(
        test_db, words, title="Zephyr sealed", content="zephyr sealed", sensitivity="confidential"
    )

    _, written = _build(tmp_path, wiki=True)
    assert written is not None
    first = _page_watch_lines(written.read_text())
    unnotified = test_db.execute(
        "SELECT count(*) FROM saved_search_hits WHERE notified_at IS NULL"
    ).fetchone()[0]
    _, rewritten = _build(tmp_path, wiki=True)
    assert rewritten is not None
    second = _page_watch_lines(rewritten.read_text())

    hit_lines = [line for line in first if "Zephyr open" in line]
    assert len(hit_lines) == 5
    assert "- [zephyr] and 2 more (`brain watch hits zephyr`)" in first
    assert not any("sealed" in line for line in first)
    assert "Zephyr sealed" not in written.read_text()
    assert unnotified == 0
    assert second == []


def test_a_hit_marked_by_wiki_still_shows_in_a_later_plain_brief(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    words = WordEmbedder()
    _watch(test_db, "zephyr", "zephyr")
    ingest(test_db, words, title="Zephyr open", content="zephyr open")
    ingest(
        test_db, words, title="Zephyr sealed", content="zephyr sealed", sensitivity="confidential"
    )
    _build(tmp_path, wiki=True)

    data, _ = _build(tmp_path, wiki=False)

    assert sorted((h.title, h.notified) for h in data.watch_hits) == [
        ("Zephyr open", True),
        ("Zephyr sealed", True),
    ]


def test_a_skipped_semantic_watch_is_named_in_both_payloads(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    _watch(test_db, "orbit", "orbital", mode=MODE_SEMANTIC, matcher=_word_matcher())

    data, written = _build(tmp_path, wiki=True, embedder=DownEmbedder())

    assert data.watch_notes == [WatchNote(watch="orbit", skipped=SKIP_EMBEDDER)]
    assert written is not None
    assert f"- [orbit] {SKIP_EMBEDDER}" in _page_watch_lines(written.read_text())


def test_watch_fields_alone_do_not_cost_a_second_suggestion_call(
    test_db: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two payloads' watch fields differ by construction (here the terminal one carries a
    skipped watch's note before ``withheld`` is computed), and no hit reaches the prompt, so
    the comparison leaves them out (plan D4)."""
    words = WordEmbedder()
    _watch(test_db, "orbit", "orbital", mode=MODE_SEMANTIC, matcher=_word_matcher())
    ingest(test_db, words, title="Open memo", content="open memo body")
    calls: list[object] = []
    monkeypatch.setattr("brain.brief.suggest_next_steps", lambda b, c: calls.append(b) or ["s"])

    data, written = _build(tmp_path, wiki=True, enrich=True, embedder=DownEmbedder())

    assert data.watch_notes == [WatchNote(watch="orbit", skipped=SKIP_EMBEDDER)]
    assert len(calls) == 1
    assert written is not None and "1. s" in written.read_text()


@pytest.mark.fresh_schema
def test_a_brief_without_migration_029_still_builds(
    test_db: psycopg.Connection, seed_doc: Callable[..., str], tmp_path: Path
) -> None:
    test_db.execute("DROP TABLE saved_search_hits")
    test_db.execute("DROP TABLE saved_searches")
    seed_doc(title="Still here", content="still here body")

    data, written = _build(tmp_path, wiki=True)

    assert [doc.title for doc in data.captures] == ["Still here"]
    assert data.watch_hits == [] and written is not None
