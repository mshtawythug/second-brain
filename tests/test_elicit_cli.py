"""Tests for the `brain elicit list` CLI command."""
from __future__ import annotations

import json
import os

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

# psycopg3's conn.info.dsn strips the password (security feature); use the
# explicit test URL (which includes credentials) as every other CLI test does.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def test_elicit_list_empty(test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """brain elicit list --json returns an empty JSON array when the queue is empty."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit", "list", "--json"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip().startswith("[")
    data = json.loads(res.stdout)
    assert data == []


def test_elicit_list_text_empty(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """brain elicit list (text mode) prints 'no open gaps' on empty queue."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit", "list"])
    assert res.exit_code == 0, res.output
    assert "no open gaps" in res.output.lower()


def test_elicit_default_empty_queue_exits_clean(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`brain elicit` with an empty queue never drafts (no Ollama) and exits 0.

    With no gaps the session loop has nothing to draft, so the command returns
    before any enricher call — keeping this test deterministic and offline.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit"], input="q\n")
    assert res.exit_code == 0, res.output
    assert "no open gaps" in res.output.lower()


def test_elicit_default_rejects_unknown_signal(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown --signal value fails fast with a BadParameter (exit 2)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit", "--signal", "bogus"])
    assert res.exit_code == 2, res.output
    # Rich wraps the BadParameter panel across lines, so assert on tokens that
    # survive wrapping rather than the full comma-joined phrase.
    assert "signal" in res.output.lower()
    assert "delta" in res.output


def test_elicit_list_still_works_under_default_callback(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding the default callback must not shadow the `list` subcommand."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit", "list", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == []


def test_elicit_list_bad_min_gap_score_clean_error(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-range BRAIN_ELICIT_MIN_GAP_SCORE yields a clean error, not a traceback.

    Regression: `brain elicit list` called `Config.load()` bare, so a bad
    elicit knob propagated as an uncaught ConfigError and printed a raw Rich
    traceback. The command must now catch it, print the ConfigError message,
    and exit non-zero with no leaked exception.

    The message must land on stderr (via `typer.echo(..., err=True)`) and must
    NOT appear silently on stdout.  Click 8.3+ always captures stdout and stderr
    separately; `result.stderr` / `result.stdout` are always populated.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_ELICIT_MIN_GAP_SCORE", "1.5")
    res = CliRunner().invoke(app, ["elicit", "list"])
    assert res.exit_code != 0, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception
    assert "BRAIN_ELICIT_MIN_GAP_SCORE must be a float in [0.0, 1.0]" in res.stderr
    assert "BRAIN_ELICIT_MIN_GAP_SCORE must be a float in [0.0, 1.0]" not in res.stdout


def test_elicit_default_bad_min_gap_score_clean_error(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default `brain elicit` session also surfaces a clean ConfigError.

    The message must land on stderr (via `typer.echo(..., err=True)`) and must
    NOT appear silently on stdout.  Click 8.3+ always captures stdout and stderr
    separately; `result.stderr` / `result.stdout` are always populated.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_ELICIT_MIN_GAP_SCORE", "1.5")
    res = CliRunner().invoke(app, ["elicit"], input="q\n")
    assert res.exit_code != 0, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception
    assert "BRAIN_ELICIT_MIN_GAP_SCORE must be a float in [0.0, 1.0]" in res.stderr
    assert "BRAIN_ELICIT_MIN_GAP_SCORE must be a float in [0.0, 1.0]" not in res.stdout


def _seed_org_and_tool_gaps(conn: psycopg.Connection) -> str:
    """Seed one org gap (with a resolvable entity name) + one tool gap.

    Returns the org entity's UUID (also the org gap's target_id).
    """
    entity_id = conn.execute(
        "INSERT INTO graph_entities "
        "(tenant_id, entity_type, name, canonical_key, description, doc_count) "
        "VALUES ('default', 'org', 'Acme', 'acme', 'desc', 3) RETURNING id"
    ).fetchone()[0]  # type: ignore[index]
    conn.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, "
        "rationale, status) "
        "VALUES ('default','delta','org',%s,0.9,ARRAY['d1','d2','d3'],'r','surfaced')",
        (str(entity_id),),
    )
    conn.execute(
        "INSERT INTO elicitation_gaps "
        "(tenant_id, signal_kind, target_type, target_id, score, evidence_ids, "
        "rationale, status) "
        "VALUES ('default','delta','tool','tool-target',0.9,"
        "ARRAY['d1','d2','d3'],'r','surfaced')"
    )
    return str(entity_id)


def test_elicit_list_type_filter_json(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--type org returns only org rows, each carrying a target_name key."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    entity_id = _seed_org_and_tool_gaps(test_db)

    res = CliRunner().invoke(app, ["elicit", "list", "--type", "org", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert [row["target_type"] for row in data] == ["org"]
    assert data[0]["target_id"] == entity_id
    assert data[0]["target_name"] == "Acme"
    assert all("target_name" in row for row in data)


def test_elicit_list_rejects_unknown_type(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown --type value fails fast with a BadParameter (exit 2)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    res = CliRunner().invoke(app, ["elicit", "list", "--type", "bogus", "--json"])
    assert res.exit_code != 0, res.output
    assert "bogus" in res.output
