"""The document leg in the pipeline: ``build_stages`` and the text-only gate (RM-20).

Nothing here touches a database: a fake search function stands in for
``hybrid_search`` and the stages never read ``conn``. Every ``env`` is a plain
``dict``. All values are synthetic.
"""
from __future__ import annotations

from typing import Any

import pytest

from brain.config import ConfigError
from brain.embeddings import NullEmbedder, OllamaEmbedError
from brain.rerank import RerankStage
from brain.search import SearchResult
from brain.search_doc_leg import DOC_LEG_STAGE_NAME, DocLegStage
from brain.search_fallback import SearchFallback
from brain.search_pipeline import (
    TEXT_ONLY_SKIP_REASON,
    apply_stages,
    build_stages,
    override_stages,
    run_search,
    search_was_text_only,
    stage_needs_query_vector,
)
from tests.conftest import FakeEmbedder
from tests.doc_leg_fakes import VectorReverseStage, result
from tests.search_stages import ReverseStage

pytestmark = pytest.mark.nodb

_CONN: Any = object()
ON = {"BRAIN_DOC_LEG": "true"}


def _ids(results: list[SearchResult]) -> list[str]:
    return [r.document_id for r in results]


class _Search:
    """Returns ``limit`` synthetic rows; fails the first call when ``fail_first``."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.calls: list[dict[str, Any]] = []

    def __call__(self, conn: Any, **kwargs: Any) -> list[SearchResult]:
        self.calls.append(kwargs)
        if self.fail_first and len(self.calls) == 1:
            raise OllamaEmbedError("synthetic outage", transient=True)
        return [result(n) for n in range(min(kwargs["limit"], 5))]


# --- build_stages ----------------------------------------------------------


def test_off_builds_no_stage() -> None:
    assert build_stages({}, embedder=FakeEmbedder()) == ()
    assert build_stages({"BRAIN_DOC_LEG": "false"}, embedder=FakeEmbedder()) == ()


def test_on_builds_one_doc_leg_stage_bound_to_the_embedder() -> None:
    embedder = FakeEmbedder()

    (stage,) = build_stages({**ON, "BRAIN_DOC_LEG_WEIGHT": "2.5"}, embedder=embedder)

    assert isinstance(stage, DocLegStage)
    assert stage.name == DOC_LEG_STAGE_NAME
    assert stage.embedder is embedder
    assert stage.weight == 2.5
    assert stage.signature() == {"doc_leg_weight": 2.5}


def test_the_doc_leg_runs_before_the_reranker() -> None:
    stages = build_stages({**ON, "BRAIN_RERANKER": "llamacpp"}, embedder=FakeEmbedder())

    assert [type(s) for s in stages] == [DocLegStage, RerankStage]


def test_no_rerank_keeps_the_doc_leg() -> None:
    stages = build_stages(
        {**ON, "BRAIN_RERANKER": "llamacpp"}, embedder=FakeEmbedder(), rerank=False
    )

    assert [type(s) for s in stages] == [DocLegStage]


def test_a_bad_doc_leg_knob_is_a_config_error_even_while_off() -> None:
    with pytest.raises(ConfigError, match="BRAIN_DOC_LEG_WEIGHT"):
        build_stages({"BRAIN_DOC_LEG_WEIGHT": "heavy"}, embedder=FakeEmbedder())


def test_override_stages_follows_the_setting_when_the_flag_is_absent() -> None:
    assert override_stages(None, embedder=FakeEmbedder()) is None


def test_override_stages_passes_the_embedder_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_DOC_LEG", "true")
    embedder = FakeEmbedder()

    stages = override_stages(False, embedder=embedder)

    assert stages is not None
    (stage,) = stages
    assert isinstance(stage, DocLegStage)
    assert stage.embedder is embedder


def test_stages_none_builds_with_the_search_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_DOC_LEG", "true")

    out = run_search(_Search(), _CONN, embedder=NullEmbedder(), query="q", limit=2)

    # Under the FTS-only embedder the leg is built but gated off: proven by
    # the report, and by the rows staying in search order.
    assert [r.name for r in out.stages] == [DOC_LEG_STAGE_NAME]
    assert out.stages[0].skipped_reason == TEXT_ONLY_SKIP_REASON
    assert _ids(out.results) == ["doc-0", "doc-1"]


# --- the flag reader -------------------------------------------------------


def test_a_stage_without_the_attribute_does_not_need_the_query_vector() -> None:
    assert not stage_needs_query_vector(ReverseStage())
    assert not stage_needs_query_vector(object())


def test_the_doc_leg_and_a_declaring_stage_need_it() -> None:
    assert stage_needs_query_vector(DocLegStage(embedder=FakeEmbedder(), weight=1.0))
    assert stage_needs_query_vector(VectorReverseStage())


# --- search_was_text_only --------------------------------------------------


def test_a_vector_search_is_not_text_only() -> None:
    assert not search_was_text_only(FakeEmbedder(), fallback=None, fts_only=False)


def test_a_fallback_is_text_only() -> None:
    fell_back = SearchFallback(reason="OllamaEmbedError", transient=True)

    assert search_was_text_only(FakeEmbedder(), fallback=fell_back, fts_only=False)


def test_fts_only_is_text_only() -> None:
    assert search_was_text_only(FakeEmbedder(), fallback=None, fts_only=True)


def test_an_embedder_without_vectors_is_text_only() -> None:
    assert search_was_text_only(NullEmbedder(), fallback=None, fts_only=False)


# --- the gate in apply_stages ----------------------------------------------


def test_the_gate_skips_a_vector_stage_without_calling_it() -> None:
    vector_stage = VectorReverseStage("vec")
    plain = ReverseStage("plain")
    rows = [result(n) for n in range(3)]

    out, reports = apply_stages(
        _CONN,
        query="q",
        session_id=None,
        results=rows,
        stages=(vector_stage, plain),
        text_only=True,
    )

    assert vector_stage.calls == []
    assert reports[0].name == "vec"
    assert not reports[0].applied
    assert reports[0].skipped_reason == TEXT_ONLY_SKIP_REASON
    assert reports[1].applied
    assert _ids(out) == ["doc-2", "doc-1", "doc-0"]


def test_without_the_gate_a_vector_stage_runs() -> None:
    vector_stage = VectorReverseStage("vec")
    rows = [result(n) for n in range(3)]

    out, reports = apply_stages(
        _CONN, query="q", session_id=None, results=rows, stages=(vector_stage,)
    )

    assert len(vector_stage.calls) == 1
    assert reports[0].applied
    assert _ids(out) == ["doc-2", "doc-1", "doc-0"]


def test_run_search_gates_a_vector_stage_after_a_fallback() -> None:
    search = _Search(fail_first=True)
    vector_stage = VectorReverseStage("vec")

    out = run_search(
        search, _CONN, embedder=FakeEmbedder(), query="q", limit=2, stages=(vector_stage,)
    )

    assert out.fallback is not None
    assert vector_stage.calls == []
    assert out.stages[0].skipped_reason == TEXT_ONLY_SKIP_REASON
    assert _ids(out.results) == ["doc-0", "doc-1"]


def test_run_search_gates_a_vector_stage_under_fts_only() -> None:
    vector_stage = VectorReverseStage("vec")

    out = run_search(
        _Search(),
        _CONN,
        embedder=FakeEmbedder(),
        query="q",
        limit=2,
        stages=(vector_stage,),
        fts_only=True,
    )

    assert vector_stage.calls == []
    assert not out.stages[0].applied


def test_run_search_applies_a_vector_stage_on_an_ordinary_search() -> None:
    vector_stage = VectorReverseStage("vec")

    out = run_search(
        _Search(), _CONN, embedder=FakeEmbedder(), query="q", limit=5, stages=(vector_stage,)
    )

    assert len(vector_stage.calls) == 1
    assert out.stages[0].applied
    assert _ids(out.results) == ["doc-4", "doc-3", "doc-2", "doc-1", "doc-0"]
