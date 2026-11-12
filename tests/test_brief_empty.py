"""An empty daily brief writes no page, sends nothing and asks the model nothing.

The owner's 0.4.0 request: an empty daily brief is not created. ``brain.brief.is_empty`` is the
one place emptiness is decided. It reads the assembled data only, never ``suggestions``, so
model output cannot rescue an otherwise-empty brief. ``build_brief`` consults it on the published
(gated) payload BEFORE any model call. Plan: ``docs/plans/2026-11-12-empty-daily-brief.md``.

The model is a counting test double on ``brain.brief.suggest_next_steps``; ``chat.chat_json``
raises, so nothing reaches Ollama. Delivery goes to a recording fake channel. All data is
synthetic.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import fields, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import chat, cli, mcp_server
from brain.brief import BriefData, PinnedDoc, is_empty
from brain.brief_entities import NewEntity
from brain.brief_publish import build_brief
from brain.config import Config
from brain.errors import OllamaUnavailable
from brain.queries import DocumentRow
from brain.saved_search import MODE_LEXICAL, WatchFilters, WatchHit, WatchNote
from brain.saved_search_check import Matcher, add_watch
from brain.todo import TodoRow
from brain.vault.frontmatter import parse_frontmatter
from tests.saved_search_fakes import WordEmbedder, ingest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
ON_DATE = date(2026, 6, 9)
ISO = ON_DATE.isoformat()
OWNER = "owner@example.invalid"
EMAIL_ENV = {"BRAIN_BRIEF_DELIVERY": "email", "BRAIN_USER_EMAIL": OWNER}
SKIP_LINE = f"No page written for {ISO}: nothing to publish (confidential documents excluded)."
WHEN = datetime(2026, 6, 9, 7, 0, tzinfo=UTC)

runner = CliRunner()


def _cfg(vault: Path) -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        vault_path=vault,
        brief_since_hours=24,
        brief_todo_since_days=7,
        brief_capture_limit=20,
        brief_pin_limit=10,
        user_email=OWNER,
    )


@pytest.fixture(autouse=True)
def _no_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise OllamaUnavailable("down")

    monkeypatch.setattr(chat, "chat_json", _boom)


class _FakeChannel:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, *, subject: str, body: str) -> None:
        self.sent.append((subject, body))


def _fake_model(
    monkeypatch: pytest.MonkeyPatch, answer: list[str]
) -> list[BriefData]:
    """Replace the suggestion leg; return the list of payloads it was called with."""
    calls: list[BriefData] = []

    def _suggest(brief: BriefData, _cfg: Config) -> list[str]:
        calls.append(brief)
        return list(answer)

    monkeypatch.setattr("brain.brief.suggest_next_steps", _suggest)
    return calls


def _build(vault: Path, *, enrich: bool = False, **kwargs: Any) -> tuple[BriefData, Path | None]:
    return build_brief(
        _cfg(vault),
        on_date=ON_DATE,
        since_hours=24,
        todo_since_days=7,
        enrich=enrich,
        wiki=True,
        **kwargs,
    )


def _page_path(vault: Path) -> Path:
    return vault / "daily" / "2026" / f"{ISO}-brief.md"


def _lexical_watch(conn: psycopg.Connection, name: str, query: str) -> None:
    def _never() -> Any:
        raise AssertionError("a lexical check must not build an embedder")

    add_watch(
        conn,
        name=name,
        query=query,
        filters=WatchFilters(),
        mode=MODE_LEXICAL,
        matcher=Matcher(embedder=_never, vector_sim_floor=0.25, min_cosine=0.45),
    )


def _unnotified(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM saved_search_hits WHERE notified_at IS NULL"
    ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# U: the predicate, pure
# ---------------------------------------------------------------------------


def _empty() -> BriefData:
    return BriefData(date=ON_DATE, captures=[], open_todos=[], pinned=[], suggestions=[])


#: One item per content field. ``suggestions`` is deliberately absent: it is not content.
_ONE_ITEM: dict[str, Any] = {
    "captures": [DocumentRow("d1", "Capture", "note", [], "manual", WHEN)],
    "open_todos": [TodoRow("d2", "Action items", WHEN, "open", "Send the draft")],
    "pinned": [PinnedDoc("d3", "Pinned memo", WHEN)],
    "watch_hits": [WatchHit("zephyr", "d4", "Zephyr memo", "manual", WHEN, False)],
    "watch_notes": [WatchNote(watch="orbit", skipped="embedder unavailable")],
    "new_entities": [NewEntity("e1", "Example Org", "org", WHEN, 1)],
    "possible_duplicates": 1,
}


def test_an_all_empty_brief_is_empty() -> None:
    assert is_empty(_empty()) is True


@pytest.mark.parametrize("field_name", sorted(_ONE_ITEM))
def test_one_item_in_any_content_field_makes_the_brief_non_empty(field_name: str) -> None:
    brief = replace(_empty(), **{field_name: _ONE_ITEM[field_name]})

    assert is_empty(brief) is False


def test_every_field_but_date_and_suggestions_is_covered() -> None:
    """A new BriefData field must be classified here, or this test names it."""
    names = {f.name for f in fields(BriefData)}

    assert names - {"date", "suggestions"} == set(_ONE_ITEM)


def test_suggestions_alone_cannot_rescue_an_empty_brief() -> None:
    brief = BriefData(
        date=ON_DATE, captures=[], open_todos=[], pinned=[], suggestions=["Invented step"]
    )

    assert is_empty(brief) is True


# ---------------------------------------------------------------------------
# I: build_brief against the test database
# ---------------------------------------------------------------------------


def test_an_empty_brief_writes_no_page_sends_nothing_and_asks_no_model(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = _fake_model(monkeypatch, [])
    channel = _FakeChannel()

    with caplog.at_level(logging.WARNING, logger="brain"):
        data, written = _build(
            tmp_path, enrich=True, delivery_env=EMAIL_ENV, delivery_channel=channel
        )

    assert written is None
    assert list(tmp_path.rglob("*")) == []
    assert calls == []
    assert channel.sent == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert is_empty(data)


def test_model_output_cannot_rescue_an_empty_brief(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_model(monkeypatch, ["Invented step"])

    data, written = _build(tmp_path, enrich=True)

    assert calls == []
    assert written is None and not _page_path(tmp_path).exists()
    assert data.suggestions == []


def test_one_capture_writes_the_page_exactly_as_before(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    seed_doc: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterization: green before the change and after it."""
    seed_doc(title="Open memo", content="open memo body")
    calls = _fake_model(monkeypatch, ["Review the open memo"])
    channel = _FakeChannel()

    data, written = _build(
        tmp_path, enrich=True, delivery_env=EMAIL_ENV, delivery_channel=channel
    )

    assert written == _page_path(tmp_path)
    front, body = parse_frontmatter(written.read_text(encoding="utf-8"))
    assert front["kind"] == "brief" and front["title"] == f"Brain Brief · {ISO}"
    assert body == (
        f"# Brain Brief · {ISO}\n\n"
        "## Recent captures\n\n- [manual] Open memo\n\n"
        "## Open action items\n\n_No open action items._\n\n"
        "## Pinned / follow-up docs\n\n_No pinned docs._\n\n"
        "## Suggested next steps\n\n1. Review the open memo\n"
    )
    assert len(calls) == 1
    assert data.suggestions == ["Review the open memo"]
    assert len(channel.sent) == 1


def test_a_confidential_only_brief_writes_no_page_and_asks_the_model_once(
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = ingest(
        test_db, WordEmbedder(), title="Sealed memo", content="sealed memo body",
        sensitivity="confidential",
    )
    calls = _fake_model(monkeypatch, ["Read the sealed memo"])

    data, written = _build(tmp_path, enrich=True)

    assert [doc.id for doc in data.captures] == [sealed]
    assert data.suggestions == ["Read the sealed memo"]
    assert written is None and not _page_path(tmp_path).exists()
    assert len(calls) == 1 and [d.id for d in calls[0].captures] == [sealed]


def test_an_old_unnotified_hit_writes_a_page_when_the_terminal_brief_is_empty(
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page is decided on the published payload, after its watch view is swapped in."""
    _lexical_watch(test_db, "zephyr", "zephyr")
    doc = ingest(test_db, WordEmbedder(), title="Zephyr old", content="zephyr old plan")
    build_brief(  # records the hit and marks nothing
        _cfg(tmp_path), on_date=ON_DATE, since_hours=24, todo_since_days=7,
        enrich=False, wiki=False,
    )
    test_db.execute(
        "UPDATE documents SET ingested_at = NOW() - INTERVAL '3 days' WHERE id = %s", (doc,)
    )
    test_db.execute("UPDATE saved_search_hits SET first_seen_at = NOW() - INTERVAL '3 days'")
    calls = _fake_model(monkeypatch, ["Invented step"])

    data, written = _build(tmp_path, enrich=True)

    assert is_empty(data)
    assert written == _page_path(tmp_path)
    assert "- [zephyr] Zephyr old" in written.read_text(encoding="utf-8")
    assert calls == []
    assert _unnotified(test_db) == 0


def test_a_skipped_page_still_marks_its_confidential_watch_hits(
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    _lexical_watch(test_db, "zephyr", "zephyr")
    ingest(
        test_db, WordEmbedder(), title="Zephyr sealed", content="zephyr sealed plan",
        sensitivity="confidential",
    )

    _, written = _build(tmp_path)

    assert written is None and not _page_path(tmp_path).exists()
    assert _unnotified(test_db) == 0


def test_an_empty_rerun_leaves_an_existing_page_untouched(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    tmp_path: Path,
) -> None:
    page = _page_path(tmp_path)
    page.parent.mkdir(parents=True)
    page.write_text("---\nkind: brief\n---\n\nAn earlier synthetic page.\n", encoding="utf-8")
    before = page.read_bytes()

    _, written = _build(tmp_path)

    assert written is None
    assert page.read_bytes() == before


# ---------------------------------------------------------------------------
# C: the CLI
# ---------------------------------------------------------------------------


def test_cli_empty_wiki_exits_zero_with_one_skip_line(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(cli.app, ["brief", "--no-enrich", "--wiki", "--date", ISO])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.count(SKIP_LINE) == 1
    assert "Wrote " not in result.stdout
    assert not _page_path(tmp_path).exists()


def test_cli_empty_json_wiki_keeps_the_shape_and_pure_json(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))

    result = runner.invoke(
        cli.app, ["brief", "--no-enrich", "--json", "--wiki", "--date", ISO]
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert set(payload) == set(_empty().to_dict())
    assert payload["captures"] == [] and payload["possible_duplicates"] == 0
    assert not _page_path(tmp_path).exists()


def test_cli_one_capture_still_prints_the_wrote_line(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    seed_doc: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterization: green before the change and after it."""
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    seed_doc(title="Open memo", content="open memo body")

    result = runner.invoke(cli.app, ["brief", "--no-enrich", "--wiki", "--date", ISO])

    assert result.exit_code == 0, result.stdout
    assert f"Wrote {_page_path(tmp_path)} (confidential documents excluded)" in result.stdout
    assert SKIP_LINE not in result.stdout


# ---------------------------------------------------------------------------
# M: MCP brain_brief asks the model nothing on an empty payload
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_state(
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


def test_mcp_brain_brief_asks_no_model_on_an_empty_payload(
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs state
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_model(monkeypatch, ["Invented step"])

    payload = mcp_server.brain_brief()

    assert calls == []
    assert payload["suggestions"] == []


def test_mcp_brain_brief_still_asks_the_model_once_for_a_real_item(
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs state
    seed_doc: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_doc(title="Open memo", content="open memo body")
    calls = _fake_model(monkeypatch, ["Review the open memo"])

    payload = mcp_server.brain_brief()

    assert len(calls) == 1
    assert payload["suggestions"] == ["Review the open memo"]
