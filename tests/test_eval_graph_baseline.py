"""Tests for brain.eval.graph_baseline — graph-eval baseline save/load + diff.

The graph analogue of ``tests/test_eval_baseline.py`` for the parallel
:class:`brain.eval.graph_runner.GraphEvalReport` (wave G4-d; spec §17d Q3). Pure
unit tests (hand-constructed reports, no DB / no Ollama). All data is synthetic.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from brain.eval.errors import EvalBaselineError
from brain.eval.graph_baseline import (
    diff_graph_reports,
    load_graph_baseline,
    save_graph_baseline,
)
from brain.eval.graph_runner import (
    GraphDocEvalResult,
    GraphEvalReport,
    GraphThemesEvalResult,
)

# ---------------------------------------------------------------------------
# Fixture helpers (synthetic — no PII)
# ---------------------------------------------------------------------------


def _doc_result(
    *,
    mode: str = "local",
    query: str = "pricing",
    ndcg: float = 1.0,
    mrr: float = 1.0,
    recall: float = 1.0,
) -> GraphDocEvalResult:
    return GraphDocEvalResult(
        mode=mode,
        query=query,
        expected_doc_ids=["doc-1", "doc-2", "doc-3"],
        actual_doc_ids=["doc-1", "doc-2", "doc-3"],
        ndcg_at_k=ndcg,
        mrr=mrr,
        recall_at_k=recall,
        ndcg_k=5,
        recall_k=20,
    )


def _themes_result(
    *, person: str = "dana lee", f1: float = 1.0, precision: float = 1.0, recall: float = 1.0
) -> GraphThemesEvalResult:
    return GraphThemesEvalResult(
        person=person,
        expected_theme_keysets=[["billing", "pricing", "acmepay"], ["analytics", "roadmap"]],
        actual_theme_keysets=[["billing", "pricing", "acmepay"], ["analytics", "roadmap"]],
        precision=precision,
        recall=recall,
        f1=f1,
        matched=2,
        n_expected=2,
        n_actual=2,
    )


def _make_report(
    *,
    doc_results: list[GraphDocEvalResult] | None = None,
    themes_results: list[GraphThemesEvalResult] | None = None,
    backend: str = "age-test",
) -> GraphEvalReport:
    if doc_results is None:
        doc_results = [_doc_result(mode="local"), _doc_result(mode="fuse")]
    if themes_results is None:
        themes_results = [_themes_result()]
    local = [r for r in doc_results if r.mode == "local"]
    fuse = [r for r in doc_results if r.mode == "fuse"]

    def _mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    sig: dict[str, Any] = {
        "graph_depth": 2,
        "graph_frontier_cap": 200,
        "graph_min_edge_weight": 0.2,
        "graph_theme_limit": 5,
        "backend": backend,
        "include_fuse": True,
        "ndcg_k": 5,
        "recall_k": 20,
    }
    return GraphEvalReport(
        doc_results=doc_results,
        themes_results=themes_results,
        mean_local_ndcg_at_k=_mean([r.ndcg_at_k for r in local]),
        mean_local_mrr=_mean([r.mrr for r in local]),
        mean_local_recall_at_k=_mean([r.recall_at_k for r in local]),
        mean_fuse_ndcg_at_k=_mean([r.ndcg_at_k for r in fuse]),
        mean_fuse_mrr=_mean([r.mrr for r in fuse]),
        mean_fuse_recall_at_k=_mean([r.recall_at_k for r in fuse]),
        mean_themes_precision=_mean([r.precision for r in themes_results]),
        mean_themes_recall=_mean([r.recall for r in themes_results]),
        mean_themes_f1=_mean([r.f1 for r in themes_results]),
        config_signature=sig,
        generated_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Save / load roundtrip
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """A saved GraphEvalReport loads back with the same field values."""
    report = _make_report()
    path = tmp_path / "graph.json"
    save_graph_baseline(report, path=path)

    loaded = load_graph_baseline(path)
    assert loaded.mean_local_recall_at_k == pytest.approx(
        report.mean_local_recall_at_k, abs=1e-4
    )
    assert loaded.mean_fuse_recall_at_k == pytest.approx(
        report.mean_fuse_recall_at_k, abs=1e-4
    )
    assert loaded.mean_themes_f1 == pytest.approx(report.mean_themes_f1, abs=1e-4)
    assert loaded.config_signature == report.config_signature
    assert loaded.generated_at == report.generated_at
    assert len(loaded.doc_results) == len(report.doc_results)
    assert len(loaded.themes_results) == len(report.themes_results)
    assert {r.mode for r in loaded.doc_results} == {"local", "fuse"}
    # Keysets round-trip as lists, preserving order.
    assert loaded.themes_results[0].expected_theme_keysets == [
        ["billing", "pricing", "acmepay"],
        ["analytics", "roadmap"],
    ]


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    """save_graph_baseline creates missing parent directories."""
    deep = tmp_path / "a" / "b" / "graph.json"
    save_graph_baseline(_make_report(), path=deep)
    assert deep.exists()


def test_save_floats_rounded_to_4_decimals(tmp_path: Path) -> None:
    """Serialized floats are rounded to 4 decimals for byte-stable diffs."""
    report = _make_report(
        doc_results=[
            _doc_result(mode="local", ndcg=0.123456789),
            _doc_result(mode="fuse"),
        ]
    )
    path = tmp_path / "graph.json"
    save_graph_baseline(report, path=path)
    data = json.loads(path.read_text())
    raw = data["mean_local_ndcg_at_k"]
    assert raw == round(raw, 4), f"float not rounded to 4 decimals: {raw}"


# ---------------------------------------------------------------------------
# Diff reports
# ---------------------------------------------------------------------------


def test_diff_zero_delta_when_identical() -> None:
    """Diffing a report against itself → all deltas 0.0, no config change."""
    report = _make_report()
    diff = diff_graph_reports(report, report)
    assert diff.config_signature_changed is False
    assert diff.mean_local_recall_at_k_delta == pytest.approx(0.0)
    assert diff.mean_fuse_recall_at_k_delta == pytest.approx(0.0)
    assert diff.mean_themes_f1_delta == pytest.approx(0.0)
    assert len(diff.per_doc) == len(report.doc_results)
    assert len(diff.per_themes) == len(report.themes_results)
    for dd in diff.per_doc:
        assert dd.ndcg_at_k_delta == pytest.approx(0.0)
        assert dd.recall_at_k_delta == pytest.approx(0.0)
    for td in diff.per_themes:
        assert td.f1_delta == pytest.approx(0.0)


def test_diff_flags_config_change() -> None:
    """A different backend name → config_signature_changed=True."""
    baseline = _make_report(backend="age-test")
    current = _make_report(backend="neo4j")
    diff = diff_graph_reports(baseline, current)
    assert diff.config_signature_changed is True
    assert diff.baseline_signature["backend"] == "age-test"
    assert diff.current_signature["backend"] == "neo4j"


def test_diff_per_doc_delta_known_change() -> None:
    """A known local-mode score change produces the expected delta sign."""
    baseline = _make_report(
        doc_results=[_doc_result(mode="local", recall=0.5), _doc_result(mode="fuse")]
    )
    current = _make_report(
        doc_results=[_doc_result(mode="local", recall=1.0), _doc_result(mode="fuse")]
    )
    diff = diff_graph_reports(baseline, current)
    local_diff = next(d for d in diff.per_doc if d.mode == "local")
    assert local_diff.recall_at_k_delta == pytest.approx(0.5, abs=1e-6)
    assert diff.mean_local_recall_at_k_delta == pytest.approx(0.5, abs=1e-6)


def test_diff_regression_is_negative_delta() -> None:
    """A themes F1 drop (regression) shows as a negative delta."""
    baseline = _make_report(themes_results=[_themes_result(f1=1.0)])
    current = _make_report(themes_results=[_themes_result(f1=0.4)])
    diff = diff_graph_reports(baseline, current)
    assert diff.mean_themes_f1_delta < 0
    (td,) = diff.per_themes
    assert td.f1_delta == pytest.approx(-0.6, abs=1e-6)


# ---------------------------------------------------------------------------
# Atomic write safety
# ---------------------------------------------------------------------------


class _FakeOs:
    """Minimal os-module stub that delegates everything except ``replace``."""

    def __init__(self, replace_impl: Any) -> None:
        import os as _real_os

        self._real = _real_os
        self._replace_impl = replace_impl

    def replace(self, src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        self._replace_impl(src, dst)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_save_graph_baseline_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-flight failure leaves no partial or temp file on disk."""
    report = _make_report()
    out_path = tmp_path / "graph.json"

    def _raise_on_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("simulated disk full")

    import brain.vault._atomic as _atomic_mod

    monkeypatch.setattr(_atomic_mod, "os", _FakeOs(_raise_on_replace))

    with pytest.raises(OSError, match="simulated disk full"):
        save_graph_baseline(report, path=out_path)

    assert not out_path.exists()
    assert not out_path.with_name(out_path.name + ".tmp").exists()


# ---------------------------------------------------------------------------
# Load errors
# ---------------------------------------------------------------------------


def test_load_missing_raises(tmp_path: Path) -> None:
    """Loading a non-existent file raises EvalBaselineError with the path."""
    missing = tmp_path / "no_such.json"
    with pytest.raises(EvalBaselineError, match="not found"):
        load_graph_baseline(missing)


def test_load_invalid_json_raises(tmp_path: Path) -> None:
    """A file containing invalid JSON raises EvalBaselineError."""
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json }{", encoding="utf-8")
    with pytest.raises(EvalBaselineError, match="invalid JSON"):
        load_graph_baseline(bad)


def test_load_missing_field_raises(tmp_path: Path) -> None:
    """A JSON file missing required fields raises EvalBaselineError."""
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"doc_results": []}), encoding="utf-8")
    with pytest.raises(EvalBaselineError, match="unexpected structure"):
        load_graph_baseline(partial)
