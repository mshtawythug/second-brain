"""``recall()`` never calls ``embed()`` (F2).

Recall must work under ``BRAIN_EMBEDDER=none``, where there is no embedding
backend at all. That works because ``hybrid_search`` auto-degrades to the
lexical leg via the duck-typed ``produces_embeddings`` check — but only for as
long as recall keeps routing every embedding decision through it.

A future "optimization" that embeds the query directly in ``recall()`` would
break FTS-only brains, and would do so *at runtime on the user's machine*
rather than in CI, because every other test uses a working fake embedder.

So the stub below raises on ``embed()``. If recall ever calls it, these tests
fail loudly and immediately. ``count_tokens`` still works, because budgeting
needs only that half of the Protocol — which is exactly why the budgeter takes
a ``cost`` callable rather than an embedder.

All fixture data is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.recall import recall
from tests.conftest import TEST_DATABASE_URL


class EmbedWouldBeAMistake:
    """An ``Embedder`` whose ``embed()`` is a tripwire.

    Mirrors ``NullEmbedder``'s duck-typed contract: ``produces_embeddings``
    is ``False``, so ``hybrid_search`` must never reach the vector leg.
    """

    dim = 1024
    produces_embeddings = False

    def __init__(self) -> None:
        self.embed_calls = 0

    def embed(
        self, texts: list[str], *, input_type: str | None = None
    ) -> list[list[float]]:
        self.embed_calls += 1
        raise AssertionError(
            "recall() must never call embed() — hybrid_search auto-degrades "
            "via produces_embeddings, and that is what makes recall work "
            "under BRAIN_EMBEDDER=none"
        )

    def count_tokens(self, text: str) -> int:
        """Budgeting needs only this half of the Protocol."""
        return max(1, len(text) // 4)


def _cfg() -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        recall_passage_tokens=120,
        vector_sim_floor=0.0,
        recency_halflife_days=None,
    )


def _seed(conn: psycopg.Connection[Any], embedder: Any, *, count: int = 4) -> None:
    from brain.ingest import ingest_document
    from brain.ingest.text import ExtractedDoc

    body = (
        "Platform migration notes covering staffing, the runway, and the "
        "quarterly hiring plan. "
    ) * 20
    for i in range(count):
        ingest_document(
            conn,
            doc=ExtractedDoc(
                title=f"Platform Migration {i}",
                content=f"{body} Entry {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            embedder=embedder,
            source_kind="manual",
            source_external_id=f"w4-null-{i}",
        )


def test_recall_works_without_an_embedding_backend(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The headline: FTS-only brains get recall.

    Seeded with the working fake embedder (ingest legitimately embeds), then
    recalled with the tripwire — which is the real-world shape of a user who
    switches to ``BRAIN_EMBEDDER=none``.
    """
    _seed(test_db, fake_embedder)
    tripwire = EmbedWouldBeAMistake()

    result = recall(
        test_db,
        _cfg(),
        embedder=tripwire,  # type: ignore[arg-type]
        query="platform migration staffing",
        budget_tokens=1500,
        max_candidates=25,
    )

    assert tripwire.embed_calls == 0
    assert result.passages, "the lexical leg must still return passages"
    assert tripwire.count_tokens(result.context_block()) <= 1500


def test_recall_honours_the_budget_under_fts_only(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Degrading the retrieval leg must not degrade the budget guarantee."""
    _seed(test_db, fake_embedder)
    tripwire = EmbedWouldBeAMistake()

    result = recall(
        test_db,
        _cfg(),
        embedder=tripwire,  # type: ignore[arg-type]
        query="platform migration staffing",
        budget_tokens=400,
        max_candidates=25,
    )

    assert tripwire.count_tokens(result.context_block()) <= 400
    assert tripwire.embed_calls == 0


def test_explicit_fts_only_also_never_embeds(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The explicit flag and the duck-typed degrade reach the same place."""
    _seed(test_db, fake_embedder)
    tripwire = EmbedWouldBeAMistake()

    result = recall(
        test_db,
        _cfg(),
        embedder=tripwire,  # type: ignore[arg-type]
        query="platform migration staffing",
        budget_tokens=1500,
        max_candidates=25,
        fts_only=True,
    )

    assert tripwire.embed_calls == 0
    assert result.passages


def test_empty_result_set_still_never_embeds(
    test_db: psycopg.Connection[Any]
) -> None:
    """The no-match path must not fall back to a vector search."""
    tripwire = EmbedWouldBeAMistake()

    result = recall(
        test_db,
        _cfg(),
        embedder=tripwire,  # type: ignore[arg-type]
        query="zzzz-no-such-term-anywhere",
        budget_tokens=1500,
        max_candidates=25,
    )

    assert tripwire.embed_calls == 0
    assert result.passages == []


def test_the_tripwire_actually_fires_when_embed_is_called() -> None:
    """Proves the guard is live rather than a stub that can never trigger.

    A test double that cannot fail is indistinguishable from no test at all;
    this pins that ``embed()`` really does raise.
    """
    tripwire = EmbedWouldBeAMistake()

    with pytest.raises(AssertionError, match="must never call embed"):
        tripwire.embed(["anything"])
