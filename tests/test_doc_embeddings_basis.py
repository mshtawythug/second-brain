"""``doc_embeddings``: the text a document's vector is made from, and the refusal (RM-20).

Pure: a word-count cost stands in for ``Embedder.count_tokens``. All text is
synthetic.
"""
from __future__ import annotations

import hashlib

import pytest

from brain.doc_embeddings import (
    BASIS_TITLE_HEAD,
    BASIS_TITLE_SUMMARY,
    DOC_BASIS_MAX_TOKENS,
    DOC_HEAD_CHAR_CAP,
    DOC_HEAD_TOKENS,
    DocEmbeddingError,
    backfill,
    document_basis,
    refuse_without_vectors,
)
from brain.embeddings import NullEmbedder
from brain.ollama_budget import INGEST_CHUNK_OVERLAP_TOKENS, INGEST_CHUNK_TARGET_TOKENS
from tests.conftest import FakeEmbedder

pytestmark = pytest.mark.nodb


def _words(text: str) -> int:
    """One token per whitespace-separated word: easy to reason about in a test."""
    return len(text.split())


def _body(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def test_a_summary_is_the_basis_when_present() -> None:
    basis = document_basis(
        "Synthetic title", "A synthetic summary.", "body text", count_tokens=_words
    )

    assert basis.kind == BASIS_TITLE_SUMMARY
    assert basis.text == "Synthetic title\nA synthetic summary."
    assert basis.digest == hashlib.sha256(basis.text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("summary", [None, "", "   \n"])
def test_no_summary_falls_back_to_the_head_of_the_content(summary: str | None) -> None:
    basis = document_basis("Synthetic title", summary, "first words here", count_tokens=_words)

    assert basis.kind == BASIS_TITLE_HEAD
    assert basis.text == "Synthetic title\nfirst words here"


def test_the_head_is_the_first_512_tokens() -> None:
    basis = document_basis("T", None, _body(2000), count_tokens=_words)

    assert DOC_HEAD_TOKENS == 512
    assert basis.text == "T\n" + _body(512)


def test_the_head_is_read_from_the_first_8192_characters_only() -> None:
    # A single "word" longer than the cap: only its first 8192 characters count.
    long_word = "x" * (DOC_HEAD_CHAR_CAP + 500)

    basis = document_basis("T", None, long_word, count_tokens=_words)

    assert DOC_HEAD_CHAR_CAP == 8192
    assert basis.text == "T\n" + "x" * DOC_HEAD_CHAR_CAP


def test_the_whole_basis_is_capped_at_the_chunk_bound() -> None:
    # A title far longer than a chunk must not make the basis longer than the
    # embed window is validated for.
    basis = document_basis(_body(3000), "A synthetic summary.", "", count_tokens=_words)

    assert DOC_BASIS_MAX_TOKENS == INGEST_CHUNK_TARGET_TOKENS + INGEST_CHUNK_OVERLAP_TOKENS
    assert _words(basis.text) == DOC_BASIS_MAX_TOKENS
    assert basis.text == _body(DOC_BASIS_MAX_TOKENS)


def test_a_short_basis_is_not_cut() -> None:
    basis = document_basis("Short title", "Short summary.", "", count_tokens=_words)

    assert basis.text == "Short title\nShort summary."


def test_the_digest_is_stable_and_tracks_the_text() -> None:
    first = document_basis("T", "S one", "", count_tokens=_words)
    again = document_basis("T", "S one", "", count_tokens=_words)
    changed = document_basis("T", "S two", "", count_tokens=_words)

    assert first.digest == again.digest
    assert first.digest != changed.digest


def test_an_embedder_without_vectors_is_refused_before_the_database_is_touched() -> None:
    # ``conn`` is never used: the refusal comes first (M10).
    with pytest.raises(DocEmbeddingError, match="BRAIN_EMBEDDER=none"):
        backfill(None, NullEmbedder())  # type: ignore[arg-type]


def test_an_embedder_with_vectors_passes_the_refusal() -> None:
    refuse_without_vectors(FakeEmbedder(dim=8))
