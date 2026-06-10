"""CLI tests for ``brain review weekly`` (Plan 10).

Drives the Typer command with ``CliRunner`` against the real test DB. The
``_build_enricher`` factory is patched to ``None`` so no test ever contacts
Ollama (the graph synthesis path is best-effort and exercised in
``test_review_weekly``). Data is seeded into the *current* ISO week so the
default-``--week`` path has activity to render.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_enricher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_build_enricher", lambda cfg: None)


def _interact_now(conn: psycopg.Connection, doc_id: str) -> None:
    # ``at`` defaults to NOW() → lands in the current ISO week window.
    conn.execute(
        "INSERT INTO interactions (document_id, action, source) "
        "VALUES (%s, 'opened', 'cli')",
        (doc_id,),
    )


def test_review_weekly_no_emit_outputs_title(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    doc = seed_doc(title="Current week doc", content="cw body", tags=["topic-alpha"])
    _interact_now(test_db, doc)

    result = runner.invoke(cli.app, ["review", "weekly", "--no-emit", "--no-graph"])

    assert result.exit_code == 0, result.stdout
    assert "Weekly review" in result.stdout


def test_review_weekly_json_week_field(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
) -> None:
    result = runner.invoke(
        cli.app,
        ["review", "weekly", "--week", "2026-W01", "--no-graph", "--json"],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["week"] == "2026-W01"
    assert payload["vault_path"] == "reviews/2026-W01"
    assert "sections" in payload


def test_review_weekly_bad_week_format_errors(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
) -> None:
    result = runner.invoke(
        cli.app, ["review", "weekly", "--week", "bad-format", "--no-graph"]
    )
    assert result.exit_code != 0
    assert "YYYY-Www" in result.output


def test_review_weekly_empty_week_reports_no_activity(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
) -> None:
    result = runner.invoke(
        cli.app,
        ["review", "weekly", "--week", "2026-W01", "--no-graph", "--no-emit"],
    )
    assert result.exit_code == 0, result.stdout
    assert "No activity found for 2026-W01." in result.stdout


def test_review_weekly_empty_week_still_emits_by_default(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default (no --no-emit) writes the page even for an empty week — matching
    # the default-emit contract + MCP parity. Regression guard for the Codex fix.
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    result = runner.invoke(
        cli.app, ["review", "weekly", "--week", "2026-W01", "--no-graph"]
    )
    assert result.exit_code == 0, result.stdout
    assert "No activity found for 2026-W01." in result.stdout
    written = tmp_path / "reviews" / "2026-W01.md"
    assert written.is_file()
    assert written.read_text(encoding="utf-8").startswith("---\n")
