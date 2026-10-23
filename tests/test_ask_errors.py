"""``brain.ask_errors``: one fixed, class-only sentence per ``ask`` failure family (RM-02).

Every exception here is raised with :data:`_SENTINEL` in its message, so each
test also proves the message never reaches the sentence. All data is synthetic.
"""
from __future__ import annotations

import pytest

from brain.ask_errors import ASK_FAILURES, describe_ask_failure
from brain.embeddings import OllamaEmbedError, VoyageEmbedError
from brain.errors import (
    ChatOutputTruncated,
    EmbedError,
    EnrichmentError,
    GraphBackendError,
    GraphTenantError,
    OllamaContextOverflow,
    OllamaUnavailable,
)

_SENTINEL = "synthetic-ask-detail-91c2"


def _truncated() -> ChatOutputTruncated:
    return ChatOutputTruncated(_SENTINEL, partial=_SENTINEL, num_predict=512)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (OllamaUnavailable(_SENTINEL), "Ollama is not available"),
        (OllamaContextOverflow(_SENTINEL), "did not fit the chat model's context window"),
        (_truncated(), "cut off at its output budget"),
        (EnrichmentError(_SENTINEL), "answer synthesis failed (EnrichmentError)"),
        (GraphTenantError(_SENTINEL), "graph retrieval failed (GraphTenantError)"),
        (GraphBackendError(_SENTINEL), "graph retrieval failed (GraphBackendError)"),
        (OllamaEmbedError(_SENTINEL, transient=True), "the embedding model is unavailable"),
        (VoyageEmbedError(_SENTINEL, transient=False), "run `brain doctor`"),
    ],
    ids=[
        "ollama-unavailable",
        "context-overflow",
        "output-truncated",
        "enrichment",
        "graph-tenant",
        "graph-backend",
        "embed-transient",
        "embed-permanent",
    ],
)
def test_each_family_gets_its_own_fixed_sentence(
    exc: BaseException, expected: str
) -> None:
    # Act
    sentence = describe_ask_failure(exc)

    # Assert
    assert expected in sentence
    assert _SENTINEL not in sentence
    assert isinstance(exc, ASK_FAILURES)


def test_subclasses_are_matched_before_the_enrichment_base() -> None:
    """All three are ``EnrichmentError``s; none may get the generic sentence."""
    # Arrange
    generic = describe_ask_failure(EnrichmentError(_SENTINEL))

    # Act
    specific = [
        describe_ask_failure(OllamaUnavailable(_SENTINEL)),
        describe_ask_failure(OllamaContextOverflow(_SENTINEL)),
        describe_ask_failure(_truncated()),
    ]

    # Assert
    assert generic not in specific
    assert len(set(specific)) == 3
    assert all("answer synthesis failed" not in s for s in specific)


def test_transient_and_permanent_embed_failures_differ() -> None:
    # Act
    transient = describe_ask_failure(EmbedError(_SENTINEL, transient=True))
    permanent = describe_ask_failure(EmbedError(_SENTINEL, transient=False))

    # Assert
    assert "retry later" in transient
    assert "brain doctor" not in transient
    assert "not pulled, or a bad key" in permanent
    assert "brain doctor" in permanent


def test_failures_tuple_is_exactly_the_four_families() -> None:
    assert set(ASK_FAILURES) == {
        EnrichmentError,
        GraphTenantError,
        GraphBackendError,
        EmbedError,
    }


def test_an_unlisted_class_still_gets_a_class_only_sentence() -> None:
    """Defensive: a caller that widens its ``except`` still never prints a message."""
    # Act
    sentence = describe_ask_failure(RuntimeError(_SENTINEL))

    # Assert
    assert sentence == "ask failed (RuntimeError)"
