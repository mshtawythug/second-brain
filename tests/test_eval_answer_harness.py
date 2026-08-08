"""Live answer-eval harness for `brain ask` (Plan 06, Phase 3).

@pytest.mark.eval — EXCLUDED from the default pytest invocation (pyproject
``addopts`` carries ``-m 'not eval'``). Run explicitly with ``pytest -m eval``.
Requires a live Ollama (the synthesize step uses the REAL ``chat_json``); the
retrieval leg uses the deterministic ``FakeEmbedder`` so the harness is
reproducible without a populated brain — it seeds the matching synthetic
documents itself. NOT committed as a ``ci.json`` baseline (live-Ollama gated).
"""
from __future__ import annotations

import psycopg
import pytest

from brain import chat
from brain.ask import AskResult, ask_no_loop
from brain.config import Config
from brain.errors import OllamaUnavailable
from brain.eval import load_answer_corpus, run_answer_eval
from brain.ingest import ExtractedDoc, ingest_document

from .conftest import TEST_DATABASE_URL, FakeEmbedder

# Synthetic seed documents whose bodies contain each corpus case's expected
# facts. All synthetic (no PII).
_SEED_DOCS: list[tuple[str, str]] = [
    (
        "Synthetic onboarding playbook",
        "The synthetic onboarding playbook covers paired mentorship and a "
        "staged checklist for ramping new engineers on the platform team.",
    ),
    (
        "Synthetic data pipeline decision",
        "We decided the data pipeline would use streaming ingestion with a "
        "batch fallback for nightly reconciliation.",
    ),
    (
        "Synthetic log retention policy",
        "The retention policy for synthetic logs keeps them ninety days hot "
        "then moves them to cold storage.",
    ),
    (
        "Synthetic payments incident postmortem",
        "The synthetic incident on the payments service was resolved by a "
        "rollback and disabling a config flag.",
    ),
    (
        "Synthetic project Atlas review",
        "Recurring themes in reviews of project Atlas were scope creep and "
        "testing gaps across the squad.",
    ),
    (
        "Synthetic negotiation notes",
        "Across my synthetic job search I learned to anchor high and to use a "
        "competing offer as leverage.",
    ),
    (
        "Synthetic on-call expectations",
        "The synthetic platform team's on-call expectations name a primary "
        "responder and a clear escalation path.",
    ),
    (
        "Synthetic mentor feedback",
        "The mentor gave the synthetic mentee feedback on clearer "
        "communication and stronger ownership.",
    ),
    (
        "Synthetic testing strategy evolution",
        "The synthetic team's testing strategy evolved from unit tests toward "
        "end to end coverage over time.",
    ),
    (
        "Synthetic quarterly goals",
        "The synthetic quarterly goals for the platform group were to reduce "
        "latency and improve reliability.",
    ),
    (
        "Synthetic migration blockers",
        "The synthetic migration hit blockers around a schema mismatch and a "
        "tight downtime window.",
    ),
    (
        "Synthetic caching design review",
        "The synthetic design review concluded caching should use a read "
        "through cache with explicit invalidation.",
    ),
]

_MIN_MEAN_FACT_RECALL = 0.60


@pytest.mark.eval
@pytest.mark.live_ollama
def test_answer_eval_harness_meets_threshold(
    test_db: psycopg.Connection,  # noqa: ARG001 — schema reset
) -> None:
    embedder = FakeEmbedder()
    for title, content in _SEED_DOCS:
        ingest_document(
            test_db,
            embedder=embedder,
            doc=ExtractedDoc(
                title=title,
                content=content,
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            tags=[],
        )

    cfg = Config(database_url=TEST_DATABASE_URL)
    cases = load_answer_corpus()

    def _ask_fn(question: str) -> AskResult:
        return ask_no_loop(
            test_db,
            cfg,
            embedder=embedder,
            chat=chat.chat_json,
            question=question,
            limit=5,
        )

    try:
        report = run_answer_eval(cases, _ask_fn)
    except OllamaUnavailable:
        pytest.skip("Ollama unavailable — answer-eval harness needs a live model")

    assert report.mean_fact_recall >= _MIN_MEAN_FACT_RECALL, (
        f"mean_fact_recall={report.mean_fact_recall:.3f} "
        f"below threshold {_MIN_MEAN_FACT_RECALL}"
    )
