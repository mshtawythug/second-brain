"""``DocLegStage`` over the real test database and ``hybrid_search`` (RM-20).

The corpus (:mod:`tests.doc_leg_corpus`) has one document whose summary fits the
query while its chunks barely do, so plain hybrid search ranks it last. AC2: the
stage lifts it, and every filter still excludes what it excluded. AC3, second
half: a confidential document left vectorless by a hosted embedder keeps its
index. D12: zero vectors is a skip, one vector applies. All text is synthetic.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from brain.doc_embeddings import backfill
from brain.search import hybrid_search
from brain.search_doc_leg import NO_DOCUMENT_VECTORS, DocLegStage
from brain.search_pipeline import PipelineResult, run_search
from tests.conftest import CountingEmbedder
from tests.doc_leg_corpus import (
    OTHERS,
    QUERY,
    HostedWordEmbedder,
    ModelWordEmbedder,
    reconcile,
    seed,
)
from tests.saved_search_fakes import WordEmbedder

LIMIT = OTHERS + 1
#: Heavy enough that a top-cosine document reaches the head from last place.
WEIGHT = 10.0


@pytest.fixture
def word(test_db: psycopg.Connection[Any]) -> WordEmbedder:
    embedder = WordEmbedder()
    reconcile(test_db, embedder)
    return embedder


def _search(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    staged: bool,
    query: str = QUERY,
    limit: int = LIMIT,
    **kw: Any,
) -> PipelineResult:
    stages = (DocLegStage(embedder=embedder, weight=WEIGHT),) if staged else ()
    return run_search(
        hybrid_search, conn, embedder=embedder, query=query, limit=limit, stages=stages, **kw
    )


def _ids(result: PipelineResult) -> list[str]:
    return [r.document_id for r in result.results]


def test_a_document_whose_summary_fits_moves_up(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)
    backfill(test_db, word)

    plain = _ids(_search(test_db, word, staged=False))
    staged_result = _search(test_db, word, staged=True)
    staged = _ids(staged_result)

    assert plain[-1] == ids["fit"]
    assert staged[0] == ids["fit"]
    assert sorted(staged) == sorted(plain)
    (report,) = staged_result.stages
    assert (report.name, report.applied, report.skipped_reason) == ("doc_leg", True, None)


def test_a_source_filter_still_excludes_the_fitting_document(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word, fit_source="krisp")
    backfill(test_db, word)

    staged = _ids(_search(test_db, word, staged=True, source_kind="manual"))

    assert ids["fit"] not in staged
    assert len(staged) == OTHERS


def test_a_tag_filter_still_excludes_the_fitting_document(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)
    test_db.execute(
        "UPDATE documents SET tags = ARRAY['harbour-log'] WHERE id <> %s", (ids["fit"],)
    )
    backfill(test_db, word)

    staged = _ids(_search(test_db, word, staged=True, tag="harbour-log"))

    assert ids["fit"] not in staged
    assert len(staged) == OTHERS


def test_the_sensitivity_lens_still_excludes_the_fitting_document(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    # A local embedder: the confidential document HAS a vector, the best one.
    ids = seed(test_db, word, fit_sensitivity="confidential")
    backfill(test_db, word)

    staged = _ids(_search(test_db, word, staged=True, sensitivity="normal"))

    assert ids["fit"] not in staged
    assert len(staged) == OTHERS


def test_a_confidential_document_without_a_vector_keeps_its_index(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    # AC3, second half: under a hosted embedder the confidential lead document
    # has no vector, and the stage neither promotes nor demotes it. It ranks
    # above the fit document in plain order (never last), and the fit document
    # (which has a vector) is lifted past it, so a demotion would show.
    ids = seed(test_db, word, lead_sensitivity="confidential")
    hosted = HostedWordEmbedder()
    reconcile(test_db, hosted)
    backfill(test_db, hosted)

    plain = _ids(_search(test_db, hosted, staged=False, limit=LIMIT + 1))
    staged_result = _search(test_db, hosted, staged=True, limit=LIMIT + 1)
    staged = _ids(staged_result)

    assert len(plain) == LIMIT + 1
    assert plain.index(ids["lead"]) < len(plain) - 1
    assert plain[-1] == ids["fit"]
    assert staged_result.stages[0].applied
    assert staged.index(ids["fit"]) < plain.index(ids["fit"])
    assert staged.index(ids["lead"]) == plain.index(ids["lead"])


def test_zero_vectors_is_a_skip_and_keeps_the_order(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    seed(test_db, word)

    plain = _ids(_search(test_db, word, staged=False))
    staged_result = _search(test_db, word, staged=True)

    (report,) = staged_result.stages
    assert (report.applied, report.skipped_reason) == (False, NO_DOCUMENT_VECTORS)
    assert _ids(staged_result) == plain


def test_one_vector_is_enough_to_apply(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    seed(test_db, word)
    backfill(test_db, word, limit=1)

    (report,) = _search(test_db, word, staged=True).stages

    assert report.applied


def test_vectors_from_another_model_are_ignored(test_db: psycopg.Connection[Any]) -> None:
    writer, reader = ModelWordEmbedder("model-one"), ModelWordEmbedder("model-two")
    reconcile(test_db, writer)
    seed(test_db, writer)
    backfill(test_db, writer)

    (report,) = _search(test_db, reader, staged=True).stages

    assert report.skipped_reason == NO_DOCUMENT_VECTORS


def test_the_stage_reuses_the_searchs_query_vector(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    seed(test_db, word)
    counting = CountingEmbedder(word)
    backfill(test_db, counting)  # the vectors carry the counting wrapper's identity
    before = counting.embed_calls
    # A query no other test asks, so the in-process cache starts cold for it.
    query = f"{QUERY} probe{uuid.uuid4().hex}"

    result = _search(test_db, counting, staged=True, query=query)

    assert result.stages[0].applied
    assert counting.embed_calls == before + 1
