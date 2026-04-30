"""CLI integration tests for ``brain links``."""
import json
import os
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app


def _derived(
    conn: psycopg.Connection[Any],
    *,
    a: str,
    b: str,
    rule: str = "shared_thread",
    weight: float = 1.0,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Insert a ``derived_links`` row in canonical (LEAST, GREATEST) order."""
    src, dst = (a, b) if a < b else (b, a)
    payload = {} if evidence is None else evidence
    conn.execute(
        """
        INSERT INTO derived_links
          (src_document_id, dst_document_id, rule, evidence, weight)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        """,
        (src, dst, rule, json.dumps(payload), weight),
    )

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
) -> None:
    conn.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s, NULL)
        """,
        (src, dst, text, kind),
    )


def _unresolved(
    conn: psycopg.Connection[Any],
    *,
    src: str,
    text: str,
) -> None:
    conn.execute(
        """
        INSERT INTO unresolved_links
          (src_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, 'wiki', NULL)
        """,
        (src, text),
    )


def test_links_resolved_human(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B", vault_path="b.md")
    _link(test_db, src=a, dst=b, text="[[B]]")
    result = CliRunner().invoke(app, ["links", a[:8]])
    assert result.exit_code == 0, result.output
    assert "B" in result.output
    assert "[[B]]" in result.output


def test_links_no_unresolved_by_default(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    """Without --unresolved, dangling refs are hidden."""
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    _unresolved(test_db, src=a, text="[[Nowhere]]")
    result = CliRunner().invoke(app, ["links", a[:8]])
    assert result.exit_code == 0
    assert "(no outgoing links)" in result.output


def test_links_unresolved_flag_includes_dangling(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    _unresolved(test_db, src=a, text="[[Nowhere]]")
    result = CliRunner().invoke(app, ["links", a[:8], "--unresolved"])
    assert result.exit_code == 0, result.output
    assert "(unresolved)" in result.output
    assert "[[Nowhere]]" in result.output


def test_links_mixed_resolved_and_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B", vault_path="b.md")
    _link(test_db, src=a, dst=b, text="[[B]]")
    _unresolved(test_db, src=a, text="[[Nowhere]]")
    result = CliRunner().invoke(app, ["links", a[:8], "--unresolved"])
    assert result.exit_code == 0
    # Resolved row precedes unresolved (the underlying helper guarantees that).
    out = result.output
    resolved_idx = out.find("[[B]]")
    unresolved_idx = out.find("[[Nowhere]]")
    assert resolved_idx >= 0
    assert unresolved_idx >= 0
    assert resolved_idx < unresolved_idx


def test_links_json_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B", vault_path="b.md")
    _link(test_db, src=a, dst=b, text="[[B]]")
    _unresolved(test_db, src=a, text="[[Nowhere]]")
    result = CliRunner().invoke(
        app, ["links", a[:8], "--unresolved", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 2
    assert payload[0]["resolved"] is True
    assert payload[0]["dst_document_id"] == b
    assert payload[1]["resolved"] is False
    assert payload[1]["dst_document_id"] is None


def test_links_no_outgoing(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    result = CliRunner().invoke(app, ["links", a[:8]])
    assert result.exit_code == 0
    assert "(no outgoing links)" in result.output


def test_links_unknown_id_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["links", "00000000"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_links_annotates_derived_rows(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    """Outgoing derived rows carry a ``[derived: <rule>]`` prefix.

    Derived edges are undirected — ``brain links <doc>`` returns the
    partner regardless of which side of the canonical (LEAST, GREATEST)
    storage row the doc sits on.
    """
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="person-x conversation", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="Re: person-x intro", vault_path="b.md")
    _derived(test_db, a=a, b=b, rule="same_day_participant", weight=0.7)
    result = CliRunner().invoke(app, ["links", a[:8]])
    assert result.exit_code == 0, result.output
    assert "[derived: same_day_participant]" in result.output
    assert "Re: person-x intro" in result.output


def test_links_does_not_annotate_wiki_rows(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    """Pure wiki-link outgoing rows must not pick up a ``[derived:`` prefix."""
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B", vault_path="b.md")
    _link(test_db, src=a, dst=b, text="[[B]]")
    result = CliRunner().invoke(app, ["links", a[:8]])
    assert result.exit_code == 0, result.output
    assert "[derived:" not in result.output
    assert "[[B]]" in result.output


def test_links_json_includes_rule_weight_evidence(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    """``--json`` for derived outgoing rows carries rule / weight / evidence."""
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="person-x conversation", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="Re: person-x intro", vault_path="b.md")
    _derived(
        test_db,
        a=a,
        b=b,
        rule="shared_thread",
        weight=1.0,
        evidence={"thread_id": "t-42"},
    )
    result = CliRunner().invoke(app, ["links", a[:8], "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    derived_rows = [p for p in payload if p["link_kind"] == "derived"]
    assert len(derived_rows) == 1
    row = derived_rows[0]
    assert row["rule"] == "shared_thread"
    assert row["weight"] == 1.0
    assert row["evidence"] == {"thread_id": "t-42"}
    assert row["resolved"] is True


def test_links_json_wiki_rows_have_null_derived_fields(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    """Wiki rows in ``brain links --json`` expose rule / weight / evidence as null.

    Mirror of ``test_backlinks_json_wiki_rows_have_null_derived_fields``
    — keeps the JSON shape uniform across wiki and derived rows so
    downstream consumers can rely on field presence regardless of which
    side of the edge they're inspecting.
    """
    _set_env(monkeypatch)
    a = _make_doc(test_db, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    b = _make_doc(test_db, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B", vault_path="b.md")
    _link(test_db, src=a, dst=b, text="[[B]]")
    result = CliRunner().invoke(app, ["links", a[:8], "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    row = payload[0]
    assert row["link_kind"] == "wiki"
    assert row["resolved"] is True
    assert row["rule"] is None
    assert row["weight"] is None
    assert row["evidence"] is None
