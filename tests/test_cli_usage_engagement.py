"""``brain usage --engagement`` — the readout's CLI forms (RM-22a).

AC5: ``--days 7`` and ``--days 365`` print different counts but the same gate
line, which is always computed over 90 days. Also covers the human and
``--json`` forms, the ``--raw-queries`` refusal, and that neither form prints a
title, a query or a document id. AC4 (no flag, byte-identical) lives in
``tests/test_cli_usage_golden.py``.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import format as brain_format
from brain.cli import app
from brain.engagement import (
    GATE_MIN_DOCUMENTS,
    GATE_MIN_OPENS,
    GATE_MIN_RATINGS,
)
from brain.gaps import record_search_query
from brain.interactions import record_interaction

_TITLE = "Synthetic Quillfeather Note"
_QUERY = "synthetic quillfeather lookup"
_SESSION = uuid.UUID("00000000-0000-4000-8000-0000000000e7")


def _docs(conn: psycopg.Connection[Any], n: int) -> list[str]:
    ids = []
    for i in range(n):
        row = conn.execute(
            "INSERT INTO documents (title, content, content_type, kind, content_hash) "
            "VALUES (%s, 'body', 'note', 'vault', %s) RETURNING id::text",
            (f"{_TITLE} {i}", f"cli-eng-{i}"),
        ).fetchone()
        assert row is not None
        ids.append(str(row[0]))
    return ids


def _bulk(
    conn: psycopg.Connection[Any], *, action: str, count: int, documents: list[str],
    days_ago: int,
) -> None:
    conn.execute(
        """
        INSERT INTO interactions (document_id, action, source, at)
        SELECT (%s::uuid[])[1 + (g %% %s)], %s, 'cli',
               NOW() - make_interval(days => %s)
        FROM generate_series(0, %s - 1) AS g
        """,
        (documents, len(documents), action, days_ago, count),
    )


@pytest.fixture
def cli_db(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> psycopg.Connection[Any]:
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(brain_format.console, "_width", 120)
    return test_db


@pytest.fixture
def three_windows(cli_db: psycopg.Connection[Any]) -> list[str]:
    """Rows 3 days, 60 days and 200 days old: each window sees a different set.

    Within 90 days the gate is met exactly (500 reads over 100 documents,
    30 ratings), so ``--days 7`` (too few) and ``--days 365`` (more than
    enough) would each judge it differently if the gate followed ``--days``.
    """
    documents = _docs(cli_db, GATE_MIN_DOCUMENTS)
    _bulk(cli_db, action="opened", count=10, documents=documents, days_ago=3)
    _bulk(
        cli_db, action="opened", count=GATE_MIN_OPENS - 10, documents=documents,
        days_ago=60,
    )
    _bulk(
        cli_db, action="rated_useful", count=GATE_MIN_RATINGS, documents=documents,
        days_ago=60,
    )
    _bulk(cli_db, action="opened", count=700, documents=documents, days_ago=200)
    return documents


def _gate_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("Gate")]
    assert len(lines) == 1, output
    return lines[0]


def _invoke(*args: str) -> str:
    result = CliRunner().invoke(app, ["usage", "--engagement", *args])
    assert result.exit_code == 0, result.output
    return result.stdout


# ---------------------------------------------------------------------------
# AC5 — the gate line is always over 90 days
# ---------------------------------------------------------------------------


def test_days_changes_the_counts_but_not_the_gate_line(three_windows: list[str]) -> None:
    short = _invoke("--days", "7")
    long = _invoke("--days", "365")

    assert "opens 10 " in short
    assert "opens 1,200 " in long
    assert _gate_line(short) == _gate_line(long)
    assert "MET" in _gate_line(short) and "NOT MET" not in _gate_line(short)
    assert "reads 500 of 500" in _gate_line(short)


def test_json_days_changes_the_counts_but_not_the_gate(three_windows: list[str]) -> None:
    short = json.loads(_invoke("--days", "7", "--json"))
    long = json.loads(_invoke("--days", "365", "--json"))

    assert (short["days"], short["reads"]) == (7, 10)
    assert (long["days"], long["reads"]) == (365, 1200)
    assert short["gate"] == long["gate"]
    assert short["gate"]["met"] is True
    assert short["gate"]["days"] == 90


def test_the_default_window_judges_the_gate_from_the_same_counts(
    three_windows: list[str],
) -> None:
    """``--days`` defaults to 30 for the readout; the gate is still 90."""
    out = json.loads(_invoke("--json"))

    assert out["days"] == 30
    assert out["reads"] == 10
    assert out["gate"]["reads"] == GATE_MIN_OPENS


def test_ninety_days_reuses_the_readout_counts(three_windows: list[str]) -> None:
    out = json.loads(_invoke("--days", "90", "--json"))

    assert out["reads"] == out["gate"]["reads"] == GATE_MIN_OPENS
    assert out["documents_opened"] == out["gate"]["documents"] == GATE_MIN_DOCUMENTS
    assert out["ratings"] == out["gate"]["ratings"] == GATE_MIN_RATINGS


# ---------------------------------------------------------------------------
# The two forms
# ---------------------------------------------------------------------------


def test_human_readout_on_an_empty_table(cli_db: psycopg.Connection[Any]) -> None:
    out = _invoke()

    assert out.splitlines()[0] == "Brain engagement — last 30 day(s), document interactions"
    assert "opens 0  ·  clicks 0  ·  documents opened 0" in out
    assert "from a search session 0 of 0" in out and "%" not in out
    assert _gate_line(out) == (
        "Gate (always the last 90 days): NOT MET — reads 0 of 500 · "
        "documents 0 of 100 · ratings 0 of 30"
    )


def test_human_readout_reports_surfaces_ratings_share_and_size(
    cli_db: psycopg.Connection[Any],
) -> None:
    (a, b) = _docs(cli_db, 2)
    record_search_query(
        cli_db, query=_QUERY, result_count=1, session_id=_SESSION, source="mcp"
    )
    record_interaction(
        cli_db, document_id=a, action="opened", source="mcp", session_id=_SESSION
    )
    record_interaction(cli_db, document_id=b, action="opened", source="cli")
    record_interaction(cli_db, document_id=b, action="rated_irrelevant", source="cli")

    out = _invoke()

    assert "rated useful 0  ·  rated irrelevant 1" in out
    assert "from a search session 1 of 2 (50.0%)" in out
    assert "interactions table: 3 rows" in out
    assert "bytes" in out
    assert "By surface" in out and "mcp" in out and "cli" in out
    assert "documents 2 of 100" in _gate_line(out)


def test_json_reports_table_size(cli_db: psycopg.Connection[Any]) -> None:
    (a,) = _docs(cli_db, 1)
    record_interaction(cli_db, document_id=a, action="opened", source="ui")

    out = json.loads(_invoke("--json"))

    assert out["interactions_rows"] == 1
    assert out["interactions_bytes"] > 0
    assert out["by_surface"] == [{"surface": "ui", "opens": 1, "clicks": 0}]


def test_neither_form_prints_a_title_query_or_id(cli_db: psycopg.Connection[Any]) -> None:
    ids = _docs(cli_db, 2)
    record_search_query(
        cli_db, query=_QUERY, result_count=2, session_id=_SESSION, source="mcp"
    )
    for doc in ids:
        record_interaction(
            cli_db, document_id=doc, action="opened", source="mcp",
            session_id=_SESSION, query=_QUERY, agent_id="agent-synthetic",
        )

    for output in (_invoke(), _invoke("--json")):
        assert "Quillfeather" not in output and "quillfeather" not in output
        assert "agent-synthetic" not in output
        assert str(_SESSION) not in output
        for doc in ids:
            assert doc not in output


def test_raw_queries_is_refused_with_the_readout(cli_db: psycopg.Connection[Any]) -> None:
    """The readout has no query strings; the flag could only mislead."""
    result = CliRunner().invoke(app, ["usage", "--engagement", "--json", "--raw-queries"])

    assert result.exit_code == 2
    assert "--raw-queries" in result.output


def test_help_names_the_flag() -> None:
    result = CliRunner().invoke(app, ["usage", "--help"])

    assert result.exit_code == 0
    assert "--engagement" in result.output


@pytest.mark.fresh_schema
@pytest.mark.parametrize("table", ["interactions", "search_queries"])
def test_a_missing_table_is_a_clean_exit_with_the_init_hint(
    cli_db: psycopg.Connection[Any], table: str
) -> None:
    """Either table the readout reads, missing, names `brain init`, not a traceback."""
    cli_db.execute(f"DROP TABLE {table} CASCADE")

    result = CliRunner().invoke(app, ["usage", "--engagement"])

    assert result.exit_code == 1, result.output
    assert "brain init" in result.output
    assert "interactions or search_queries" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
