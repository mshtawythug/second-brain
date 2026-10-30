"""``brain usage`` without ``--engagement`` is byte-identical to before (RM-22a AC4).

RM-22a adds a ``--engagement`` flag to ``brain usage``. AC4 says the command
WITHOUT the flag prints exactly what it printed before the flag existed. These
goldens were captured from ``cli_usage.py`` at ``ef97b7c``, before the flag
landed (the commit that adds this file changes no ``src/`` line). Every later
run must reproduce them byte for byte, both the human tables and ``--json``.

Two normalizations, both applied to the golden and the output alike, and
neither able to hide a change the flag could make:

- **Dates.** The daily table and nothing else prints calendar days. They
  depend on the day the suite runs, so every ``YYYY-MM-DD`` becomes
  ``<DATE>``. Every other character still counts.
- **Width and colour.** Rich sizes tables to the terminal, so the shared
  console is pinned to a fixed width for the test (``monkeypatch``, restored
  after), and ANSI escapes are stripped. A developer's wide terminal or CI's
  ``GITHUB_ACTIONS`` colour detection cannot then flip the result.

A missing golden is written and the test FAILS, so a first run can never pass
vacuously. Regenerating one is a deliberate act: delete the file, re-run, and
justify the diff in review.

All fixture data is synthetic.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import format as brain_format
from brain.cli import app
from brain.gaps import record_search_query
from brain.interactions import record_interaction

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "usage_golden"

#: Wide enough that no column of the fixture's tables wraps.
_CONSOLE_WIDTH = 120

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: Synthetic search strings. Distinct, so the top-queries ranking is fixed.
_QUERY_FREQUENT = "synthetic roadmap planning"
_QUERY_ONCE = "synthetic zero hit query"
_QUERY_MCP = "synthetic agent lookup"

#: A fixed session id, so the MCP search and its open share one session.
_SESSION = uuid.UUID("00000000-0000-4000-8000-0000000000a1")


def _doc(conn: psycopg.Connection[Any], *, title: str, content_hash: str) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES (%s, 'body', 'note', 'vault', %s) RETURNING id::text",
        (title, content_hash),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed(conn: psycopg.Connection[Any]) -> None:
    """Searches, opens, feedback and ingests on every surface, with known counts."""
    doc_a = _doc(conn, title="Synthetic Doc A", content_hash="golden-a")
    doc_b = _doc(conn, title="Synthetic Doc B", content_hash="golden-b")
    for duration in (40, 60):
        record_search_query(
            conn,
            query=_QUERY_FREQUENT,
            result_count=3,
            session_id=None,
            source="cli",
            fts_count=2,
            duration_ms=duration,
            payload_tokens=900,
            baseline_tokens=1200,
        )
    record_search_query(
        conn,
        query=_QUERY_ONCE,
        result_count=0,
        session_id=None,
        source="cli",
        fts_count=0,
        duration_ms=25,
    )
    record_search_query(
        conn,
        query=_QUERY_MCP,
        result_count=2,
        session_id=_SESSION,
        source="mcp",
        fts_count=1,
        duration_ms=80,
        agent_id="agent-one",
        payload_tokens=400,
    )
    record_interaction(
        conn, document_id=doc_a, action="opened", source="mcp",
        session_id=_SESSION, agent_id="agent-one",
    )
    record_interaction(conn, document_id=doc_b, action="opened", source="cli")
    record_interaction(conn, document_id=doc_a, action="rated_useful", source="cli")
    record_interaction(conn, document_id=doc_b, action="pinned", source="ui")
    # Every row on one calendar day (today's midnight), so a run that crosses
    # midnight cannot split the daily table into two rows.
    for table, column in (
        ("search_queries", "at"),
        ("interactions", "at"),
        ("documents", "ingested_at"),
    ):
        conn.execute(f"UPDATE {table} SET {column} = date_trunc('day', NOW())")
    # One event outside the default 30-day window, so the window is exercised.
    record_interaction(conn, document_id=doc_b, action="opened", source="cli")
    conn.execute(
        "UPDATE interactions SET at = NOW() - make_interval(days => 45) "
        "WHERE at > date_trunc('day', NOW())"
    )


def _normalize(text: str) -> str:
    return _ISO_DATE.sub("<DATE>", _ANSI.sub("", text))


@pytest.fixture
def seeded_usage(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import TEST_DATABASE_URL

    _seed(test_db)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # ``_width`` rather than the ``width`` property: monkeypatch restores the
    # private attribute's original value (None, "size to the terminal"),
    # where restoring through the setter would pin the old measured width.
    monkeypatch.setattr(brain_format.console, "_width", _CONSOLE_WIDTH)


def _assert_matches_golden(name: str, output: str) -> None:
    golden = GOLDEN_DIR / name
    actual = _normalize(output)
    if not golden.exists():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual, encoding="utf-8")
        pytest.fail(f"golden {name} was missing and has been written; re-run")
    assert actual == golden.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("human.txt", ["usage"]),
        ("human_days7_limit2.txt", ["usage", "--days", "7", "--limit", "2"]),
        ("json.txt", ["usage", "--json"]),
        ("json_raw_queries.txt", ["usage", "--json", "--raw-queries"]),
    ],
)
def test_usage_without_the_flag_matches_the_pre_flag_golden(
    seeded_usage: None, name: str, argv: list[str]
) -> None:
    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0, result.output
    _assert_matches_golden(name, result.stdout)


def test_the_goldens_are_not_vacuous() -> None:
    """Each golden carries the fixture's real numbers, not an empty report."""
    human = (GOLDEN_DIR / "human.txt").read_text(encoding="utf-8")
    as_json = (GOLDEN_DIR / "json.txt").read_text(encoding="utf-8")

    assert "searches 4" in human
    assert "opens 2" in human
    assert "By agent" in human and "agent-one" in human
    assert '"searches": 4' in as_json
    assert _QUERY_FREQUENT not in as_json, "the default --json withholds queries"
    raw = (GOLDEN_DIR / "json_raw_queries.txt").read_text(encoding="utf-8")
    assert _QUERY_FREQUENT in raw
