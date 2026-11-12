"""The engagement stage in the pipeline: ``build_stages`` and its place in it (RM-22b).

No database: every ``env`` is a plain ``dict`` and a fake search function stands
in for ``hybrid_search``. All values are synthetic.
"""
from __future__ import annotations

from typing import Any

import pytest

from brain.config import ConfigError
from brain.embeddings import NullEmbedder
from brain.rerank import RerankStage
from brain.search import SearchResult
from brain.search_doc_leg import DocLegStage
from brain.search_engagement import ENGAGEMENT_STAGE_NAME, EngagementStage
from brain.search_pipeline import build_stages, override_stages, run_search
from tests.conftest import FakeEmbedder
from tests.doc_leg_fakes import result

pytestmark = pytest.mark.nodb

ON = {"BRAIN_ENGAGEMENT_ALPHA": "0.5"}


class _EmptyConn:
    """Answers the engagement statement with no rows: nothing was ever opened."""

    def execute(self, sql: str, params: Any) -> Any:
        class _Rows:
            def fetchall(self) -> list[tuple[Any, ...]]:
                return []

        return _Rows()


def _search(conn: Any, **kwargs: Any) -> list[SearchResult]:
    return [result(n) for n in range(min(kwargs["limit"], 5))]


def test_off_builds_no_stage() -> None:
    # AC1: both knobs at their default, or explicitly zero, add nothing.
    assert build_stages({}, embedder=FakeEmbedder()) == ()
    zero = {"BRAIN_ENGAGEMENT_ALPHA": "0", "BRAIN_ENGAGEMENT_BETA": "0"}
    assert build_stages(zero, embedder=FakeEmbedder()) == ()


def test_on_builds_one_engagement_stage_with_its_weights() -> None:
    (stage,) = build_stages({**ON, "BRAIN_ENGAGEMENT_BETA": "0.25"}, embedder=FakeEmbedder())

    assert isinstance(stage, EngagementStage)
    assert stage.name == ENGAGEMENT_STAGE_NAME
    assert (stage.alpha, stage.beta) == (0.5, 0.25)
    assert stage.signature() == {"engagement_alpha": 0.5, "engagement_beta": 0.25}


def test_beta_alone_builds_it() -> None:
    (stage,) = build_stages({"BRAIN_ENGAGEMENT_BETA": "0.25"}, embedder=FakeEmbedder())

    assert isinstance(stage, EngagementStage)


def test_engagement_runs_last_after_the_doc_leg_and_the_reranker() -> None:
    # D3: before the reranker, its full re-sort of the head would erase the boost.
    env = {**ON, "BRAIN_DOC_LEG": "true", "BRAIN_RERANKER": "llamacpp"}

    stages = build_stages(env, embedder=FakeEmbedder())

    assert [type(s) for s in stages] == [DocLegStage, RerankStage, EngagementStage]


def test_no_rerank_keeps_engagement() -> None:
    stages = build_stages(
        {**ON, "BRAIN_RERANKER": "llamacpp"}, embedder=FakeEmbedder(), rerank=False
    )

    assert [type(s) for s in stages] == [EngagementStage]


def test_rerank_true_without_a_backend_still_refuses() -> None:
    with pytest.raises(ConfigError, match="BRAIN_RERANKER"):
        build_stages(ON, embedder=FakeEmbedder(), rerank=True)


def test_a_bad_knob_is_a_config_error_even_while_off() -> None:
    with pytest.raises(ConfigError, match="BRAIN_ENGAGEMENT_BETA"):
        build_stages({"BRAIN_ENGAGEMENT_BETA": "lots"}, embedder=FakeEmbedder())


def test_override_stages_keeps_engagement_under_no_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_ENGAGEMENT_ALPHA", "0.5")

    stages = override_stages(False, embedder=FakeEmbedder())

    assert stages is not None
    assert [type(s) for s in stages] == [EngagementStage]


def test_stages_none_builds_it_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_ENGAGEMENT_ALPHA", "0.5")

    out = run_search(_search, _EmptyConn(), embedder=FakeEmbedder(), query="q", limit=2)  # type: ignore[arg-type]

    assert [r.name for r in out.stages] == [ENGAGEMENT_STAGE_NAME]
    assert out.stages[0].applied
    assert [r.document_id for r in out.results] == ["doc-0", "doc-1"]


def test_it_runs_after_a_text_only_search(monkeypatch: pytest.MonkeyPatch) -> None:
    # It needs no query vector, so the text-only gate does not skip it.
    monkeypatch.setenv("BRAIN_ENGAGEMENT_ALPHA", "0.5")

    out = run_search(_search, _EmptyConn(), embedder=NullEmbedder(), query="q", limit=2)  # type: ignore[arg-type]

    (report,) = out.stages
    assert (report.applied, report.skipped_reason) == (True, None)
