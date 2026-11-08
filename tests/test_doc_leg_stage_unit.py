"""``DocLegStage`` over a fake connection: its logic without a database (RM-20).

``FakeVectorConn`` answers the stage's one lookup statement from a dict. The
real-database behaviour (the SQL, filters, the confidential case) is in
``tests/test_doc_leg_stage.py``. All values are synthetic.
"""
from __future__ import annotations

from typing import Any

import pytest

from brain.search import embedder_identity
from brain.search_doc_leg import (
    DOC_LEG_DEPTH,
    NO_DOCUMENT_VECTORS,
    DocLegStage,
)
from tests.conftest import CountingEmbedder, FakeEmbedder
from tests.doc_leg_fakes import FakeVectorConn, result

pytestmark = pytest.mark.nodb


def _ids(rows: list[Any]) -> list[str]:
    return [r.document_id for r in rows]


def _stage(embedder: Any = None, weight: float = 1.0) -> DocLegStage:
    return DocLegStage(embedder=embedder or FakeEmbedder(dim=8), weight=weight)


def test_it_looks_as_deep_as_a_ranking_leg_can_contribute() -> None:
    stage = _stage()

    assert DOC_LEG_DEPTH == 50
    assert stage.candidate_limit(5) == 50
    assert stage.candidate_limit(80) == 80


def test_its_signature_is_the_weight_alone() -> None:
    assert _stage(weight=0.5).signature() == {"doc_leg_weight": 0.5}


def test_empty_input_applies_without_a_lookup() -> None:
    conn = FakeVectorConn({})

    outcome = _stage().apply(conn, query="q", session_id=None, results=[])  # type: ignore[arg-type]

    assert outcome.report.applied
    assert outcome.results == []
    assert conn.calls == []


def test_zero_vectors_among_the_candidates_is_a_skip() -> None:
    rows = [result(n) for n in range(3)]

    outcome = _stage().apply(
        FakeVectorConn({}), query="q", session_id=None, results=rows  # type: ignore[arg-type]
    )

    assert not outcome.report.applied
    assert outcome.report.skipped_reason == NO_DOCUMENT_VECTORS
    assert _ids(outcome.results) == ["doc-0", "doc-1", "doc-2"]


def test_one_vector_among_the_candidates_applies() -> None:
    rows = [result(n) for n in range(3)]

    outcome = _stage().apply(
        FakeVectorConn({"doc-2": 0.9}), query="q", session_id=None, results=rows  # type: ignore[arg-type]
    )

    assert outcome.report.applied
    assert outcome.report.skipped_reason is None
    assert _ids(outcome.results) == ["doc-0", "doc-1", "doc-2"]


def test_a_fitting_document_moves_up_and_a_vectorless_one_holds_its_place() -> None:
    rows = [result(n) for n in range(4)]
    cosines = {"doc-0": 0.1, "doc-2": 0.2, "doc-3": 0.99}

    outcome = _stage(weight=10.0).apply(
        FakeVectorConn(cosines), query="q", session_id=None, results=rows  # type: ignore[arg-type]
    )

    assert _ids(outcome.results) == ["doc-3", "doc-1", "doc-2", "doc-0"]


def test_the_lookup_is_keyed_by_the_active_embedders_identity() -> None:
    embedder = FakeEmbedder(dim=8)
    rows = [result(n) for n in range(2)]
    conn = FakeVectorConn({"doc-1": 0.9}, identity=embedder_identity(embedder))

    _stage(embedder).apply(conn, query="q", session_id=None, results=rows)  # type: ignore[arg-type]

    (_sql, (_vector, ids, identity)), = conn.calls
    assert ids == ["doc-0", "doc-1"]
    assert identity == embedder_identity(embedder)


def test_rows_from_another_embedder_count_as_absent() -> None:
    rows = [result(n) for n in range(2)]
    conn = FakeVectorConn({"doc-1": 0.9}, identity="some.Other|other-model|8")

    outcome = _stage().apply(conn, query="q", session_id=None, results=rows)  # type: ignore[arg-type]

    assert outcome.report.skipped_reason == NO_DOCUMENT_VECTORS


def test_rows_past_the_depth_keep_their_order_after_the_head() -> None:
    rows = [result(n) for n in range(DOC_LEG_DEPTH + 3)]
    cosines = {f"doc-{n}": 1.0 - n / 100 for n in range(DOC_LEG_DEPTH)}

    outcome = _stage().apply(
        FakeVectorConn(cosines), query="q", session_id=None, results=rows  # type: ignore[arg-type]
    )

    assert _ids(outcome.results)[DOC_LEG_DEPTH:] == [
        f"doc-{n}" for n in range(DOC_LEG_DEPTH, DOC_LEG_DEPTH + 3)
    ]
    conn_ids = sorted(_ids(outcome.results)[:DOC_LEG_DEPTH])
    assert conn_ids == sorted(f"doc-{n}" for n in range(DOC_LEG_DEPTH))


def test_the_score_is_not_overwritten() -> None:
    rows = [result(n) for n in range(3)]

    outcome = _stage(weight=10.0).apply(
        FakeVectorConn({"doc-2": 0.9, "doc-0": 0.1}), query="q", session_id=None, results=rows  # type: ignore[arg-type]
    )

    assert {r.document_id: r.score for r in outcome.results} == {
        r.document_id: r.score for r in rows
    }


def test_the_query_vector_comes_from_the_search_cache() -> None:
    # The search embeds the query first; the stage must reuse that vector, so a
    # second embed never happens for the same embedder and text.
    counting = CountingEmbedder(FakeEmbedder(dim=8))
    rows = [result(n) for n in range(2)]
    from brain.search import cached_query_embedding

    cached_query_embedding(counting, "a synthetic query only this test asks")
    before = counting.embed_calls
    _stage(counting).apply(
        FakeVectorConn({"doc-1": 0.5}),  # type: ignore[arg-type]
        query="a synthetic query only this test asks",
        session_id=None,
        results=rows,
    )

    assert counting.embed_calls == before == 1
