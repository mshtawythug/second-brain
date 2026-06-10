"""Unit tests for the answer-quality eval scorer + corpus loader (Plan 06)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.ask import AskResult, Citation
from brain.eval import (
    AnswerEvalCase,
    load_answer_corpus,
    run_answer_eval,
    score_answer,
)
from brain.eval.errors import EvalCorpusError


def test_score_answer_fact_recall() -> None:
    # Both facts covered: "streaming" exact, "data pipeline" via majority-token
    # overlap ("pipeline" present).
    assert score_answer(
        "the pipeline uses streaming", ["streaming", "data pipeline"]
    ) == 1.0


def test_score_answer_partial_recall() -> None:
    # "streaming" present, "data pipeline" absent (no significant-token overlap).
    score = score_answer("only mentions streaming here", ["streaming", "data pipeline"])
    assert score == 0.5


def test_score_answer_empty_facts_is_one() -> None:
    assert score_answer("anything", []) == 1.0


def test_score_answer_substring_match() -> None:
    assert score_answer("we chose a read through cache layer", ["read through cache"]) == 1.0


def test_score_answer_zero_when_absent() -> None:
    assert score_answer("totally unrelated text", ["quantum entanglement"]) == 0.0


def test_run_answer_eval_aggregates() -> None:
    cases = [
        AnswerEvalCase(
            question="q1", expected_facts=["streaming"], category="synthesis"
        ),
        AnswerEvalCase(
            question="q2", expected_facts=["absent fact"], category="multi-hop"
        ),
    ]

    def _ask_fn(question: str) -> AskResult:
        answer = "streaming ingestion [1]" if question == "q1" else "nothing here"
        citations = (
            [Citation(1, "doc-1", "T", "manual", "s")] if question == "q1" else []
        )
        return AskResult(
            answer=answer,
            citations=citations,
            iterations_used=1,
            sub_queries=[question],
            fallback_used=True,
            session_id="abc",
        )

    report = run_answer_eval(cases, _ask_fn, now=datetime(2026, 6, 9, tzinfo=UTC))
    assert len(report.scores) == 2
    assert report.scores[0].fact_recall == 1.0
    assert report.scores[1].fact_recall == 0.0
    assert report.mean_fact_recall == 0.5
    assert report.mean_citation_count == 0.5
    assert report.timestamp == "2026-06-09T00:00:00+00:00"


def test_load_answer_corpus_default() -> None:
    cases = load_answer_corpus()
    assert len(cases) >= 10
    for case in cases:
        assert case.question
        assert case.expected_facts
        assert case.category in {"synthesis", "multi-hop", "person", "timeline"}


def test_load_answer_corpus_validates_schema(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\ncases:\n  - question: q\n    category: synthesis\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalCorpusError, match="expected_facts"):
        load_answer_corpus(bad)


def test_load_answer_corpus_rejects_empty_facts(tmp_path: Path) -> None:
    bad = tmp_path / "empty.yaml"
    bad.write_text(
        "version: 1\ncases:\n  - question: q\n    expected_facts: []\n    category: synthesis\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalCorpusError, match="empty"):
        load_answer_corpus(bad)


def test_load_answer_corpus_rejects_unknown_category(tmp_path: Path) -> None:
    bad = tmp_path / "cat.yaml"
    bad.write_text(
        "version: 1\ncases:\n  - question: q\n    expected_facts: [x]\n    category: bogus\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalCorpusError, match="unknown category"):
        load_answer_corpus(bad)


def test_load_answer_corpus_rejects_version_mismatch(tmp_path: Path) -> None:
    bad = tmp_path / "ver.yaml"
    bad.write_text("version: 99\ncases: []\n", encoding="utf-8")
    with pytest.raises(EvalCorpusError, match="version mismatch"):
        load_answer_corpus(bad)


def test_load_answer_corpus_missing_file(tmp_path: Path) -> None:
    with pytest.raises(EvalCorpusError, match="not found"):
        load_answer_corpus(tmp_path / "nope.yaml")
