"""CLI tests for `brain ask` (Plan 06, Phase 1)."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

import brain.cli as cli
from brain.config import Config
from brain.errors import OllamaUnavailable

runner = CliRunner()


def _fake_chat_factory(answer: str) -> Callable[..., dict[str, Any]]:
    """Build a fake ``ChatJson`` that returns a fixed synthesize answer.

    The plan/reflect steps also route through it; returning a body keyed by the
    requested schema keeps the no-loop path (synthesize only) simple while still
    satisfying the looped path's schema checks if exercised.
    """

    def _chat(
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if "sub_queries" in schema:
            return {"sub_queries": ["synthetic"]}
        if "sufficient" in schema:
            return {"sufficient": True, "follow_up_queries": []}
        return {"answer": answer}

    return _chat


def test_cli_ask_command_runs(
    test_db: psycopg.Connection,
    seed_doc: Callable[..., str],
    patch_embedder: Callable[[object], None],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: a real ingested doc + fake embedder + fake chat (no Ollama).
    doc_id = seed_doc(
        title="Synthetic onboarding playbook",
        content="The synthetic onboarding playbook covers paired mentorship.",
    )
    patch_embedder(fake_embedder)
    monkeypatch.setattr(
        cli, "_build_chat", lambda cfg: _fake_chat_factory("Uses mentorship [1].")
    )

    # Act
    result = runner.invoke(
        cli.app, ["ask", "synthetic onboarding playbook", "--no-loop", "--json"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "answer" in payload
    assert "citations" in payload
    assert payload["fallback_used"] is True
    assert any(c["document_id"] == doc_id for c in payload["citations"])


def test_cli_ask_human_output(
    test_db: psycopg.Connection,
    seed_doc: Callable[..., str],
    patch_embedder: Callable[[object], None],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_doc(
        title="Synthetic onboarding playbook",
        content="The synthetic onboarding playbook covers paired mentorship.",
    )
    patch_embedder(fake_embedder)
    monkeypatch.setattr(
        cli, "_build_chat", lambda cfg: _fake_chat_factory("Uses mentorship [1].")
    )

    result = runner.invoke(
        cli.app, ["ask", "synthetic onboarding playbook", "--no-loop", "--explain"]
    )
    assert result.exit_code == 0, result.output
    assert "Answer" in result.output
    assert "Sources" in result.output
    assert "fast mode" in result.output


def test_cli_ask_logs_interactions(
    test_db: psycopg.Connection,
    seed_doc: Callable[..., str],
    patch_embedder: Callable[[object], None],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_id = seed_doc(
        title="Synthetic onboarding playbook",
        content="The synthetic onboarding playbook covers paired mentorship.",
    )
    patch_embedder(fake_embedder)
    monkeypatch.setattr(
        cli, "_build_chat", lambda cfg: _fake_chat_factory("Uses mentorship [1].")
    )

    result = runner.invoke(
        cli.app, ["ask", "synthetic onboarding playbook", "--no-loop"]
    )
    assert result.exit_code == 0, result.output

    rows = test_db.execute(
        "SELECT document_id, action, source FROM interactions WHERE document_id = %s",
        (doc_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "opened"
    assert rows[0][2] == "cli"


def test_cli_ask_ollama_unavailable(
    test_db: psycopg.Connection,
    seed_doc: Callable[..., str],
    patch_embedder: Callable[[object], None],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_doc(title="Synthetic doc", content="synthetic content body here.")
    patch_embedder(fake_embedder)

    def _dead_chat(
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        raise OllamaUnavailable("Ollama unreachable: connection refused")

    monkeypatch.setattr(cli, "_build_chat", lambda cfg: _dead_chat)

    result = runner.invoke(cli.app, ["ask", "synthetic doc", "--no-loop"])
    assert result.exit_code == 1
    assert "Ollama is not available" in result.output


def test_cli_ask_bad_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/none")
    result = runner.invoke(cli.app, ["ask", "q", "--mode", "bogus"])
    assert result.exit_code != 0
    assert "mode must be one of" in result.output
