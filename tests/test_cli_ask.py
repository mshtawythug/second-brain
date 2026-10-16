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
from brain.embeddings import OllamaEmbedError, VoyageEmbedError
from brain.errors import EmbedError, OllamaContextOverflow, OllamaUnavailable

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


def test_cli_eval_answer_runs(
    test_db: psycopg.Connection,
    seed_doc: Callable[..., str],
    patch_embedder: Callable[[object], None],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The answer harness loads the committed corpus and runs ask_no_loop per
    # case. With a fake chat (no Ollama) + fake embedder it executes end-to-end;
    # we assert the command wiring + JSON shape, not the (model-dependent) score.
    seed_doc(
        title="Synthetic onboarding playbook",
        content="The synthetic onboarding playbook covers paired mentorship.",
    )
    patch_embedder(fake_embedder)
    monkeypatch.setattr(
        cli, "_build_chat", lambda cfg: _fake_chat_factory("paired mentorship [1].")
    )

    result = runner.invoke(cli.app, ["eval", "--answer", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "mean_fact_recall" in payload
    assert "mean_citation_count" in payload
    assert len(payload["scores"]) >= 10


def test_cli_eval_answer_ollama_unavailable(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    patch_embedder: Callable[[object], None],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        raise OllamaUnavailable("down")

    monkeypatch.setattr(cli, "_build_chat", lambda cfg: _dead_chat)

    result = runner.invoke(cli.app, ["eval", "--answer"])
    assert result.exit_code == 1
    assert "Ollama is not available" in result.output


def test_cli_eval_answer_context_overflow_exits_cleanly(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    patch_embedder: Callable[[object], None],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION (PR #32 audit): with ``truncate``/``shift`` off an over-window
    prompt is ``OllamaContextOverflow`` (it used to be truncated silently), and
    this handler caught only ``OllamaUnavailable`` -- so it escaped as a
    traceback. It must be a clean exit 1 naming the knob.
    """
    patch_embedder(fake_embedder)

    def _overflowing_chat(
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        raise OllamaContextOverflow(
            "Ollama refused a prompt larger than num_ctx=8192; "
            "raise BRAIN_OLLAMA_NUM_CTX or shrink the input"
        )

    monkeypatch.setattr(cli, "_build_chat", lambda cfg: _overflowing_chat)

    result = runner.invoke(cli.app, ["eval", "--answer"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert "BRAIN_OLLAMA_NUM_CTX" in result.output


class _QueryEmbedFails:
    """Protocol-conforming embedder whose QUERY embeds raise ``error``.

    Document embeds delegate, so the same instance could seed a corpus.
    """

    def __init__(self, inner: Any, error: EmbedError) -> None:
        self._inner = inner
        self._error = error
        self.dim = inner.dim

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        if input_type == "query":
            raise self._error
        return self._inner.embed(texts, input_type=input_type)  # type: ignore[no-any-return]

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("error", "advice"),
    [
        (VoyageEmbedError("synthetic 429", transient=True), "retry when the embedder is healthy"),
        (OllamaEmbedError("Ollama returned HTTP 404: gone"), "a retry will not fix"),
    ],
    ids=["transient", "permanent"],
)
def test_cli_eval_answer_embed_failure_exits_cleanly(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
    patch_embedder: Callable[[object], None],
    fake_embedder: object,
    monkeypatch: pytest.MonkeyPatch,
    error: EmbedError,
    advice: str,
) -> None:
    """REGRESSION (spec 2026-10-16 section 3.4): each case's retrieval step
    embeds, and the handler caught only chat errors, so any backend's embed
    failure escaped as a traceback. It must be a clean exit 1 whose advice
    matches the kind -- the harness aborts on a transient chat failure too."""
    patch_embedder(_QueryEmbedFails(fake_embedder, error))
    monkeypatch.setattr(
        cli, "_build_chat", lambda cfg: _fake_chat_factory("never reached [1].")
    )

    result = runner.invoke(cli.app, ["eval", "--answer"])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "eval --answer: the embedder failed" in result.output
    assert type(error).__name__ in result.output
    assert advice in result.output
