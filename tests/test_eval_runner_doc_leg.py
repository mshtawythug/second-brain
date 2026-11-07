"""``brain eval`` with the document leg (RM-20, spec §2.7).

``run_eval(env=...)`` builds the leg from the dict, bound to the run's embedder,
scores the fused order, and adds exactly ``doc_leg_weight`` to
``config_signature``. Off, the signature is the four keys it always had (AC1).
A row where the leg skipped (no vectors yet, D12) is marked ``stages_skipped``,
so the run refuses it loudly rather than quietly measuring "off".

Every ``run_eval`` call passes ``stages`` or ``env`` explicitly. Real Postgres
(the 5434 test database); all data is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.doc_embeddings import backfill
from brain.eval.corpus import EvalQuery
from brain.eval.runner import run_eval, skipped_stage_results
from tests.doc_leg_corpus import QUERY, reconcile, seed
from tests.saved_search_fakes import WordEmbedder

BASE_SIGNATURE = {
    "recency_halflife_days": None,
    "snippet_context_tokens": 0,
    "vector_sim_floor": 0.0,
    "embedder": "unknown",
}
ON = {"BRAIN_DOC_LEG": "true", "BRAIN_DOC_LEG_WEIGHT": "10"}


@pytest.fixture
def word(test_db: psycopg.Connection[Any]) -> WordEmbedder:
    embedder = WordEmbedder()
    reconcile(test_db, embedder)
    return embedder


def _query(expected: str) -> EvalQuery:
    return EvalQuery(query=QUERY, expected_doc_ids=[expected], category="semantic")


def test_off_leaves_the_signature_as_it_was(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)

    report = run_eval(test_db, embedder=word, queries=[_query(ids["fit"])], env={})

    assert report.config_signature == BASE_SIGNATURE
    assert list(report.config_signature) == list(BASE_SIGNATURE)
    assert report.results[0].stage_ms == {}


def test_on_scores_the_fused_order_and_signs_the_weight(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)
    backfill(test_db, word)

    off = run_eval(test_db, embedder=word, queries=[_query(ids["fit"])], env={})
    on = run_eval(test_db, embedder=word, queries=[_query(ids["fit"])], env=ON)

    assert on.config_signature == {**BASE_SIGNATURE, "doc_leg_weight": 10.0}
    assert set(on.results[0].stage_ms) == {"doc_leg"}
    assert on.results[0].actual_doc_ids[0] == ids["fit"]
    assert on.results[0].mrr == 1.0
    assert off.results[0].mrr < on.results[0].mrr


def test_a_run_before_the_backfill_refuses_the_row(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)

    report = run_eval(test_db, embedder=word, queries=[_query(ids["fit"])], env=ON)

    assert report.results[0].stages_skipped == ("doc_leg",)
    assert skipped_stage_results(report) == [report.results[0]]
