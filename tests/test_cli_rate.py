"""Tests for the ``brain rate <id> useful|irrelevant`` CLI command.

Covers the happy paths for both verdicts, the append semantics, and the
id-prefix / verdict error paths.
"""
from __future__ import annotations

import os
import time

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> str:
    """Wire env + fake embedder; seed one doc; return its id."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="Seed",
            content="Seed body",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    assert result.document_id is not None
    return result.document_id


def _interaction_rows(doc_id: str) -> list[tuple[str, str, str, str | None]]:
    """Read every interaction row for ``doc_id`` as (action, source, query, session_id)."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT action, source, query, session_id::text "
            "FROM interactions WHERE document_id = %s ORDER BY at",
            (doc_id,),
        ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def test_brain_rate_useful_inserts_rated_useful_row(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["rate", doc_id, "useful"])
    assert result.exit_code == 0, result.output
    assert "rated_useful" in result.output
    rows = _interaction_rows(doc_id)
    assert len(rows) == 1
    assert rows[0][0] == "rated_useful"
    assert rows[0][1] == "cli"
    assert rows[0][2] is None
    assert rows[0][3] is None


def test_brain_rate_irrelevant_inserts_rated_irrelevant_row(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["rate", doc_id, "irrelevant"])
    assert result.exit_code == 0, result.output
    rows = _interaction_rows(doc_id)
    assert len(rows) == 1
    assert rows[0][0] == "rated_irrelevant"


def test_brain_rate_invalid_verdict_rejects(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["rate", doc_id, "bogus"])
    assert result.exit_code != 0
    assert "useful" in result.output or "irrelevant" in result.output
    rows = _interaction_rows(doc_id)
    assert rows == []


def test_brain_rate_appends_each_call(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Per plan D9 — repeated ratings APPEND (no row-level dedup)."""
    doc_id = _setup(monkeypatch, test_db, fake_embedder)
    runner = CliRunner()
    runner.invoke(app, ["rate", doc_id, "useful"])
    # Postgres NOW() has microsecond resolution; on extremely fast
    # back-to-back runs the two rows can land on the same timestamp.
    # Sleeping 1ms guarantees distinct ``at`` values for the assertion.
    time.sleep(0.001)
    runner.invoke(app, ["rate", doc_id, "useful"])
    rows = _interaction_rows(doc_id)
    assert len(rows) == 2
    # Each row reads back as a separate event — both are 'rated_useful'.
    assert {r[0] for r in rows} == {"rated_useful"}


def test_brain_rate_unknown_id_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["rate", "deadbeef", "useful"])
    assert result.exit_code != 0


def test_brain_rate_short_id_prefix_rejects(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """``_resolve_id`` rejects <6-char prefixes before issuing SQL."""
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["rate", "abc", "useful"])
    assert result.exit_code != 0


def test_brain_rate_ambiguous_id_prefix_exits(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Two docs sharing a 6-char prefix → ``IdPrefixAmbiguous`` →
    Exit(1) with an ``ambiguous`` message. Seeds via direct INSERT to
    pick controlled UUIDs (the chunk pipeline isn't needed — `brain rate`
    only touches the FK target on `documents`)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    shared_prefix = "abcdef"
    for new_id, content in (
        ("abcdef00-0000-0000-0000-000000000001", "alpha"),
        ("abcdef00-0000-0000-0000-000000000002", "bravo"),
    ):
        test_db.execute(
            "INSERT INTO documents (id, title, content, content_hash, "
            "content_type) VALUES (%s, %s, %s, %s, %s)",
            (new_id, content, content, content + "_h", "note"),
        )
    result = CliRunner().invoke(app, ["rate", shared_prefix, "useful"])
    assert result.exit_code != 0
    # `IdPrefixAmbiguous.__init__` surfaces "id prefix ambiguous: <prefix>";
    # `_resolve_id` echoes that string to stderr before raising Exit(1).
    combined = (result.output or "") + (result.stderr or "")
    assert "ambiguous" in combined.lower()
    # No interaction row was written (the resolver raised before INSERT).
    rows = test_db.execute("SELECT count(*) FROM interactions").fetchone()
    assert rows is not None
    assert rows[0] == 0
