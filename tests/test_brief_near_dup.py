"""The brief's read-only "Possible duplicates (N)" line on every surface (RM-19).

The terminal payload counts every open pair. The published page and the delivered message are
built from the gated payload, whose count leaves out any pair with a confidential or a draft end
(plan R4), so the number says nothing about confidential pairs. The brief never scans. Delivery
is a fake channel with a plain-dict environment; the LLM is a double that never reaches Ollama.
All data is synthetic.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import chat, cli, near_dup_store
from brain.brief import BriefData, assemble_brief, terminal_sections, write_brief_to_vault
from brain.brief_delivery import render_delivery
from brain.brief_publish import build_brief
from brain.config import Config
from brain.errors import OllamaUnavailable
from tests.near_dup_fakes import body, insert_doc, near_copy

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
ON_DATE = date(2026, 6, 9)
OWNER = "owner@example.invalid"
EMAIL_ENV = {"BRAIN_BRIEF_DELIVERY": "email", "BRAIN_USER_EMAIL": OWNER}


def _cfg(vault: Path) -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        vault_path=vault,
        brief_since_hours=24,
        brief_todo_since_days=7,
        brief_capture_limit=20,
        brief_pin_limit=10,
        user_email=OWNER,
        graph_enabled=False,
    )


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


def _brief(**overrides: Any) -> BriefData:
    fields: dict[str, Any] = {
        "date": ON_DATE, "captures": [], "open_todos": [], "pinned": [], "suggestions": [],
    }
    fields.update(overrides)
    return BriefData(**fields)


def _seed_pairs(conn: psycopg.Connection[Any]) -> None:
    """Three open pairs: one plain, one with a confidential end, one with a draft end."""
    for seed, kwargs in ((1, {}), (2, {"sensitivity": "confidential"}), (3, {"draft": True})):
        text = body(400, seed)
        insert_doc(conn, text, **kwargs)
        insert_doc(conn, near_copy(text))
    near_dup_store.scan(conn)


def _assemble(conn: psycopg.Connection[Any], cfg: Config, *, gated: bool) -> BriefData:
    return assemble_brief(
        conn, cfg, since_hours=24, todo_since_days=7, on_date=ON_DATE,
        exclude_confidential=gated,
    )


def test_assemble_brief_counts_open_pairs_under_each_lens(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    _seed_pairs(test_db)

    assert _assemble(test_db, _cfg(tmp_path), gated=False).possible_duplicates == 3
    assert _assemble(test_db, _cfg(tmp_path), gated=True).possible_duplicates == 1


def test_assemble_brief_never_scans(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    text = body(400, 4)
    insert_doc(test_db, text)
    insert_doc(test_db, near_copy(text))

    data = _assemble(test_db, _cfg(tmp_path), gated=False)

    assert data.possible_duplicates == 0
    assert test_db.execute("SELECT count(*) FROM document_minhash").fetchone() == (0,)


@pytest.mark.fresh_schema
def test_without_migration_030_the_brief_counts_zero(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    test_db.execute("DROP TABLE near_duplicates")
    test_db.execute("DROP TABLE document_minhash")

    assert _assemble(test_db, _cfg(tmp_path), gated=True).possible_duplicates == 0


def test_to_dict_carries_the_count() -> None:
    assert _brief(possible_duplicates=2).to_dict()["possible_duplicates"] == 2
    assert _brief().to_dict()["possible_duplicates"] == 0


def test_page_renders_the_line_and_omits_it_at_zero(tmp_path: Path) -> None:
    page = write_brief_to_vault(tmp_path, ON_DATE, _brief(possible_duplicates=2))
    empty = write_brief_to_vault(tmp_path / "empty", ON_DATE, _brief())

    assert "## Possible duplicates (2)\n\n- review them with `brain dedup list`\n" in (
        page.read_text(encoding="utf-8")
    )
    assert "Possible duplicates" not in empty.read_text(encoding="utf-8")


def test_terminal_sections_and_cli_print_the_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert terminal_sections(_brief(possible_duplicates=1)) == [
        ("🔁  Possible duplicates (1)", ["review them with `brain dedup list`"])
    ]
    assert terminal_sections(_brief()) == []

    cli._print_brief(_brief(possible_duplicates=4))

    assert "🔁  Possible duplicates (4)\n  • review them with `brain dedup list`\n" in (
        capsys.readouterr().out
    )


def test_delivery_sends_the_count_line() -> None:
    _subject, sent = render_delivery(_brief(possible_duplicates=3), include_todos=False)
    _subject, quiet = render_delivery(_brief(), include_todos=False)

    assert "Possible duplicates (3)\n- review them with `brain dedup list`\n" in sent
    assert "Possible duplicates" not in quiet


def test_build_brief_gates_the_page_and_delivery(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    # R4: the terminal sees 3; the page and the delivered message see only the plain pair.
    _seed_pairs(test_db)
    channel = _FakeChannel()

    data, written = build_brief(
        _cfg(tmp_path), on_date=ON_DATE, since_hours=24, todo_since_days=7, enrich=False,
        wiki=True, delivery_env=EMAIL_ENV, delivery_channel=channel,
    )

    assert written is not None
    [(_, delivered)] = channel.sent
    assert data.possible_duplicates == 3
    assert "Possible duplicates (1)" in written.read_text(encoding="utf-8")
    assert "Possible duplicates (1)" in delivered
