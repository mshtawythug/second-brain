"""Tests for brain.eval.baseline — save/load roundtrip and diff logic."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.eval.baseline import (
    _assert_baseline_name,
    diff_reports,
    load_baseline,
    save_baseline,
)
from brain.eval.errors import EvalBaselineError
from brain.eval.runner import CategorySummary, EvalReport, EvalResult

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_result(
    query: str = "test query",
    category: str = "semantic",
    ndcg: float = 0.8,
    mrr_score: float = 0.9,
    recall: float = 1.0,
) -> EvalResult:
    return EvalResult(
        query=query,
        category=category,
        expected_doc_ids=["abc12345-0000-0000-0000-000000000000"],
        actual_doc_ids=["abc12345-0000-0000-0000-000000000000", "other-doc"],
        ndcg_at_5=ndcg,
        mrr=mrr_score,
        recall_at_20=recall,
    )


def _make_report(
    results: list[EvalResult] | None = None,
    recency_halflife: float = 180.0,
    embedder: str = "arctic",
) -> EvalReport:
    if results is None:
        results = [_make_result()]
    cat_map: dict[str, list[EvalResult]] = {}
    for r in results:
        cat_map.setdefault(r.category, []).append(r)
    per_category = {
        cat: CategorySummary(
            category=cat,
            count=len(rs),
            mean_ndcg_at_5=sum(r.ndcg_at_5 for r in rs) / len(rs),
            mean_mrr=sum(r.mrr for r in rs) / len(rs),
            mean_recall_at_20=sum(r.recall_at_20 for r in rs) / len(rs),
        )
        for cat, rs in cat_map.items()
    }
    n = len(results)
    return EvalReport(
        results=results,
        mean_ndcg_at_5=sum(r.ndcg_at_5 for r in results) / n,
        mean_mrr=sum(r.mrr for r in results) / n,
        mean_recall_at_20=sum(r.recall_at_20 for r in results) / n,
        per_category=per_category,
        config_signature={
            "recency_halflife_days": recency_halflife,
            "snippet_context_tokens": 200,
            "vector_sim_floor": 0.25,
            "embedder": embedder,
        },
        generated_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Save / load roundtrip
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """A saved EvalReport loads back with the same field values."""
    report = _make_report()
    path = tmp_path / "baseline.json"
    save_baseline(report, path=path)

    loaded = load_baseline(path)
    assert loaded.mean_ndcg_at_5 == pytest.approx(report.mean_ndcg_at_5, abs=1e-4)
    assert loaded.mean_mrr == pytest.approx(report.mean_mrr, abs=1e-4)
    assert loaded.mean_recall_at_20 == pytest.approx(report.mean_recall_at_20, abs=1e-4)
    assert loaded.config_signature == report.config_signature
    assert loaded.generated_at == report.generated_at
    assert len(loaded.results) == len(report.results)
    r0 = loaded.results[0]
    assert r0.query == report.results[0].query
    assert r0.ndcg_at_5 == pytest.approx(report.results[0].ndcg_at_5, abs=1e-4)
    assert "semantic" in loaded.per_category
    assert loaded.per_category["semantic"].count == 1


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    """save_baseline creates missing parent directories."""
    deep_path = tmp_path / "a" / "b" / "c" / "baseline.json"
    save_baseline(_make_report(), path=deep_path)
    assert deep_path.exists()


def test_save_floats_rounded_to_4_decimals(tmp_path: Path) -> None:
    """Serialized floats are rounded to 4 decimal places for byte-stable diffs."""
    import json

    report = _make_report(results=[_make_result(ndcg=0.123456789)])
    path = tmp_path / "b.json"
    save_baseline(report, path=path)
    data = json.loads(path.read_text())
    # mean_ndcg_at_5 should be rounded to 4 decimals
    raw = data["mean_ndcg_at_5"]
    assert raw == round(raw, 4), f"float not rounded to 4 decimals: {raw}"


# ---------------------------------------------------------------------------
# Diff reports
# ---------------------------------------------------------------------------


def test_diff_reports_zero_delta_when_identical() -> None:
    """Diffing a report against itself → all deltas 0.0, no config change."""
    report = _make_report()
    diff = diff_reports(report, report)
    assert diff.mean_ndcg_at_5_delta == pytest.approx(0.0)
    assert diff.mean_mrr_delta == pytest.approx(0.0)
    assert diff.mean_recall_at_20_delta == pytest.approx(0.0)
    assert diff.config_signature_changed is False
    assert len(diff.per_query) == len(report.results)
    for qd in diff.per_query:
        assert qd.ndcg_at_5_delta == pytest.approx(0.0)
        assert qd.mrr_delta == pytest.approx(0.0)
        assert qd.recall_at_20_delta == pytest.approx(0.0)


def test_diff_reports_flags_config_change() -> None:
    """Different recency_halflife_days → config_signature_changed=True."""
    baseline = _make_report(recency_halflife=180.0)
    current = _make_report(recency_halflife=90.0)
    diff = diff_reports(baseline, current)
    assert diff.config_signature_changed is True
    assert diff.baseline_signature["recency_halflife_days"] == 180.0
    assert diff.current_signature["recency_halflife_days"] == 90.0


def test_diff_reports_per_query_delta() -> None:
    """A known score change produces the expected delta sign and magnitude."""
    base_result = _make_result(query="q1", ndcg=0.5, mrr_score=0.5, recall=0.5)
    curr_result = _make_result(query="q1", ndcg=0.8, mrr_score=0.7, recall=1.0)
    baseline = _make_report(results=[base_result])
    current = _make_report(results=[curr_result])
    diff = diff_reports(baseline, current)
    (qd,) = diff.per_query
    assert qd.query == "q1"
    assert qd.ndcg_at_5_delta == pytest.approx(0.3, abs=1e-6)
    assert qd.mrr_delta == pytest.approx(0.2, abs=1e-6)
    assert qd.recall_at_20_delta == pytest.approx(0.5, abs=1e-6)
    assert diff.mean_ndcg_at_5_delta == pytest.approx(0.3, abs=1e-6)


def test_diff_reports_regression_is_negative_delta() -> None:
    """A performance drop (regression) shows as a negative delta."""
    base_result = _make_result(ndcg=0.9, mrr_score=1.0, recall=1.0)
    curr_result = _make_result(ndcg=0.5, mrr_score=0.5, recall=0.5)
    diff = diff_reports(_make_report([base_result]), _make_report([curr_result]))
    (qd,) = diff.per_query
    assert qd.ndcg_at_5_delta < 0


# ---------------------------------------------------------------------------
# Atomic write safety
# ---------------------------------------------------------------------------


def test_save_baseline_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-flight failure leaves no partial or temp file on disk."""
    report = _make_report()
    out_path = tmp_path / "baseline.json"

    def _raise_on_replace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
        raise OSError("simulated disk full")

    # Patch os.replace in the _atomic module so the sibling-tempfile rename fails.
    import brain.vault._atomic as _atomic_mod  # noqa: PLC0415
    monkeypatch.setattr(_atomic_mod, "os", _make_failing_os(_raise_on_replace))

    with pytest.raises(OSError, match="simulated disk full"):
        save_baseline(report, path=out_path)

    # The destination file must NOT exist (write was interrupted).
    assert not out_path.exists()
    # The .tmp sibling must also be cleaned up by atomic_write_text's except block.
    tmp_sibling = out_path.with_name(out_path.name + ".tmp")
    assert not tmp_sibling.exists()


class _FakeOs:
    """Minimal os-module stub that delegates everything except ``replace``."""

    def __init__(self, replace_impl):  # type: ignore[no-untyped-def]
        import os as _real_os

        self._real = _real_os
        self._replace_impl = replace_impl

    def replace(self, src: str | os.PathLike, dst: str | os.PathLike) -> None:
        self._replace_impl(src, dst)

    def __getattr__(self, name: str):  # type: ignore[return]
        return getattr(self._real, name)


def _make_failing_os(replace_impl):  # type: ignore[no-untyped-def]
    return _FakeOs(replace_impl)


# ---------------------------------------------------------------------------
# Load errors
# ---------------------------------------------------------------------------


def test_load_baseline_missing_raises(tmp_path: Path) -> None:
    """Loading a non-existent file raises EvalBaselineError with the path."""
    missing = tmp_path / "no_such_file.json"
    with pytest.raises(EvalBaselineError, match=str(missing)):
        load_baseline(missing)


def test_load_baseline_invalid_json_raises(tmp_path: Path) -> None:
    """A file containing invalid JSON raises EvalBaselineError."""
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json }{", encoding="utf-8")
    with pytest.raises(EvalBaselineError, match="invalid JSON"):
        load_baseline(bad)


def test_load_baseline_missing_field_raises(tmp_path: Path) -> None:
    """A JSON file missing required fields raises EvalBaselineError."""
    import json

    bad = tmp_path / "partial.json"
    bad.write_text(json.dumps({"results": []}), encoding="utf-8")
    with pytest.raises(EvalBaselineError, match="unexpected structure"):
        load_baseline(bad)


# ---------------------------------------------------------------------------
# Baseline name validator
# ---------------------------------------------------------------------------


def test_baseline_name_validator_accepts_valid_names() -> None:
    """Valid names (alphanumeric + hyphens + underscores) do not raise."""
    for name in ("default", "my-baseline", "run_2026", "Q1B", "a1b2-c3"):
        _assert_baseline_name(name)  # must not raise


def test_baseline_name_validator_rejects_path_traversal() -> None:
    """Path traversal attempts raise EvalBaselineError."""
    with pytest.raises(EvalBaselineError):
        _assert_baseline_name("../etc/passwd")


def test_baseline_name_validator_rejects_slash() -> None:
    """Slashes (path separators) raise EvalBaselineError."""
    with pytest.raises(EvalBaselineError):
        _assert_baseline_name("foo/bar")


def test_baseline_name_validator_rejects_dot() -> None:
    """Dots raise EvalBaselineError (could construct .json double-extension)."""
    with pytest.raises(EvalBaselineError):
        _assert_baseline_name("foo.bar")


def test_baseline_name_validator_rejects_space() -> None:
    """Spaces raise EvalBaselineError."""
    with pytest.raises(EvalBaselineError):
        _assert_baseline_name("my baseline")
