"""Stage time in the footer and in ``brain ui``'s ``timing_ms`` (RM-14a).

``SearchDiagnostics.total_ms`` is ``hybrid_search``'s own wall clock; a ranking
stage runs after it. So wherever a total is SHOWN beside a stage's time, the
stage's time is added to it, or the footer would read ``rerank 412ms · total
8ms``. With no stage, both projections are exactly what they were.
"""
from __future__ import annotations

from typing import Any

from brain.format_search import search_meta_line, timing_with_stages
from brain.search import SearchDiagnostics
from brain.search_pipeline import StageReport


def _diag(total_ms: float | None = 6041.7) -> SearchDiagnostics:
    return SearchDiagnostics(
        fts_count=50, total_documents=544, embed_ms=5820.4,
        embed_cached=False, sql_ms=214.1, total_ms=total_ms,
    )


RERANK = StageReport(name="rerank", applied=True, skipped_reason=None, ms=412.3)
SKIPPED = StageReport(name="rerank", applied=False, skipped_reason="RerankError", ms=5001.0)


def test_no_stage_leaves_the_footer_as_it_was() -> None:
    assert search_meta_line(_diag(), returned=3, stages=()) == search_meta_line(
        _diag(), returned=3
    ) == "544 matched · 3 shown · embed 5820ms · sql 214ms · total 6042ms"


def test_a_stage_adds_its_segment_and_its_time_to_the_total() -> None:
    assert search_meta_line(_diag(), returned=3, stages=[RERANK]) == (
        "544 matched · 3 shown · embed 5820ms · sql 214ms · rerank 412ms · total 6454ms"
    )


def test_a_skipped_stage_still_cost_its_time() -> None:
    line = search_meta_line(_diag(), returned=3, stages=[SKIPPED])
    assert "rerank 5001ms" in line
    assert line.endswith("total 11043ms")


def test_an_unknown_total_stays_unknown() -> None:
    line = search_meta_line(_diag(total_ms=None), returned=3, stages=[RERANK])
    assert line.endswith("rerank 412ms")


def _meta() -> dict[str, Any]:
    return {
        "returned": 3,
        "timing_ms": {"embed": 5820.4, "sql": 214.1, "facets": None, "total": 6041.7},
        "fts_only": False,
    }


def test_timing_with_no_stage_is_the_meta_unchanged() -> None:
    meta = _meta()
    assert timing_with_stages(meta, ()) == _meta()


def test_timing_gains_the_stage_and_a_total_that_includes_it() -> None:
    meta = _meta()

    out = timing_with_stages(meta, [RERANK])

    assert out["timing_ms"] == {
        "embed": 5820.4, "sql": 214.1, "facets": None, "total": 6454.0, "rerank": 412.3,
    }
    assert out["returned"] == 3
    assert meta == _meta()  # not mutated


def test_timing_keeps_a_null_total_null() -> None:
    meta = {**_meta(), "timing_ms": {**_meta()["timing_ms"], "total": None}}
    assert timing_with_stages(meta, [RERANK])["timing_ms"]["total"] is None
