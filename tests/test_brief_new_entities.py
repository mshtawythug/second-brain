"""The "new this week" section reaches every brief surface through the two-audience build (RM-17).

The terminal payload is permissive, the published page and the delivered message are gated, and
no surface lists an entity known only from drafts. Delivery is a fake channel with a plain-dict
environment, and the LLM is a test double that never reaches Ollama. All data is synthetic.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import brief as brief_mod
from brain import chat, cli
from brain.brief import BriefData, assemble_brief, terminal_sections, write_brief_to_vault
from brain.brief_delivery import render_delivery, without_drafts
from brain.brief_entities import NewEntity
from brain.brief_publish import build_brief
from brain.config import Config
from brain.errors import OllamaUnavailable
from tests.graph_entity_fakes import seed_doc, seed_entity, seed_mention

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
ON_DATE = date(2026, 6, 9)
OWNER = "owner@example.invalid"
EMAIL_ENV = {"BRAIN_BRIEF_DELIVERY": "email", "BRAIN_USER_EMAIL": OWNER}
SECTION = "New this week"


def _cfg(vault: Path, **overrides: Any) -> Config:
    fields: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "vault_path": vault,
        "brief_since_hours": 24,
        "brief_todo_since_days": 7,
        "brief_capture_limit": 20,
        "brief_pin_limit": 10,
        "user_email": OWNER,
    }
    fields.update(overrides)
    return Config(**fields)


class _FakeChannel:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, *, subject: str, body: str) -> None:
        self.sent.append((subject, body))


@pytest.fixture(autouse=True)
def _no_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise OllamaUnavailable("down")

    monkeypatch.setattr(chat, "chat_json", _boom)


def _new(conn: psycopg.Connection, name: str, **doc: Any) -> str:
    """An entity whose only mention is a document ingested yesterday (real clock)."""
    yesterday = datetime.now(UTC) - timedelta(days=1)
    entity = seed_entity(conn, name, created_at=yesterday)
    seed_mention(conn, entity, seed_doc(conn, ingested_at=yesterday, **doc))
    return entity


def _seed_three(conn: psycopg.Connection) -> None:
    _new(conn, "Open Example")
    _new(conn, "Sealed Example", sensitivity="confidential")
    _new(conn, "Draft Example", draft=True)


def _assemble(conn: psycopg.Connection, cfg: Config, *, gated: bool) -> BriefData:
    return assemble_brief(
        conn, cfg, since_hours=24, todo_since_days=7, on_date=ON_DATE,
        exclude_confidential=gated,
    )


def _entity(name: str, entity_type: str = "person") -> NewEntity:
    return NewEntity(
        entity_id=str(uuid.uuid4()),
        name=name,
        entity_type=entity_type,
        first_ingested_at=datetime(2026, 6, 8, tzinfo=UTC),
        document_count=1,
    )


def _brief(**overrides: Any) -> BriefData:
    fields: dict[str, Any] = {
        "date": ON_DATE, "captures": [], "open_todos": [], "pinned": [], "suggestions": [],
    }
    fields.update(overrides)
    return BriefData(**fields)


# --- assemble_brief: the payloads -------------------------------------------------------------


def test_assemble_brief_lists_new_entities_under_each_lens(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    _seed_three(test_db)
    cfg = _cfg(tmp_path)

    permissive = _assemble(test_db, cfg, gated=False)
    gated = _assemble(test_db, cfg, gated=True)

    assert sorted(e.name for e in permissive.new_entities) == ["Open Example", "Sealed Example"]
    assert [e.name for e in gated.new_entities] == ["Open Example"]


def test_graph_disabled_brief_has_no_new_entities(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    _new(test_db, "Open Example")

    data = _assemble(test_db, _cfg(tmp_path, graph_enabled=False), gated=False)

    assert data.new_entities == []
    assert data.to_dict()["new_entities"] == []


def test_to_dict_carries_new_entities() -> None:
    entity = _entity("Wire Example")

    assert _brief(new_entities=[entity]).to_dict()["new_entities"] == [entity.to_dict()]


# --- the page and the terminal ----------------------------------------------------------------


def test_page_renders_the_section_after_watch_hits(tmp_path: Path) -> None:
    path = write_brief_to_vault(
        tmp_path, ON_DATE, _brief(new_entities=[_entity("Page Example", "org")])
    )

    body = path.read_text(encoding="utf-8")

    assert f"## {SECTION}\n\n- [org] Page Example\n" in body


def test_page_omits_an_empty_section(tmp_path: Path) -> None:
    body = write_brief_to_vault(tmp_path, ON_DATE, _brief()).read_text(encoding="utf-8")

    assert SECTION not in body


def test_terminal_sections_lists_watch_hits_then_new_entities_and_skips_empty_ones() -> None:
    sections = terminal_sections(_brief(new_entities=[_entity("Term Example")]))

    assert sections == [("🆕  New this week", ["[person] Term Example"])]
    assert terminal_sections(_brief()) == []


def test_cli_prints_the_section(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_brief(_brief(new_entities=[_entity("Cli Example", "project")]))

    out = capsys.readouterr().out

    assert "🆕  New this week\n  • [project] Cli Example\n" in out


# --- build_brief: two audiences, then delivery ------------------------------------------------


def test_build_brief_gates_the_page_and_delivery_and_never_lists_drafts(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    _seed_three(test_db)
    channel = _FakeChannel()

    data, written = build_brief(
        _cfg(tmp_path), on_date=ON_DATE, since_hours=24, todo_since_days=7, enrich=False,
        wiki=True, delivery_env=EMAIL_ENV, delivery_channel=channel,
    )

    assert written is not None
    page = written.read_text(encoding="utf-8")
    [(_, delivered)] = channel.sent
    assert {e.name for e in data.new_entities} == {"Open Example", "Sealed Example"}
    assert "[person] Open Example" in page
    assert "Sealed Example" not in page
    assert f"{SECTION}\n- [person] Open Example\n" in delivered
    assert "Sealed Example" not in delivered
    for surface in (page, delivered, str(data.to_dict())):
        assert "Draft Example" not in surface


def test_new_entities_alone_do_not_cost_a_second_suggestion_call(
    test_db: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The payloads differ only in the section, which never reaches the suggestion prompt.
    _new(test_db, "Sealed Example", sensitivity="confidential")
    calls: list[BriefData] = []

    def _suggest(brief: BriefData, _cfg: Config) -> list[str]:
        calls.append(brief)
        return ["synthetic step"]

    monkeypatch.setattr(brief_mod, "suggest_next_steps", _suggest)

    build_brief(
        _cfg(tmp_path), on_date=ON_DATE, since_hours=24, todo_since_days=7, enrich=True,
        wiki=True, delivery_env={},
    )

    assert len(calls) == 1


# --- delivery render --------------------------------------------------------------------------


def test_delivery_render_carries_the_section_on_one_line_per_name() -> None:
    brief = _brief(new_entities=[_entity("Mail\nExample")])

    _, body = render_delivery(brief, include_todos=False)

    assert f"{SECTION}\n- [person] Mail Example\n" in body


def test_delivery_render_omits_an_empty_section() -> None:
    _, body = render_delivery(_brief(), include_todos=False)

    assert SECTION not in body


def test_without_drafts_keeps_the_section() -> None:
    entity = _entity("Kept Example")

    assert without_drafts(_brief(new_entities=[entity]), {"d1"}).new_entities == [entity]
