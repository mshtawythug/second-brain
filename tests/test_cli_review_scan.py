"""CLI tests for ``brain review scan|list|dismiss`` (Plan 03).

Drives the Typer commands with ``CliRunner`` against the real test DB. Seeding
helpers are shared with :mod:`tests.test_review`. No test contacts Ollama: the
conflict path patches ``_build_enricher`` / ``_ollama_reachable``; the staleness
path needs no LLM at all.
"""
from __future__ import annotations

import json
import math

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli
from brain.review import queries

from .test_review import (
    _FakeEnricher,
    _insert_doc,
    _insert_entity,
    _seed_conflict_entity,
)

runner = CliRunner()
_TENANT = "default"


@pytest.fixture(autouse=True)
def _wire_fakes(monkeypatch: pytest.MonkeyPatch, fake_embedder: object) -> None:
    monkeypatch.setattr(cli, "_build_embedder", lambda cfg: fake_embedder)


def _seed_stale_pair(conn: psycopg.Connection) -> str:
    old = _insert_doc(
        conn,
        title="Compensation ranges — synthetic role",
        summary="old comp",
        ingested_days_ago=400,
        embedding=[1.0, 0.0],
    )
    new = _insert_doc(
        conn,
        title="Updated salary bands — synthetic",
        summary="new comp",
        ingested_days_ago=15,
        embedding=[0.7, math.sqrt(0.51)],
    )
    _insert_entity(conn, canonical_key="comp", name="Compensation", doc_ids=[old, new])
    return old


def test_review_scan_stale_dry_run(test_db: psycopg.Connection) -> None:
    _seed_stale_pair(test_db)
    result = runner.invoke(cli.app, ["review", "scan", "--stale", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "STALE" in result.stdout
    count = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps WHERE signal_kind = 'stale'"
    ).fetchone()[0]
    assert count == 0  # dry-run writes nothing


def test_review_scan_stale_writes(test_db: psycopg.Connection) -> None:
    old = _seed_stale_pair(test_db)
    result = runner.invoke(cli.app, ["review", "scan", "--stale"])
    assert result.exit_code == 0, result.stdout
    row = test_db.execute(
        "SELECT target_id FROM elicitation_gaps WHERE signal_kind = 'stale'"
    ).fetchone()
    assert row[0] == old


def test_review_scan_conflicts_gated(test_db: psycopg.Connection) -> None:
    _seed_conflict_entity(test_db)
    # Default config: contradiction detection disabled -> warning + exit 0.
    result = runner.invoke(cli.app, ["review", "scan", "--conflicts"])
    assert result.exit_code == 0, result.stdout
    # The gating notice is a warning on stderr.
    assert "disabled" in (result.stdout + result.stderr)
    count = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps WHERE signal_kind = 'contradiction'"
    ).fetchone()[0]
    assert count == 0


def test_review_scan_conflicts_dry_run(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_conflict_entity(test_db)
    monkeypatch.setenv("BRAIN_ELICIT_CONTRADICTION_ENABLED", "true")
    monkeypatch.setenv("BRAIN_ELICIT_CONTRADICTION_MIN_DOCS", "2")
    monkeypatch.setattr(cli, "_ollama_reachable", lambda cfg: True)
    monkeypatch.setattr(cli, "_build_enricher", lambda cfg: _FakeEnricher())

    result = runner.invoke(
        cli.app, ["review", "scan", "--conflicts", "--dry-run"]
    )
    assert result.exit_code == 0, result.stdout
    assert "CONFLICT" in result.stdout
    count = test_db.execute(
        "SELECT count(*) FROM elicitation_gaps WHERE signal_kind = 'contradiction'"
    ).fetchone()[0]
    assert count == 0


def test_review_scan_json(test_db: psycopg.Connection) -> None:
    _seed_stale_pair(test_db)
    result = runner.invoke(
        cli.app, ["review", "scan", "--stale", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.stdout
    line = result.stdout.strip().splitlines()[0]
    payload = json.loads(line)
    assert payload["kind"] == "stale"
    assert "evidence_ids" in payload


def test_review_list_empty(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
) -> None:
    result = runner.invoke(cli.app, ["review", "list"])
    assert result.exit_code == 0, result.stdout
    assert "No findings in review queue." in result.stdout


def test_review_list_shows_findings(test_db: psycopg.Connection) -> None:
    queries.upsert_review_finding(
        test_db,
        tenant_id=_TENANT,
        signal_kind="stale",
        target_type="doc",
        target_id="doc-z",
        score=0.8,
        evidence_ids=["doc-z", "doc-y"],
        rationale="aged note",
    )
    result = runner.invoke(cli.app, ["review", "list", "--kind", "stale"])
    assert result.exit_code == 0, result.stdout
    assert "STALE" in result.stdout


def test_review_list_bad_kind(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
) -> None:
    result = runner.invoke(cli.app, ["review", "list", "--kind", "bogus"])
    assert result.exit_code != 0


def test_review_dismiss(test_db: psycopg.Connection) -> None:
    queries.upsert_review_finding(
        test_db,
        tenant_id=_TENANT,
        signal_kind="stale",
        target_type="doc",
        target_id="doc-d",
        score=0.7,
        evidence_ids=["doc-d", "doc-e"],
        rationale="aged",
    )
    finding_id = test_db.execute(
        "SELECT id::text FROM elicitation_gaps WHERE target_id = 'doc-d'"
    ).fetchone()[0]

    result = runner.invoke(cli.app, ["review", "dismiss", finding_id[:8]])
    assert result.exit_code == 0, result.stdout
    status = test_db.execute(
        "SELECT status FROM elicitation_gaps WHERE id = %s::uuid", (finding_id,)
    ).fetchone()[0]
    assert status == "dismissed"
    # Idempotent.
    again = runner.invoke(cli.app, ["review", "dismiss", finding_id[:8]])
    assert again.exit_code == 0, again.stdout


def test_review_dismiss_no_match(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
) -> None:
    result = runner.invoke(cli.app, ["review", "dismiss", "ffffffff"])
    assert result.exit_code != 0
