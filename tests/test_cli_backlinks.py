"""CLI integration tests for ``brain backlinks``.

Goes through the Typer ``CliRunner`` so we exercise argument parsing,
prefix resolution, and JSON formatting end-to-end against the real test
DB.
"""
import json
import os
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)


def _make_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    kind: str = "vault",
    vault_path: str | None = None,
) -> str:
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, vault_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (doc_id, title, f"body of {title}", f"hash-{doc_id}", "note", kind, vault_path),
    )
    return doc_id


def _link(
    conn: psycopg.Connection[Any],
    *,
    src: str,
    dst: str,
    text: str,
    kind: str = "wiki",
    display: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (src, dst, text, kind, display),
    )


def test_backlinks_human_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A note", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B note", vault_path="b.md")
    _link(test_db, src=a, dst=b, text="[[B note]]")
    result = CliRunner().invoke(app, ["backlinks", b[:8]])
    assert result.exit_code == 0, result.output
    assert "A note" in result.output
    assert "[[B note]]" in result.output
    assert a[:8] in result.output


def test_backlinks_no_results(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A note", vault_path="a.md")
    result = CliRunner().invoke(app, ["backlinks", a[:8]])
    assert result.exit_code == 0
    assert "(no backlinks)" in result.output


def test_backlinks_json_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A note", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B note", vault_path="b.md")
    _link(test_db, src=a, dst=b, text="[[B note]]")
    result = CliRunner().invoke(app, ["backlinks", b[:8], "--json"])
    assert result.exit_code == 0, result.output
    # Strip Rich markup if any; the payload is the only JSON-shaped thing.
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["src_document_id"] == a
    assert payload[0]["src_title"] == "A note"
    assert payload[0]["src_kind"] == "vault"
    assert payload[0]["link_text"] == "[[B note]]"
    assert payload[0]["link_kind"] == "wiki"


def test_backlinks_unknown_id_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["backlinks", "00000000"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_backlinks_short_prefix_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["backlinks", "abc"])
    assert result.exit_code != 0
    assert "6 characters" in result.output.lower()
