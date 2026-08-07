"""``brain recall`` honours its token budget (F2).

The first test documents the defect recall exists to fix: ``hybrid_search``
bounds *snippet length*, not *token count*, so a handful of results with
generous snippet context runs to thousands of tokens with nothing in the
system aware of it. It passes on today's code — that is the point.

Everything after it pins the budget contract: the whole emitted block (header
included) fits, refs renumber over survivors so citations have no gaps, and a
budget too small for even one passage yields one truncated passage rather than
silence.

All fixture data is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.recall import recall
from brain.search import hybrid_search
from tests.conftest import TEST_DATABASE_URL

_LONG_BODY = (
    "The quarterly planning review covered platform staffing, the migration "
    "runway, and the hiring plan for the next two quarters. "
) * 40


def _cfg(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "recall_passage_tokens": 120,
        "vector_sim_floor": 0.0,
        "recency_halflife_days": None,
    }
    base.update(overrides)
    return Config(**base)


def _seed(
    conn: psycopg.Connection[Any],
    fake_embedder: Any,
    *,
    count: int = 7,
    body: str = _LONG_BODY,
) -> None:
    """Ingest ``count`` long synthetic docs that all match 'quarterly'."""
    from brain.ingest import ingest_document
    from brain.ingest.text import ExtractedDoc

    for i in range(count):
        ingest_document(
            conn,
            doc=ExtractedDoc(
                title=f"Quarterly Planning {i}",
                content=f"{body} Document number {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            embedder=fake_embedder,
            source_kind="manual",
            source_external_id=f"w4-recall-{i}",
        )


# ---------------------------------------------------------------------------
# The defect, documented
# ---------------------------------------------------------------------------


def test_search_result_size_is_unbounded_by_tokens(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """This PASSES on today's code and documents why recall exists.

    ``hybrid_search`` caps snippet *characters*, never tokens, and reports
    nothing about size. Stated as a direct comparison against ``recall`` on
    the *same corpus and query* rather than against a bare number: the
    absolute token count depends on chunking and the fake embedder's
    tokenizer and would drift, but "search overshoots the budget, recall does
    not" is the actual claim and cannot.
    """
    _seed(test_db, fake_embedder)
    budget = 800

    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="quarterly planning platform",
        limit=5,
        snippet_context_tokens=200,
    )
    search_tokens = sum(fake_embedder.count_tokens(r.snippet) for r in results)

    recalled = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="quarterly planning platform",
        budget_tokens=budget,
        max_candidates=25,
    )
    recall_tokens = fake_embedder.count_tokens(recalled.context_block())

    assert search_tokens > budget, (
        f"search should overshoot an agent budget of {budget}; got {search_tokens}"
    )
    assert recall_tokens <= budget, (
        f"recall must honour {budget}; got {recall_tokens}"
    )


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_recall_never_exceeds_token_budget(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The headline guarantee, measured on the WHOLE emitted block."""
    _seed(test_db, fake_embedder)

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="quarterly planning platform",
        budget_tokens=2000,
        max_candidates=25,
    )

    assert fake_embedder.count_tokens(result.context_block()) <= 2000


@pytest.mark.parametrize("budget", [200, 500, 1000, 2000, 4000])
def test_budget_is_honoured_across_sizes(
    test_db: psycopg.Connection[Any], fake_embedder: Any, budget: int
) -> None:
    _seed(test_db, fake_embedder)

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="quarterly planning platform",
        budget_tokens=budget,
        max_candidates=25,
    )

    assert fake_embedder.count_tokens(result.context_block()) <= budget
    assert result.used_tokens <= budget


def test_tiny_budget_yields_exactly_one_truncated_passage(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Silence is a worse answer than a cut-down passage."""
    _seed(test_db, fake_embedder)

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="quarterly planning platform",
        budget_tokens=80,
        max_candidates=25,
    )

    assert len(result.passages) == 1
    assert result.truncated is True
    assert result.passages[0].truncated is True


def test_refs_renumber_over_survivors_without_gaps(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """A dropped candidate must not leave a hole in the citation sequence.

    An agent told to cite ``[3]`` when refs run 1, 2, 4 will cite a passage
    that is not there.
    """
    _seed(test_db, fake_embedder)

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="quarterly planning platform",
        budget_tokens=600,
        max_candidates=25,
    )

    assert result.dropped > 0, "budget must be tight enough to drop something"
    assert [p.ref for p in result.passages] == list(
        range(1, len(result.passages) + 1)
    )


def test_dropped_counts_candidates_not_kept(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _seed(test_db, fake_embedder)

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="quarterly planning platform",
        budget_tokens=600,
        max_candidates=25,
    )

    assert result.dropped == result.candidates_considered - len(result.passages)


def test_one_passage_per_document(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Source diversity is what a limited budget most wants."""
    _seed(test_db, fake_embedder)

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="quarterly planning platform",
        budget_tokens=4000,
        max_candidates=25,
    )

    ids = [p.document_id for p in result.passages]
    assert len(ids) == len(set(ids))


def test_empty_corpus_yields_an_empty_recall(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="nothing matches this",
        budget_tokens=2000,
        max_candidates=25,
    )

    assert result.passages == []
    assert result.used_tokens == 0
    assert result.dropped == 0
    assert result.truncated is False


def test_header_tokens_are_inside_the_budget(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The envelope reservation — otherwise the header is a surprise on top."""
    _seed(test_db, fake_embedder)

    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="quarterly planning platform",
        budget_tokens=1000,
        max_candidates=25,
    )

    block = result.context_block()
    assert block.startswith("# recall:")
    assert fake_embedder.count_tokens(block) <= 1000


def test_recall_result_is_frozen(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    result = recall(
        test_db,
        _cfg(),
        embedder=fake_embedder,
        query="anything",
        budget_tokens=500,
        max_candidates=5,
    )

    with pytest.raises(AttributeError):
        result.used_tokens = 1  # type: ignore[misc]
