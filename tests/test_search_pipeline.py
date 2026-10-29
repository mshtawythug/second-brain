"""Pure tests for :mod:`brain.search_pipeline` (RM-09).

A fake ``search_fn`` stands in for ``hybrid_search``, so nothing here touches a
database: the pipeline only forwards ``conn`` to the search function and the
stages. All data is synthetic.
"""
from __future__ import annotations

from typing import Any

import pytest

from brain import search_pipeline
from brain.embeddings import OllamaEmbedError
from brain.search import SearchResult
from brain.search_pipeline import (
    PipelineResult,
    StageReport,
    apply_stages,
    build_stages,
    pipeline_json,
    run_search,
    stage_reports_json,
)
from tests.search_stages import (
    STAGE_SECRET,
    DeclinedStage,
    RaisingStage,
    ReverseStage,
)

pytestmark = pytest.mark.nodb

#: Stands in for a connection: the pipeline never uses it, only forwards it.
_CONN: Any = object()


def _result(n: int) -> SearchResult:
    return SearchResult(
        document_id=f"doc-{n}",
        title=f"Synthetic note {n}",
        source_kind="manual",
        snippet=f"snippet {n}",
        score=1.0 / (n + 1),
        content_type="note",
        tags=[],
    )


class FakeSearch:
    """Returns ``limit`` synthetic rows; can fail the first ``fail_first`` calls."""

    def __init__(self, *, available: int = 20, fail_first: int = 0) -> None:
        self.available = available
        self.fail_first = fail_first
        self.calls: list[dict[str, Any]] = []

    def __call__(self, conn: Any, **kwargs: Any) -> list[SearchResult]:
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_first:
            raise OllamaEmbedError("connection refused", transient=True)
        return [_result(n) for n in range(min(kwargs["limit"], self.available))]


def _ids(results: list[SearchResult]) -> list[str]:
    return [r.document_id for r in results]


class _Embedder:
    dim = 4


# --- run_search ----------------------------------------------------------


def test_no_stages_returns_the_search_rows_unchanged() -> None:
    # Arrange
    search = FakeSearch()

    # Act
    out = run_search(
        search, _CONN, embedder=_Embedder(), query="q", limit=5, stages=(), tag="t"
    )

    # Assert
    assert isinstance(out, PipelineResult)
    assert _ids(out.results) == [f"doc-{n}" for n in range(5)]
    assert out.fallback is None
    assert out.stages == ()
    assert len(search.calls) == 1
    assert search.calls[0]["limit"] == 5
    assert search.calls[0]["tag"] == "t"
    assert search.calls[0]["query"] == "q"


def test_a_stage_reorders_the_results() -> None:
    # Arrange
    stage = ReverseStage()

    # Act
    out = run_search(
        FakeSearch(), _CONN, embedder=_Embedder(), query="q", limit=3, stages=(stage,)
    )

    # Assert
    assert _ids(out.results) == ["doc-2", "doc-1", "doc-0"]
    assert [r.name for r in out.stages] == ["reverse"]
    assert out.stages[0].applied is True


def test_candidate_limit_deepens_the_fetch_and_the_output_is_truncated() -> None:
    # Arrange
    search = FakeSearch()
    stage = ReverseStage(depth=8)

    # Act
    out = run_search(
        search, _CONN, embedder=_Embedder(), query="q", limit=3, stages=(stage,)
    )

    # Assert
    assert search.calls[0]["limit"] == 8
    assert len(stage.calls[0]["results"]) == 8
    assert _ids(out.results) == ["doc-7", "doc-6", "doc-5"]


def test_candidate_limit_never_shrinks_the_fetch_below_limit() -> None:
    # Arrange
    search = FakeSearch()

    # Act
    run_search(
        search,
        _CONN,
        embedder=_Embedder(),
        query="q",
        limit=6,
        stages=(ReverseStage(depth=2),),
    )

    # Assert
    assert search.calls[0]["limit"] == 6


def test_session_id_and_query_reach_the_stage() -> None:
    # Arrange
    stage = ReverseStage()

    # Act
    run_search(
        FakeSearch(),
        _CONN,
        embedder=_Embedder(),
        query="harbour",
        limit=2,
        session_id="sess-1",
        stages=(stage,),
    )

    # Assert
    assert stage.calls[0]["query"] == "harbour"
    assert stage.calls[0]["session_id"] == "sess-1"


def test_fallback_retries_text_only_and_still_applies_the_stages() -> None:
    # Arrange
    search = FakeSearch(fail_first=1)
    stage = ReverseStage()

    # Act
    out = run_search(
        search, _CONN, embedder=_Embedder(), query="q", limit=2, stages=(stage,)
    )

    # Assert
    assert out.fallback is not None
    assert out.fallback.reason == "OllamaEmbedError"
    assert search.calls[1]["fts_only"] is True
    assert _ids(out.results) == ["doc-1", "doc-0"]


def test_fallback_false_lets_the_embed_error_propagate() -> None:
    # Arrange
    search = FakeSearch(fail_first=1)

    # Act / Assert
    with pytest.raises(OllamaEmbedError):
        run_search(
            search,
            _CONN,
            embedder=_Embedder(),
            query="q",
            limit=2,
            fallback=False,
            stages=(),
        )
    assert len(search.calls) == 1


def test_fallback_false_still_applies_the_stages() -> None:
    # Act
    out = run_search(
        FakeSearch(),
        _CONN,
        embedder=_Embedder(),
        query="q",
        limit=2,
        fallback=False,
        stages=(ReverseStage(),),
    )

    # Assert
    assert _ids(out.results) == ["doc-1", "doc-0"]
    assert out.fallback is None


def test_limit_cannot_also_arrive_in_kwargs() -> None:
    # Act / Assert: ``limit`` is a named parameter, so a splat carrying it
    # again is refused by Python before any search runs (RM-09 criterion 5).
    search = FakeSearch()
    with pytest.raises(TypeError):
        run_search(
            search,
            _CONN,
            embedder=_Embedder(),
            query="q",
            limit=2,
            stages=(),
            **{"limit": 4},  # type: ignore[misc]
        )
    assert search.calls == []


def test_stages_none_means_the_configured_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    seen: list[Any] = []

    def fake_build(env: Any) -> tuple[ReverseStage, ...]:
        seen.append(env)
        return (ReverseStage(),)

    monkeypatch.setattr(search_pipeline, "build_stages", fake_build)

    # Act
    out = run_search(FakeSearch(), _CONN, embedder=_Embedder(), query="q", limit=2)

    # Assert
    assert len(seen) == 1
    assert _ids(out.results) == ["doc-1", "doc-0"]


def test_build_stages_is_empty_until_a_stage_ships() -> None:
    assert build_stages({}) == ()
    assert build_stages({"BRAIN_RERANKER": "llamacpp"}) == ()


# --- apply_stages --------------------------------------------------------


def test_stages_run_in_order() -> None:
    # Arrange
    first = ReverseStage("first")
    second = ReverseStage("second")
    rows = [_result(n) for n in range(3)]

    # Act
    out, reports = apply_stages(
        _CONN, query="q", session_id=None, results=rows, stages=(first, second)
    )

    # Assert: the second stage saw the first one's output.
    assert _ids(second.calls[0]["results"]) == ["doc-2", "doc-1", "doc-0"]
    assert _ids(out) == ["doc-0", "doc-1", "doc-2"]
    assert [r.name for r in reports] == ["first", "second"]


def test_a_raising_stage_is_skipped_with_a_class_only_reason() -> None:
    # Arrange
    rows = [_result(n) for n in range(3)]
    after = ReverseStage("after")

    # Act
    out, reports = apply_stages(
        _CONN,
        query="q",
        session_id=None,
        results=rows,
        stages=(RaisingStage(), after),
    )

    # Assert: the raising stage changed nothing, and the next stage still ran
    # on the unstaged rows.
    assert reports[0] == StageReport(
        name="raising",
        applied=False,
        skipped_reason="StageBackendDown",
        ms=reports[0].ms,
    )
    assert STAGE_SECRET not in repr(reports)
    assert _ids(after.calls[0]["results"]) == ["doc-0", "doc-1", "doc-2"]
    assert _ids(out) == ["doc-2", "doc-1", "doc-0"]


def test_a_raising_stage_returns_the_unstaged_rows_through_run_search() -> None:
    # Act
    out = run_search(
        FakeSearch(),
        _CONN,
        embedder=_Embedder(),
        query="q",
        limit=3,
        stages=(RaisingStage(),),
    )

    # Assert
    assert _ids(out.results) == ["doc-0", "doc-1", "doc-2"]
    assert out.stages[0].applied is False
    assert out.stages[0].skipped_reason == "StageBackendDown"


def test_a_declined_stage_passes_its_input_on() -> None:
    # Arrange
    rows = [_result(n) for n in range(3)]

    # Act
    out, reports = apply_stages(
        _CONN, query="q", session_id=None, results=rows, stages=(DeclinedStage(),)
    )

    # Assert
    assert _ids(out) == ["doc-0", "doc-1", "doc-2"]
    assert reports[0].applied is False
    assert reports[0].skipped_reason == "disabled"


def test_reports_are_named_after_the_stage_and_timed_by_the_pipeline() -> None:
    # Arrange: the stage's own report says ``ms=0.0`` and a different name.
    class Mislabelled(ReverseStage):
        def apply(self, conn: Any, **kwargs: Any) -> Any:
            out = super().apply(conn, **kwargs)
            return type(out)(
                results=out.results,
                report=StageReport(name="other", applied=True, skipped_reason=None, ms=-1.0),
            )

    # Act
    _, reports = apply_stages(
        _CONN,
        query="q",
        session_id=None,
        results=[_result(0)],
        stages=(Mislabelled("real"),),
    )

    # Assert
    assert reports[0].name == "real"
    assert reports[0].ms >= 0.0


def test_no_stages_is_the_identity() -> None:
    # Arrange
    rows = [_result(n) for n in range(2)]

    # Act
    out, reports = apply_stages(_CONN, query="q", session_id=None, results=rows, stages=())

    # Assert
    assert out == rows
    assert reports == ()


# --- stage_reports_json --------------------------------------------------


def test_stage_reports_json_is_always_present() -> None:
    assert stage_reports_json(()) == {"stages": []}


def test_stage_reports_json_projects_every_report() -> None:
    """Timings are rounded to 0.1 ms, as ``timing_ms`` is (``format_search._round_ms``)."""
    # Arrange
    reports = (
        StageReport(name="reverse", applied=True, skipped_reason=None, ms=1.54),
        StageReport(name="raising", applied=False, skipped_reason="Boom", ms=0.26),
    )

    # Act
    payload = stage_reports_json(reports)

    # Assert
    assert payload == {
        "stages": [
            {"name": "reverse", "applied": True, "skipped_reason": None, "ms": 1.5},
            {"name": "raising", "applied": False, "skipped_reason": "Boom", "ms": 0.3},
        ]
    }


def test_pipeline_json_is_the_fallback_keys_then_stages() -> None:
    # Arrange
    result = PipelineResult(results=[], fallback=None, stages=())

    # Act
    payload = pipeline_json(result)

    # Assert
    assert list(payload) == [
        "fallback",
        "fallback_reason",
        "fallback_transient",
        "fallback_note",
        "stages",
    ]
    assert payload["stages"] == []
