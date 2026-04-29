"""CLI integration tests for ``brain orphans``."""
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
) -> None:
    conn.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, '[[X]]', 'wiki', NULL)
        """,
        (src, dst),
    )


def test_orphans_empty(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["orphans"])
    assert result.exit_code == 0, result.output
    assert "(no orphans)" in result.output


def test_orphans_vault_only_default(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A vault", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B vault", vault_path="b.md")
    _link(test_db, src=a, dst=b)
    # A and B are linked, neither is an orphan.
    # Add a vault orphan and an ingested orphan.
    _make_doc(test_db, doc_id="33333333-3333-3333-3333-333333333333",
              title="C orphan vault", vault_path="c.md")
    _make_doc(
        test_db,
        doc_id="44444444-4444-4444-4444-444444444444",
        title="D orphan ingested",
        kind="ingested",
        vault_path="_ingested/manual/d.md",
    )
    result = CliRunner().invoke(app, ["orphans"])
    assert result.exit_code == 0
    assert "C orphan vault" in result.output
    assert "D orphan ingested" not in result.output


def test_orphans_all_includes_ingested(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
              title="vault orphan", vault_path="a.md")
    _make_doc(
        test_db,
        doc_id="22222222-2222-2222-2222-222222222222",
        title="ingested orphan",
        kind="ingested",
        vault_path="_ingested/krisp/lonely.md",
    )
    result = CliRunner().invoke(app, ["orphans", "--all"])
    assert result.exit_code == 0
    assert "vault orphan" in result.output
    assert "ingested orphan" in result.output


def test_orphans_json_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
              title="lonely", vault_path="lonely.md")
    result = CliRunner().invoke(app, ["orphans", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["title"] == "lonely"
    assert payload[0]["kind"] == "vault"


def test_orphans_json_empty(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["orphans", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == []
