"""``brain ask``: text-only retrieval when the embedder is down, and clean failures (RM-02).

Covers the ticket's acceptance criteria 1-6 through the real Typer app. The
fallback notice goes to stderr, the ``Retrieval: text-only`` line goes under the
answer, and every failure is one ``ask: <fixed sentence>`` line on stderr with
exit 1: never a traceback, never an exception message. All data is synthetic.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from typer.testing import CliRunner

import brain.cli as cli
from brain.config import Config
from brain.embeddings import OllamaEmbedError
from brain.errors import (
    ChatOutputTruncated,
    EnrichmentError,
    GraphBackendError,
    GraphTenantError,
    OllamaContextOverflow,
    OllamaUnavailable,
)
from brain.ingest import Embedder
from tests.embed_down import SECRET_DETAIL, QueryEmbedDownEmbedder

runner = CliRunner()

_NOTICE = "text-only (FTS) matches"
_RESULT_LINE = "Retrieval: text-only (semantic ranking unavailable)"
_ANSWER = "The survey counted wildflower plots [1]."
#: Placed in every raised exception's message; it must never be printed.
_SENTINEL = "synthetic-ask-detail-91c2"


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
        return {"sub_queries": ["meadow survey"]}
    if "sufficient" in schema:
        return {"sufficient": True, "follow_up_queries": []}
    return {"answer": _ANSWER}


def _failing_chat(exc: BaseException) -> Callable[..., dict[str, Any]]:
    def _chat_down(prompt: str, **kwargs: Any) -> dict[str, Any]:
        raise exc

    return _chat_down


@pytest.fixture
def meadow(seed_doc: Callable[..., str]) -> str:
    return seed_doc(
        title="Meadow survey note",
        content="The meadow survey counted the wildflower plots by section.",
    )


@pytest.fixture
def embed_down(
    meadow: str,  # noqa: ARG001 — seeds the corpus first
    fake_embedder: Embedder,
    patch_embedder: Callable[[object], None],
) -> QueryEmbedDownEmbedder:
    down = QueryEmbedDownEmbedder(fake_embedder, transient=True)
    patch_embedder(down)
    return down


@pytest.mark.parametrize("loop_args", [["--no-loop"], []], ids=["no-loop", "loop"])
def test_embedder_down_chat_up_answers_text_only(
    embed_down: QueryEmbedDownEmbedder,
    monkeypatch: pytest.MonkeyPatch,
    loop_args: list[str],
) -> None:
    """REGRESSION (criteria 1, 2): the query embed used to traceback out of ``ask``."""
    # Arrange
    monkeypatch.setattr(cli, "_build_chat", lambda cfg: _chat)

    # Act
    result = runner.invoke(cli.app, ["ask", "meadow survey", *loop_args])

    # Assert
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert _ANSWER in result.stdout
    assert _RESULT_LINE in result.stdout
    assert result.stdout.index(_ANSWER) < result.stdout.index(_RESULT_LINE)
    assert _NOTICE in result.stderr
    assert _NOTICE not in result.stdout
    assert SECRET_DETAIL not in result.output
    assert embed_down.query_calls == 1


@pytest.mark.parametrize("loop_args", [["--no-loop"], []], ids=["no-loop", "loop"])
def test_embedder_down_json_carries_the_fallback_keys(
    embed_down: QueryEmbedDownEmbedder,  # noqa: ARG001 — installs the embedder
    monkeypatch: pytest.MonkeyPatch,
    loop_args: list[str],
) -> None:
    # Arrange
    monkeypatch.setattr(cli, "_build_chat", lambda cfg: _chat)

    # Act
    result = runner.invoke(cli.app, ["ask", "meadow survey", "--json", *loop_args])

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["fallback"] == "fts_only"
    assert payload["fallback_transient"] is True
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert _NOTICE in result.stderr


def test_a_healthy_run_prints_no_fallback_notice(
    meadow: str,  # noqa: ARG001 — seeds the corpus
    fake_embedder: Embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    patch_embedder(fake_embedder)
    monkeypatch.setattr(cli, "_build_chat", lambda cfg: _chat)

    # Act
    result = runner.invoke(cli.app, ["ask", "meadow survey", "--no-loop"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "Retrieval:" not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("loop_args", [["--no-loop"], []], ids=["no-loop", "loop"])
def test_embedder_and_chat_down_exit_1_with_the_ollama_sentence_only(
    embed_down: QueryEmbedDownEmbedder,  # noqa: ARG001 — installs the embedder
    monkeypatch: pytest.MonkeyPatch,
    loop_args: list[str],
) -> None:
    """Criterion 3: one clean line, empty stdout, no traceback."""
    # Arrange
    monkeypatch.setattr(
        cli, "_build_chat", lambda cfg: _failing_chat(OllamaUnavailable(_SENTINEL))
    )

    # Act
    result = runner.invoke(cli.app, ["ask", "meadow survey", *loop_args])

    # Assert
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.stdout == ""
    assert result.stderr.strip().splitlines() == [
        "ask: Ollama is not available. Start it (for example `brew services start "
        "ollama`) and retry."
    ]


@pytest.mark.parametrize(
    ("exc", "sentence"),
    [
        (OllamaUnavailable(_SENTINEL), "Ollama is not available"),
        (OllamaContextOverflow(_SENTINEL), "did not fit the chat model's context window"),
        (
            ChatOutputTruncated(_SENTINEL, partial=_SENTINEL, num_predict=512),
            "cut off at its output budget",
        ),
        (EnrichmentError(_SENTINEL), "answer synthesis failed (EnrichmentError)"),
    ],
    ids=["unavailable", "context-overflow", "output-truncated", "enrichment"],
)
def test_each_chat_failure_gets_its_own_class_only_sentence(
    meadow: str,  # noqa: ARG001 — seeds the corpus
    fake_embedder: Embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    sentence: str,
) -> None:
    """Criteria 5 and 6: the specific sentence, and never the exception message."""
    # Arrange
    patch_embedder(fake_embedder)
    monkeypatch.setattr(cli, "_build_chat", lambda cfg: _failing_chat(exc))

    # Act
    result = runner.invoke(cli.app, ["ask", "meadow survey", "--no-loop"])

    # Assert
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.stderr.startswith("ask: ")
    assert sentence in result.stderr
    assert _SENTINEL not in result.output


@pytest.mark.parametrize(
    ("exc", "sentence"),
    [
        (
            OllamaEmbedError(_SENTINEL, transient=True),
            "the embedding model is unavailable; retry later",
        ),
        (
            OllamaEmbedError(_SENTINEL, transient=False),
            "the embedding model is not usable (not pulled, or a bad key)",
        ),
        (GraphTenantError(_SENTINEL), "graph retrieval failed (GraphTenantError)"),
        (GraphBackendError(_SENTINEL), "graph retrieval failed (GraphBackendError)"),
    ],
    ids=["embed-transient", "embed-permanent", "graph-tenant", "graph-backend"],
)
def test_a_graph_leg_failure_exits_1_with_its_sentence(
    meadow: str,  # noqa: ARG001 — seeds the corpus
    fake_embedder: Embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    sentence: str,
) -> None:
    """Criteria 4 and 6: a graph mode embeds outside the hybrid leg, which does not fall back."""
    # Arrange
    patch_embedder(fake_embedder)
    monkeypatch.setattr(cli, "_build_chat", lambda cfg: _chat)

    def _graph_down(*args: Any, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr("brain.graph_rag.graph_rag_search", _graph_down)

    # Act
    result = runner.invoke(
        cli.app, ["ask", "meadow survey", "--no-loop", "--mode", "local"]
    )

    # Assert
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.stdout == ""
    assert result.stderr.startswith("ask: ")
    assert sentence in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1
    assert _SENTINEL not in result.output
