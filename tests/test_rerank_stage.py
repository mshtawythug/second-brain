"""``brain.rerank.RerankStage``: ordering, tail, passages, failure and egress (RM-14a).

The pure tests pass ``conn=None``: a local (non-hosted) reranker never touches
the database. The egress tests use the 5434 test database, because the
confidential lookup is one SQL statement. All data is synthetic.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import psycopg
import pytest

from brain.ingest import ExtractedDoc, ingest_document
from brain.rerank import RERANK_STAGE_NAME, RerankError, RerankStage, rerank_notice
from brain.search import SearchExplanation, SearchResult
from brain.search_pipeline import SearchStage, StageReport, apply_stages
from tests.rerank_fakes import (
    RERANK_SECRET,
    FakeReranker,
    RaisingReranker,
    ShortReranker,
    ids,
    title_scorer,
)


def _result(n: int, *, explain: bool = False, snippet: str | None = None) -> SearchResult:
    return SearchResult(
        document_id=f"doc-{n}",
        title=f"Note {n}",
        source_kind="manual",
        snippet=snippet if snippet is not None else f"snippet {n}",
        score=1.0 / (n + 1),
        content_type="note",
        tags=[],
        explain=_explanation(n) if explain else None,
    )


def _explanation(n: int) -> SearchExplanation:
    return SearchExplanation(
        fts_rank=n + 1,
        fts_score=0.5,
        fts_rrf_contribution=1 / 61,
        vector_rank=None,
        vector_cosine=None,
        vector_rrf_contribution=0.0,
        rrf_score=1 / 61,
        recency_age_days=None,
        recency_boost=1.0,
        final_score=1.0 / (n + 1),
        best_chunk_id=f"chunk-{n}",
        best_chunk_index=0,
        matched_filters={},
    )


def _stage(reranker: Any, *, depth: int = 3, max_chars: int = 1500) -> RerankStage:
    return RerankStage(reranker=reranker, depth=depth, max_chars=max_chars, timeout=2.5)


def _apply(stage: RerankStage, results: list[SearchResult], conn: Any = None) -> Any:
    return stage.apply(conn, query="synthetic query", session_id=None, results=results)


# --- shape -------------------------------------------------------------------


def test_the_stage_conforms_to_the_pipeline_protocol() -> None:
    stage: SearchStage = _stage(FakeReranker(lambda _p: 0.0))
    assert stage.name == RERANK_STAGE_NAME == "rerank"


def test_candidate_limit_is_at_least_the_depth() -> None:
    stage = _stage(FakeReranker(lambda _p: 0.0), depth=20)
    assert stage.candidate_limit(5) == 20
    assert stage.candidate_limit(30) == 30


def test_signature_names_backend_model_and_depth() -> None:
    stage = _stage(FakeReranker(lambda _p: 0.0, name="llamacpp", model="m-1"), depth=7)
    assert stage.signature() == {
        "reranker": "llamacpp",
        "reranker_model": "m-1",
        "rerank_depth": 7,
    }


# --- ordering ------------------------------------------------------------------


def test_the_head_is_reordered_by_score_and_the_tail_is_kept() -> None:
    results = [_result(n) for n in range(6)]
    reranker = FakeReranker(title_scorer(["Note 2", "Note 0", "Note 1"]))

    outcome = _apply(_stage(reranker, depth=3), results)

    assert ids(outcome.results) == ["doc-2", "doc-0", "doc-1", "doc-3", "doc-4", "doc-5"]
    assert outcome.report == StageReport(
        name="rerank", applied=True, skipped_reason=None, ms=outcome.report.ms
    )
    # Only the head is sent.
    assert len(reranker.passages_sent) == 3


def test_ties_keep_their_prior_order() -> None:
    results = [_result(n) for n in range(4)]
    reranker = FakeReranker(title_scorer(["Note 3"]))  # the rest score 0, a tie

    outcome = _apply(_stage(reranker, depth=4), results)

    assert ids(outcome.results) == ["doc-3", "doc-0", "doc-1", "doc-2"]


def test_a_depth_beyond_the_rows_scores_every_row() -> None:
    results = [_result(n) for n in range(2)]
    reranker = FakeReranker(title_scorer(["Note 1", "Note 0"]))

    outcome = _apply(_stage(reranker, depth=20), results)

    assert ids(outcome.results) == ["doc-1", "doc-0"]


def test_score_is_not_overwritten_and_the_input_is_not_mutated() -> None:
    results = [_result(n, explain=True) for n in range(3)]
    before = [replace(r) for r in results]
    reranker = FakeReranker(title_scorer(["Note 2", "Note 1", "Note 0"]))

    outcome = _apply(_stage(reranker, depth=3), results)

    assert results == before
    by_id = {r.document_id: r for r in outcome.results}
    for original in before:
        assert by_id[original.document_id].score == original.score


def test_reranker_score_is_set_on_the_explanation() -> None:
    results = [_result(n, explain=True) for n in range(4)]
    reranker = FakeReranker(title_scorer(["Note 1", "Note 0", "Note 2"]))

    outcome = _apply(_stage(reranker, depth=3), results)

    scores = {r.document_id: r.explain.reranker_score for r in outcome.results if r.explain}
    assert scores == {"doc-1": 3.0, "doc-0": 2.0, "doc-2": 1.0, "doc-3": None}


def test_rows_without_an_explanation_stay_without_one() -> None:
    outcome = _apply(_stage(FakeReranker(lambda _p: 1.0)), [_result(0), _result(1)])
    assert all(r.explain is None for r in outcome.results)


def test_the_passage_is_title_newline_snippet_cut_to_max_chars() -> None:
    long = "x" * 500
    reranker = FakeReranker(lambda _p: 0.0)

    _apply(_stage(reranker, max_chars=120), [_result(0), _result(1, snippet=long)])

    assert reranker.passages_sent == ["Note 0\nsnippet 0", ("Note 1\n" + long)[:120]]
    assert reranker.calls[0]["query"] == "synthetic query"
    assert reranker.calls[0]["timeout"] == 2.5


def test_no_rows_means_no_call() -> None:
    reranker = FakeReranker(lambda _p: 0.0)
    outcome = _apply(_stage(reranker), [])
    assert outcome.results == []
    assert outcome.report.applied
    assert reranker.calls == []


# --- failure -------------------------------------------------------------------


@pytest.mark.parametrize(
    "reranker",
    [
        RaisingReranker(),
        ShortReranker(lambda _p: 1.0),
        FakeReranker(lambda _p: math.nan),
        FakeReranker(lambda _p: math.inf),
    ],
    ids=["raises", "short", "nan", "inf"],
)
def test_a_failed_reranker_returns_the_input_with_a_class_only_reason(reranker: Any) -> None:
    results = [_result(n, explain=True) for n in range(4)]

    outcome = _apply(_stage(reranker), results)

    assert outcome.results is results
    assert outcome.report.applied is False
    assert outcome.report.skipped_reason is not None
    assert outcome.report.skipped_reason.startswith("RerankError")
    assert RERANK_SECRET not in outcome.report.skipped_reason
    assert all(r.explain is not None and r.explain.reranker_score is None for r in results)


def test_an_unexpected_error_is_skipped_by_the_pipeline_not_raised() -> None:
    results = [_result(n) for n in range(3)]
    stage = _stage(RaisingReranker(RuntimeError))

    rows, (report,) = apply_stages(
        None,  # type: ignore[arg-type]
        query="q",
        session_id=None,
        results=results,
        stages=[stage],
    )

    assert rows == results
    assert report.applied is False
    assert report.skipped_reason == "RuntimeError"


def test_rerank_error_is_a_brain_error() -> None:
    from brain.errors import BrainError

    assert issubclass(RerankError, BrainError)


# --- the CLI notice ------------------------------------------------------------


def test_the_notice_names_a_skipped_rerank_only() -> None:
    skipped = StageReport(name="rerank", applied=False, skipped_reason="RerankError", ms=1.0)
    applied = StageReport(name="rerank", applied=True, skipped_reason=None, ms=1.0)
    other = StageReport(name="other", applied=False, skipped_reason="X", ms=1.0)

    assert rerank_notice([skipped]) == "reranker unavailable; results are in hybrid order"
    assert rerank_notice([applied]) is None
    assert rerank_notice([other]) is None
    assert rerank_notice([]) is None


# --- egress (test database) ------------------------------------------------------


def _ingest(conn: psycopg.Connection[Any], embedder: Any, n: int) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=f"Note {n}",
            content=f"Synthetic body {n} about quillmoss.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=f"manual:rerank-egress-{n}",
    )
    return str(result.document_id)


@pytest.fixture
def four_docs(test_db: psycopg.Connection[Any], fake_embedder: Any) -> list[SearchResult]:
    rows = []
    for n in range(4):
        doc_id = _ingest(test_db, fake_embedder, n)
        rows.append(replace(_result(n), document_id=doc_id))
    # Row 1 (the second) is confidential.
    test_db.execute(
        "UPDATE documents SET sensitivity = 'confidential' WHERE id = %s",
        (rows[1].document_id,),
    )
    return rows


def test_a_hosted_reranker_never_receives_a_confidential_passage(
    test_db: psycopg.Connection[Any], four_docs: list[SearchResult]
) -> None:
    reranker = FakeReranker(
        title_scorer(["Note 3", "Note 2", "Note 0", "Note 1"]), hosted_egress=True
    )

    outcome = _apply(_stage(reranker, depth=4), four_docs, conn=test_db)

    assert "Note 1\nsnippet 1" not in reranker.passages_sent
    assert len(reranker.passages_sent) == 3
    # The confidential row keeps index 1; the others fill the free slots by score.
    assert [r.title for r in outcome.results] == ["Note 3", "Note 1", "Note 2", "Note 0"]
    assert outcome.report.applied


def test_a_local_reranker_scores_confidential_rows_too(
    test_db: psycopg.Connection[Any], four_docs: list[SearchResult]
) -> None:
    reranker = FakeReranker(title_scorer(["Note 1", "Note 0"]), hosted_egress=False)

    outcome = _apply(_stage(reranker, depth=4), four_docs, conn=test_db)

    assert "Note 1\nsnippet 1" in reranker.passages_sent
    assert outcome.results[0].title == "Note 1"


def test_an_all_confidential_head_sends_nothing(
    test_db: psycopg.Connection[Any], four_docs: list[SearchResult]
) -> None:
    test_db.execute(
        "UPDATE documents SET sensitivity = 'confidential' WHERE id = ANY(%s::uuid[])",
        ([r.document_id for r in four_docs],),
    )
    reranker = FakeReranker(lambda _p: 1.0, hosted_egress=True)

    outcome = _apply(_stage(reranker, depth=4), four_docs, conn=test_db)

    assert reranker.calls == []
    assert ids(outcome.results) == ids(four_docs)
    assert outcome.report.applied
