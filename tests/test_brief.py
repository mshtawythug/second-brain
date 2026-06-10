"""Tests for the daily-brief assembler (``brain.brief``) + the CLI command.

Unit + integration against the real test DB. The LLM leg is exercised by
patching ``brain.chat.chat_json`` (a standard test double); no test contacts
Ollama. All fixtures are synthetic.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import chat, cli
from brain.brief import (
    BriefData,
    PinnedDoc,
    assemble_brief,
    suggest_next_steps,
    write_brief_to_vault,
)
from brain.config import Config, ConfigError
from brain.errors import OllamaUnavailable
from brain.queries import DocumentRow
from brain.vault.frontmatter import parse_frontmatter

runner = CliRunner()
TODAY = date(2026, 6, 9)


def _cfg(**overrides: object) -> Config:
    base: dict[str, object] = {
        "database_url": "postgresql://u:p@localhost:5/db",
        "brief_since_hours": 24,
        "brief_todo_since_days": 7,
        "brief_capture_limit": 20,
        "brief_pin_limit": 10,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def _pin(conn: psycopg.Connection, doc_id: str, when: datetime | None = None) -> None:
    if when is None:
        conn.execute(
            "INSERT INTO interactions (document_id, action, source) "
            "VALUES (%s, 'pinned', 'cli')",
            (doc_id,),
        )
    else:
        conn.execute(
            "INSERT INTO interactions (document_id, action, source, at) "
            "VALUES (%s, 'pinned', 'cli', %s)",
            (doc_id, when),
        )


def _krisp_action_items_doc(conn: psycopg.Connection, body: str) -> str:
    return str(
        conn.execute(
            "INSERT INTO documents (title, content, content_hash, content_type) "
            "VALUES ('Action items', %s, %s, 'krisp_action_items') RETURNING id::text",
            (body, str(uuid.uuid4())),
        ).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# assemble_brief
# ---------------------------------------------------------------------------


def test_assemble_brief_empty_corpus(test_db: psycopg.Connection) -> None:
    data = assemble_brief(
        test_db, _cfg(), since_hours=24, todo_since_days=7, on_date=TODAY
    )
    assert data.captures == []
    assert data.open_todos == []
    assert data.pinned == []
    assert data.suggestions == []
    assert data.date == TODAY


def test_assemble_brief_capture_limit(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    for i in range(3):
        seed_doc(title=f"Cap {i}", content=f"cap body {i}")
    data = assemble_brief(
        test_db,
        _cfg(brief_capture_limit=2),
        since_hours=24,
        todo_since_days=7,
        on_date=TODAY,
    )
    assert len(data.captures) == 2


def test_assemble_brief_excludes_closed_todos(test_db: psycopg.Connection) -> None:
    _krisp_action_items_doc(
        test_db, "- [ ] open one\n- [x] closed one\n- [ ] open two\n"
    )
    data = assemble_brief(
        test_db, _cfg(), since_hours=24, todo_since_days=7, on_date=TODAY
    )
    texts = {row.text for row in data.open_todos}
    assert texts == {"open one", "open two"}


def test_assemble_brief_pinned_dedup(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    doc = seed_doc(title="Pinned doc", content="p body")
    earlier = datetime(2026, 6, 1, tzinfo=UTC)
    later = datetime(2026, 6, 5, tzinfo=UTC)
    _pin(test_db, doc, earlier)
    _pin(test_db, doc, later)
    data = assemble_brief(
        test_db, _cfg(), since_hours=24, todo_since_days=7, on_date=TODAY
    )
    assert len(data.pinned) == 1
    assert data.pinned[0].pinned_at == later


def test_assemble_brief_since_hours_boundary(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    inside = seed_doc(title="Inside", content="inside body")
    outside = seed_doc(title="Outside", content="outside body")
    # ``outside`` ingested just before the 24h window opened → excluded.
    test_db.execute(
        "UPDATE documents SET ingested_at = NOW() - INTERVAL '24 hours' "
        "- INTERVAL '1 second' WHERE id = %s",
        (outside,),
    )
    data = assemble_brief(
        test_db, _cfg(), since_hours=24, todo_since_days=7, on_date=TODAY
    )
    ids = {doc.id for doc in data.captures}
    assert inside in ids
    assert outside not in ids


# ---------------------------------------------------------------------------
# suggest_next_steps (patched chat_json — never hits Ollama)
# ---------------------------------------------------------------------------


def _brief_with_context() -> BriefData:
    return BriefData(
        date=TODAY,
        captures=[
            DocumentRow(
                id="d1",
                title="A capture title",
                content_type="note",
                tags=[],
                source_kind="manual",
                ingested_at=datetime(2026, 6, 9, tzinfo=UTC),
            )
        ],
        open_todos=[],
        pinned=[],
        suggestions=[],
    )


def test_suggest_next_steps_ollama_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise OllamaUnavailable("down")

    monkeypatch.setattr(chat, "chat_json", _boom)
    result = suggest_next_steps(_brief_with_context(), _cfg())
    assert result == []


def test_suggest_next_steps_returns_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def _ok(*_a: object, **_k: object) -> dict[str, object]:
        return {"suggestions": ["item 1", "item 2", ""]}

    monkeypatch.setattr(chat, "chat_json", _ok)
    result = suggest_next_steps(_brief_with_context(), _cfg())
    # Empty entries are dropped; no raw bodies present.
    assert result == ["item 1", "item 2"]


def _empty_brief() -> BriefData:
    return BriefData(
        date=TODAY, captures=[], open_todos=[], pinned=[], suggestions=[]
    )


def test_suggest_next_steps_empty_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def _ok(prompt: str, **_k: object) -> dict[str, object]:
        captured.append(prompt)
        return {"suggestions": ["something"]}

    monkeypatch.setattr(chat, "chat_json", _ok)
    result = suggest_next_steps(_empty_brief(), _cfg())
    assert result == ["something"]
    # The "(none)" placeholders are present for both empty sections.
    assert captured[0].count("- (none)") == 2


def test_suggest_next_steps_non_list_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _bad(*_a: object, **_k: object) -> dict[str, object]:
        return {"suggestions": "not a list"}

    monkeypatch.setattr(chat, "chat_json", _bad)
    assert suggest_next_steps(_brief_with_context(), _cfg()) == []


# ---------------------------------------------------------------------------
# write_brief_to_vault
# ---------------------------------------------------------------------------


def test_write_brief_to_vault(tmp_path: Path) -> None:
    data = BriefData(
        date=TODAY,
        captures=[],
        open_todos=[],
        pinned=[
            PinnedDoc(
                document_id="d1",
                title="A pin",
                pinned_at=datetime(2026, 6, 9, tzinfo=UTC),
            )
        ],
        suggestions=["Draft the reply."],
    )
    path = write_brief_to_vault(tmp_path, TODAY, data)
    assert path == tmp_path / "daily" / "2026" / "2026-06-09-brief.md"
    fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["kind"] == "brief"
    assert str(fm["date"]) == "2026-06-09"
    assert fm["title"] == "Brain Brief · 2026-06-09"
    assert "A pin" in body
    assert "Draft the reply." in body


def _full_brief() -> BriefData:
    from brain.todo import TodoRow

    return BriefData(
        date=TODAY,
        captures=[
            DocumentRow(
                id="d1",
                title="Capture one",
                content_type="note",
                tags=[],
                source_kind="krisp",
                ingested_at=datetime(2026, 6, 9, tzinfo=UTC),
            )
        ],
        open_todos=[
            TodoRow(
                document_id="d2",
                document_title="Meeting",
                ingested_at=datetime(2026, 6, 8, tzinfo=UTC),
                state="open",
                text="Follow up on the proposal",
            )
        ],
        pinned=[],
        suggestions=[],
    )


def test_write_brief_to_vault_full_sections(tmp_path: Path) -> None:
    path = write_brief_to_vault(tmp_path, TODAY, _full_brief())
    body = path.read_text(encoding="utf-8")
    assert "- [krisp] Capture one" in body
    assert "- [ ] Follow up on the proposal" in body


def test_suggest_next_steps_includes_todos(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def _ok(prompt: str, **_k: object) -> dict[str, object]:
        captured.append(prompt)
        return {"suggestions": ["do it"]}

    monkeypatch.setattr(chat, "chat_json", _ok)
    suggest_next_steps(_full_brief(), _cfg())
    assert "- Capture one" in captured[0]
    assert "- Follow up on the proposal" in captured[0]


def test_write_brief_to_vault_empty_sections(tmp_path: Path) -> None:
    path = write_brief_to_vault(tmp_path, TODAY, _empty_brief())
    body = path.read_text(encoding="utf-8")
    assert "_No recent captures._" in body
    assert "_No open action items._" in body
    assert "_No pinned docs._" in body
    # No suggestions section when there are none.
    assert "## Suggested next steps" not in body


# ---------------------------------------------------------------------------
# config validation
# ---------------------------------------------------------------------------


def test_brief_config_bad_since_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_BRIEF_SINCE_HOURS", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5/db")
    with pytest.raises(ConfigError):
        Config.load()


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default: any run that reaches suggestion synthesis treats Ollama as down.
    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise OllamaUnavailable("down")

    monkeypatch.setattr(chat, "chat_json", _boom)


def test_brief_command_smoke(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    seed_doc: Callable[..., str],
) -> None:
    seed_doc(title="Smoke A", content="smoke a body")
    seed_doc(title="Smoke B", content="smoke b body")
    result = runner.invoke(cli.app, ["brief", "--no-enrich", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "captures" in payload
    assert len(payload["captures"]) >= 2
    assert payload["suggestions"] == []


def test_brief_command_pinned_section(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    doc = seed_doc(title="Pinned smoke", content="ps body")
    _pin(test_db, doc)
    result = runner.invoke(cli.app, ["brief", "--no-enrich", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    pinned_ids = {p["id"] for p in payload["pinned"]}
    assert doc in pinned_ids
