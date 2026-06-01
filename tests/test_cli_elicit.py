"""Tests for the `brain elicit` sub-app and `brain elicit list` command."""
from __future__ import annotations

import json
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _seed_gap(
    conn: psycopg.Connection,
    *,
    signal_kind: str = "delta",
    target_type: str = "org",
    target_id: str = "ent-001",
    score: float = 0.016,
    status: str = "surfaced",
) -> None:
    """Insert a single row into elicitation_gaps for CLI tests."""
    conn.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, rationale, status) "
        "VALUES ('default', %s, %s, %s, %s, %s, %s, %s)",
        (
            signal_kind,
            target_type,
            target_id,
            score,
            ["doc-a", "doc-b", "doc-c"],
            "Referenced in 3 ingested docs but never authored.",
            status,
        ),
    )


# ---------------------------------------------------------------------------
# Sub-app registration smoke test
# ---------------------------------------------------------------------------


def test_elicit_help_registered() -> None:
    """brain elicit --help exits 0 and mentions the sub-app."""
    result = CliRunner().invoke(app, ["elicit", "--help"])
    assert result.exit_code == 0, result.output
    assert "elicit" in result.output.lower()


# ---------------------------------------------------------------------------
# brain elicit list — empty queue
# ---------------------------------------------------------------------------


def test_elicit_list_empty(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """list prints a 'no open gaps' message when the queue is empty."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["elicit", "list"])
    assert result.exit_code == 0, result.output
    assert "no open gaps" in result.output.lower()


# ---------------------------------------------------------------------------
# brain elicit list — surfaced gaps appear
# ---------------------------------------------------------------------------


def test_elicit_list_shows_surfaced_gap(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """list displays the target_id and signal_kind of a surfaced gap."""
    _seed_gap(test_db, target_id="ent-xyz", signal_kind="delta")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["elicit", "list"])
    assert result.exit_code == 0, result.output
    assert "ent-xyz" in result.output
    assert "delta" in result.output


# ---------------------------------------------------------------------------
# brain elicit list --json
# ---------------------------------------------------------------------------


def test_elicit_list_json(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """--json returns a valid JSON array with the gap fields."""
    _seed_gap(test_db, target_id="ent-json", signal_kind="orphan", score=0.012)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["elicit", "list", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["target_id"] == "ent-json"
    assert data[0]["signal_kind"] == "orphan"


# ---------------------------------------------------------------------------
# brain elicit list — snoozed gaps excluded
# ---------------------------------------------------------------------------


def test_elicit_list_excludes_active_snoozed(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """Snoozed gaps whose snoozed_until is in the future are hidden."""
    test_db.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, rationale, "
        " status, snoozed_until) "
        "VALUES ('default', 'delta', 'org', 'ent-snoozed', 0.016, %s, 'rationale', "
        "'snoozed', now() + interval '7 days')",
        (["doc-a"],),
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["elicit", "list"])
    assert result.exit_code == 0, result.output
    assert "ent-snoozed" not in result.output
    assert "no open gaps" in result.output.lower()
