"""``brain eval`` with the engagement stage (RM-22b, spec §2.7).

``run_eval(env=...)`` builds the stage from the dict, scores the boosted order,
and adds exactly ``engagement_alpha`` and ``engagement_beta`` to
``config_signature``. Off, the signature is the four keys it always had (AC1).
A corpus nobody has opened yet is a real measurement (D6), so no row is refused.

Every ``run_eval`` call passes ``env`` explicitly. Real Postgres (the 5434 test
database); all data is synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

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
ON = {"BRAIN_ENGAGEMENT_ALPHA": "1", "BRAIN_ENGAGEMENT_BETA": "0.5"}


@pytest.fixture
def word(test_db: psycopg.Connection[Any]) -> WordEmbedder:
    embedder = WordEmbedder()
    reconcile(test_db, embedder)
    return embedder


def _query(expected: str) -> EvalQuery:
    return EvalQuery(query=QUERY, expected_doc_ids=[expected], category="semantic")


def _open(conn: psycopg.Connection[Any], doc: str, n: int) -> None:
    for _ in range(n):
        conn.execute(
            "INSERT INTO interactions (document_id, action, source) VALUES (%s, 'opened', 'cli')",
            (doc,),
        )


def test_off_leaves_the_signature_as_it_was(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)
    _open(test_db, ids["fit"], 20)

    report = run_eval(test_db, embedder=word, queries=[_query(ids["fit"])], env={})

    assert report.config_signature == BASE_SIGNATURE
    assert list(report.config_signature) == list(BASE_SIGNATURE)
    assert report.results[0].stage_ms == {}


def test_on_scores_the_boosted_order_and_signs_both_weights(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)
    _open(test_db, ids["fit"], 20)

    off = run_eval(test_db, embedder=word, queries=[_query(ids["fit"])], env={})
    on = run_eval(test_db, embedder=word, queries=[_query(ids["fit"])], env=ON)

    assert on.config_signature == {
        **BASE_SIGNATURE,
        "engagement_alpha": 1.0,
        "engagement_beta": 0.5,
    }
    assert set(on.results[0].stage_ms) == {"engagement"}
    assert on.results[0].actual_doc_ids[0] == ids["fit"]
    assert off.results[0].mrr < on.results[0].mrr == 1.0


def test_a_corpus_nobody_opened_refuses_no_row(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    # D6: multiplier 1.0 everywhere is a measurement, not a skip.
    ids = seed(test_db, word)

    report = run_eval(test_db, embedder=word, queries=[_query(ids["fit"])], env=ON)

    assert report.results[0].stages_skipped == ()
    assert skipped_stage_results(report) == []
