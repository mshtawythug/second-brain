"""CLI smoke tests for ``brain resurface`` (Plan 02 Phase 2).

Seeds synthetic documents into the real test DB, then drives the command via
``CliRunner``. ``DATABASE_URL`` is forced to the test DB by the session-scoped
conftest fixture, so the in-process command opens its own connection against the
same database the ``test_db`` fixture seeds.
"""
from __future__ import annotations

import json
import uuid

import psycopg
from typer.testing import CliRunner

from brain.cli import app

runner = CliRunner()


def _insert_doc(
    conn: psycopg.Connection,
    *,
    title: str,
    age_days: float = 200.0,
    source_kind: str = "manual",
    tags: list[str] | None = None,
) -> str:
    """Insert one synthetic, eligible document and return its UUID as text."""
    src = conn.execute(
        "INSERT INTO sources (kind, external_id) VALUES (%s, %s) RETURNING id::text",
        (source_kind, str(uuid.uuid4())),
    ).fetchone()
    assert src is not None
    row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type, tags,
             ingested_at)
        VALUES (%s, %s, %s, %s, %s, %s, now() - (%s * interval '1 day'))
        RETURNING id::text
        """,
        (
            src[0],
            title,
            "resurface cli body text",
            str(uuid.uuid4()),
            "note",
            tags or [],
            age_days,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_cli_resurface_human_output(test_db: psycopg.Connection) -> None:
    """Human output renders the Rich table with the score column + a doc title."""
    _insert_doc(test_db, title="alpha", age_days=300.0)
    _insert_doc(test_db, title="bravo", age_days=200.0)

    result = runner.invoke(app, ["resurface"])

    assert result.exit_code == 0, result.output
    assert "Score" in result.output
    assert "alpha" in result.output
    assert "bravo" in result.output


def test_cli_resurface_json_output(test_db: psycopg.Connection) -> None:
    """--json emits a parseable array with all the documented keys."""
    _insert_doc(test_db, title="alpha", age_days=300.0, tags=["x"])

    result = runner.invoke(app, ["resurface", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 1
    item = payload[0]
    for key in (
        "id",
        "title",
        "source_kind",
        "content_type",
        "tags",
        "age_days",
        "last_access_days",
        "score",
        "snippet",
    ):
        assert key in item
    assert item["title"] == "alpha"
    assert item["last_access_days"] is None  # never opened


def test_cli_resurface_limit_flag(test_db: psycopg.Connection) -> None:
    """--limit 3 returns exactly 3 rows in JSON."""
    for i in range(6):
        _insert_doc(test_db, title=f"doc {i}", age_days=100.0 + i)

    result = runner.invoke(app, ["resurface", "--limit", "3", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 3


def test_cli_resurface_empty(test_db: psycopg.Connection) -> None:
    """Empty corpus prints the friendly no-results message."""
    result = runner.invoke(app, ["resurface"])

    assert result.exit_code == 0, result.output
    assert "No docs due for review." in result.output


def test_cli_resurface_source_filter(test_db: psycopg.Connection) -> None:
    """--source narrows the queue to the chosen source kind."""
    _insert_doc(test_db, title="manual one", source_kind="manual", age_days=200.0)
    _insert_doc(test_db, title="krisp one", source_kind="krisp", age_days=200.0)

    result = runner.invoke(app, ["resurface", "--source", "manual", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [it["title"] for it in payload] == ["manual one"]
