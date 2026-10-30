"""``search_pipeline.build_stages`` wires the reranker, and ``size_stages`` sizes it (RM-14a).

Every test passes a plain ``dict`` as ``env``. All values are synthetic.
"""
from __future__ import annotations

from typing import Any

import pytest

from brain.config import ConfigError
from brain.rerank import RerankStage
from brain.rerank_backends import LlamaCppReranker, VoyageReranker
from brain.search_pipeline import build_stages, size_stages
from tests.search_stages import ReverseStage

LLAMACPP = {"BRAIN_RERANKER": "llamacpp"}


def test_a_configured_backend_adds_one_rerank_stage() -> None:
    (stage,) = build_stages(
        {
            **LLAMACPP,
            "BRAIN_RERANK_DEPTH": "12",
            "BRAIN_RERANK_MAX_CHARS": "800",
            "BRAIN_RERANK_TIMEOUT_S": "2",
        }
    )

    assert isinstance(stage, RerankStage)
    assert isinstance(stage.reranker, LlamaCppReranker)
    assert (stage.depth, stage.max_chars, stage.timeout) == (12, 800, 2.0)
    assert stage.signature() == {
        "reranker": "llamacpp",
        "reranker_model": "bge-reranker-v2-m3",
        "rerank_depth": 12,
    }


def test_voyage_is_wired_as_a_hosted_backend() -> None:
    (stage,) = build_stages({"BRAIN_RERANKER": "voyage", "VOYAGE_API_KEY": "pa-synthetic"})
    assert isinstance(stage, RerankStage)
    assert isinstance(stage.reranker, VoyageReranker)
    assert stage.reranker.hosted_egress is True


def test_no_rerank_drops_a_configured_stage() -> None:
    assert build_stages(LLAMACPP, rerank=False) == ()


def test_rerank_true_keeps_a_configured_stage() -> None:
    (stage,) = build_stages(LLAMACPP, rerank=True)
    assert isinstance(stage, RerankStage)


def test_rerank_true_without_a_backend_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="BRAIN_RERANKER"):
        build_stages({}, rerank=True)


def test_a_bad_knob_is_a_config_error_even_while_off() -> None:
    with pytest.raises(ConfigError, match="BRAIN_RERANK_DEPTH"):
        build_stages({"BRAIN_RERANK_DEPTH": "500"})


def test_size_stages_takes_the_deepest_candidate_limit() -> None:
    shallow, deep = ReverseStage("a", depth=3), ReverseStage("b", depth=30)

    stages, fetch = size_stages([shallow, deep], 10)

    assert stages == (shallow, deep)
    assert fetch == 30


def test_size_stages_never_fetches_less_than_the_limit() -> None:
    assert size_stages([], 7) == ((), 7)


class _Unsizable(ReverseStage):
    def candidate_limit(self, limit: int) -> Any:
        raise RuntimeError("synthetic")


def test_size_stages_replaces_an_unsizable_stage() -> None:
    stages, fetch = size_stages([_Unsizable("u")], 5)
    assert fetch == 5
    assert stages[0].name == "u"
    assert not isinstance(stages[0], _Unsizable)
