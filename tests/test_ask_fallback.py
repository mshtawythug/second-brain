"""``brain ask`` retrieves text-only when the query embed fails (RM-02).

``ask._retrieve_hybrid`` goes through :mod:`brain.search_fallback`, so an
unreachable embedder costs semantic ranking, not the answer. The fallback is
reported in ``AskResult.retrieval_fallback`` and in ``to_dict()``'s four
``fallback*`` keys; the answer prose is never edited. All data is synthetic.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from brain.ask import AskResult, Citation, _retrieve, ask, ask_no_loop
from brain.config import Config
from brain.embeddings import OllamaEmbedError
from brain.ingest import Embedder
from brain.search_fallback import FALLBACK_MODE, SearchFallback
from tests.embed_down import SECRET_DETAIL, BrokenEmbedder, QueryEmbedDownEmbedder

_TITLE = "Meadow survey note"
_FALLBACK_KEYS = ("fallback", "fallback_reason", "fallback_transient", "fallback_note")
#: Present only in the confidential body; never in the question.
_BODY_MARKER = "wrenfallow"


def _cfg() -> Config:
    return Config(database_url="postgresql://unused/none")


class _Chat:
    """Fake ``ChatJson``: plans ``sub_queries``, says coverage is sufficient, cites [1].

    Records every prompt so a test can see what retrieval handed the model.
    """

    def __init__(self, sub_queries: list[str] | None = None) -> None:
        self._sub_queries = sub_queries or ["meadow survey"]
        self.prompts: list[str] = []

    def __call__(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        if "sub_queries" in schema:
            return {"sub_queries": list(self._sub_queries)}
        if "sufficient" in schema:
            return {"sufficient": True, "follow_up_queries": []}
        return {"answer": "The survey counted wildflower plots [1]."}


class _AlternatingEmbedDown(QueryEmbedDownEmbedder):
    """Query embeds fail transiently first, then permanently (first-fallback rule)."""

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if input_type == "query":
            self.query_calls += 1
            raise OllamaEmbedError(SECRET_DETAIL, transient=self.query_calls == 1)
        return self._inner.embed(texts, input_type=input_type)


@pytest.fixture
def meadow(seed_doc: Callable[..., str]) -> str:
    """One note the text-only leg can find for 'meadow survey'; returns its id."""
    return seed_doc(
        title=_TITLE,
        content="The meadow survey counted the wildflower plots by section.",
    )


@pytest.mark.parametrize("transient", [True, False], ids=["transient", "permanent"])
def test_no_loop_answers_from_text_only_retrieval(
    test_db: psycopg.Connection[Any],
    meadow: str,
    fake_embedder: Embedder,
    transient: bool,
) -> None:
    """REGRESSION: an unreachable embedder used to traceback out of ``brain ask``."""
    # Arrange
    down = QueryEmbedDownEmbedder(fake_embedder, transient=transient)
    chat = _Chat()

    # Act
    result = ask_no_loop(
        test_db, _cfg(), embedder=down, chat=chat, question="meadow survey"
    )

    # Assert
    assert down.query_calls == 1
    assert result.retrieval_fallback is not None
    assert result.retrieval_fallback.transient is transient
    assert [c.document_id for c in result.citations] == [meadow]
    assert _TITLE in chat.prompts[-1]
    assert result.answer == "The survey counted wildflower plots [1]."
    payload = result.to_dict()
    assert payload["fallback"] == FALLBACK_MODE
    assert payload["fallback_transient"] is transient
    assert payload["fallback_used"] is True
    assert SECRET_DETAIL not in str(payload)


def test_full_loop_answers_from_text_only_retrieval(
    test_db: psycopg.Connection[Any], meadow: str, fake_embedder: Embedder
) -> None:
    # Arrange
    down = QueryEmbedDownEmbedder(fake_embedder, transient=True)

    # Act
    result = ask(
        test_db, _cfg(), embedder=down, chat=_Chat(), question="what did the survey count?"
    )

    # Assert
    assert result.retrieval_fallback is not None
    assert [c.document_id for c in result.citations] == [meadow]
    assert result.fallback_used is False
    assert result.to_dict()["fallback"] == FALLBACK_MODE


def test_the_first_fallback_across_sub_queries_is_kept(
    test_db: psycopg.Connection[Any], meadow: str, fake_embedder: Embedder
) -> None:
    # Arrange: two sub-queries; the first embed fails transiently, the second permanently.
    down = _AlternatingEmbedDown(fake_embedder, transient=True)
    chat = _Chat(sub_queries=["meadow survey", "wildflower plots"])

    # Act
    result = ask(test_db, _cfg(), embedder=down, chat=chat, question="q", max_iterations=1)

    # Assert
    assert down.query_calls == 2
    assert result.retrieval_fallback is not None
    assert result.retrieval_fallback.transient is True


def test_a_healthy_run_reports_no_fallback(
    test_db: psycopg.Connection[Any], meadow: str, fake_embedder: Embedder
) -> None:
    # Act
    result = ask_no_loop(
        test_db, _cfg(), embedder=fake_embedder, chat=_Chat(), question="meadow survey"
    )

    # Assert
    assert result.retrieval_fallback is None
    payload = result.to_dict()
    assert all(payload[key] is None for key in _FALLBACK_KEYS)


@pytest.mark.parametrize("exclude", [True, False], ids=["excluded", "included"])
def test_the_confidential_lens_reaches_the_fallback_leg(
    test_db: psycopg.Connection[Any],
    seed_doc: Callable[..., str],
    fake_embedder: Embedder,
    exclude: bool,
) -> None:
    """The text-only retry must carry ``sensitivity`` too, or it leaks into the prompt."""
    # Arrange
    conf_id = seed_doc(
        title="Confidential meadow memo",
        content=f"The meadow survey budget is filed under {_BODY_MARKER}.",
    )
    test_db.execute(
        "UPDATE documents SET sensitivity='confidential' WHERE id=%s", (conf_id,)
    )
    seed_doc(title=_TITLE, content="The meadow survey counted the wildflower plots.")
    chat = _Chat()

    # Act
    result = ask_no_loop(
        test_db,
        _cfg(),
        embedder=QueryEmbedDownEmbedder(fake_embedder, transient=True),
        chat=chat,
        question="meadow survey",
        exclude_confidential=exclude,
    )

    # Assert
    assert result.retrieval_fallback is not None
    assert (_BODY_MARKER in chat.prompts[-1]) is not exclude


def test_a_non_embed_error_is_not_swallowed(
    test_db: psycopg.Connection[Any], meadow: str, fake_embedder: Embedder
) -> None:
    # Act / Assert
    with pytest.raises(RuntimeError, match="synthetic programming error"):
        ask_no_loop(
            test_db,
            _cfg(),
            embedder=BrokenEmbedder(fake_embedder, transient=True),
            chat=_Chat(),
            question="meadow survey",
        )


def test_a_graph_leg_embed_failure_still_propagates(
    monkeypatch: pytest.MonkeyPatch, fake_embedder: Embedder
) -> None:
    """Only the hybrid leg falls back; a graph mode's own embed failure is the caller's."""
    # Arrange
    def _graph_down(*args: Any, **kwargs: Any) -> Any:
        raise OllamaEmbedError(SECRET_DETAIL, transient=True)

    monkeypatch.setattr("brain.ask._retrieve_hybrid", _healthy_hybrid)
    monkeypatch.setattr("brain.graph_rag.graph_rag_search", _graph_down)

    # Act / Assert
    with pytest.raises(OllamaEmbedError):
        _retrieve(
            None,  # type: ignore[arg-type]
            _cfg(),
            embedder=fake_embedder,
            query="q",
            limit=5,
            mode="local",
            backend=object(),  # type: ignore[arg-type]
        )


def _healthy_hybrid(*args: Any, **kwargs: Any) -> Any:
    from brain.search_fallback import FallbackSearch

    return FallbackSearch(results=[], fallback=None)


def test_to_dict_merges_the_four_keys_and_keeps_fallback_used() -> None:
    # Arrange
    fallback = SearchFallback(reason="OllamaEmbedError", transient=True)
    result = AskResult(
        answer="a [1]",
        citations=[Citation(1, "doc-1", "T", "manual", "snip")],
        iterations_used=1,
        sub_queries=["a"],
        fallback_used=True,
        session_id="s",
        retrieval_fallback=fallback,
    )

    # Act
    payload = result.to_dict()

    # Assert
    assert payload["fallback"] == FALLBACK_MODE
    assert payload["fallback_reason"] == "OllamaEmbedError"
    assert payload["fallback_transient"] is True
    assert payload["fallback_note"] == fallback.note
    assert payload["fallback_used"] is True
    assert payload["answer"] == "a [1]"


@pytest.mark.parametrize("no_loop", [True, False], ids=["no-loop", "loop"])
def test_fallback_false_re_raises_the_embed_error(
    test_db: psycopg.Connection[Any],
    meadow: str,  # noqa: ARG001 — text-only WOULD find it, so a raise is no fallback
    fake_embedder: Embedder,
    no_loop: bool,
) -> None:
    """``brain eval --answer`` passes ``fallback=False`` (spec §2.7): eval never falls back.

    Pins the opt-out so the eval path cannot silently start scoring text-only answers.
    """
    # Arrange
    down = QueryEmbedDownEmbedder(fake_embedder, transient=True)

    # Act / Assert
    with pytest.raises(OllamaEmbedError):
        ask(
            test_db,
            _cfg(),
            embedder=down,
            chat=_Chat(),
            question="meadow survey",
            no_loop=no_loop,
            fallback=False,
        )
    assert down.query_calls == 1


class _SecondQueryDown(QueryEmbedDownEmbedder):
    """The first query embed succeeds; every later one fails transiently."""

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if input_type == "query":
            self.query_calls += 1
            if self.query_calls > 1:
                raise OllamaEmbedError(SECRET_DETAIL, transient=True)
        return self._inner.embed(texts, input_type=input_type)


def test_a_healthy_sub_query_does_not_switch_the_fallback_off_for_the_next(
    test_db: psycopg.Connection[Any], meadow: str, fake_embedder: Embedder
) -> None:
    """REGRESSION: the loop's per-leg fallback once shadowed the ``fallback`` flag.

    A healthy first leg returned ``None``, which became the flag for the second
    leg, so its embed failure raised instead of falling back.
    """
    # Arrange
    down = _SecondQueryDown(fake_embedder, transient=True)
    chat = _Chat(sub_queries=["meadow survey", "wildflower plots"])

    # Act
    result = ask(test_db, _cfg(), embedder=down, chat=chat, question="q", max_iterations=1)

    # Assert
    assert down.query_calls == 2
    assert result.retrieval_fallback is not None
    assert meadow in {c.document_id for c in result.citations}
