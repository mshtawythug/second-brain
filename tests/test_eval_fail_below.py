"""Task 5.5 — the ``brain eval --fail-below`` regression gate.

Contract (verbatim from CLAUDE.md / the overhaul plan):
- ``--fail-below`` requires ``--diff`` (else exit 2, BadParameter).
- Exits **3** when any mean metric (nDCG@5, MRR, Recall@20) regresses by more
  than ``1e-4``, comparing the 4-decimal-ROUNDED delta so the boundary at
  exactly ``-0.0001`` is constructible (passes) and ``-0.0002`` fails.
- The check runs in BOTH the ``--json`` and human output branches.
"""
import json
import os
from pathlib import Path

import psycopg
import pytest
import yaml
from typer.testing import CliRunner

from brain.cli import app
from brain.eval.baseline import BaselineDiff, QueryDiff, mean_metrics_regressed
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _diff(*, ndcg: float, mrr: float, recall: float) -> BaselineDiff:
    """Build a BaselineDiff with the three mean deltas set directly."""
    return BaselineDiff(
        per_query=[
            QueryDiff(
                query="q", category="semantic",
                ndcg_at_5_delta=ndcg, mrr_delta=mrr, recall_at_20_delta=recall,
            )
        ],
        mean_ndcg_at_5_delta=ndcg,
        mean_mrr_delta=mrr,
        mean_recall_at_20_delta=recall,
        config_signature_changed=False,
        baseline_signature={},
        current_signature={},
        added_queries=[],
        removed_queries=[],
    )


# ---------------------------------------------------------------------------
# mean_metrics_regressed — the boundary logic (no DB)
# ---------------------------------------------------------------------------


def test_regressed_false_when_no_change() -> None:
    assert mean_metrics_regressed(_diff(ndcg=0.0, mrr=0.0, recall=0.0)) is False


def test_regressed_false_when_improved() -> None:
    assert mean_metrics_regressed(_diff(ndcg=0.5, mrr=0.5, recall=0.5)) is False


def test_regressed_false_exactly_at_boundary() -> None:
    # -0.0001 rounds to -0.0001, which is NOT < -1e-4 → boundary passes.
    assert mean_metrics_regressed(_diff(ndcg=-0.0001, mrr=0.0, recall=0.0)) is False


def test_regressed_true_two_units_past_boundary() -> None:
    assert mean_metrics_regressed(_diff(ndcg=-0.0002, mrr=0.0, recall=0.0)) is True


def test_regressed_true_when_only_mrr_drops() -> None:
    assert mean_metrics_regressed(_diff(ndcg=0.1, mrr=-0.002, recall=0.1)) is True


def test_regressed_true_when_only_recall_drops() -> None:
    assert mean_metrics_regressed(_diff(ndcg=0.1, mrr=0.1, recall=-0.01)) is True


# ---------------------------------------------------------------------------
# CLI: --fail-below requires --diff (no DB — validation precedes corpus/DB)
# ---------------------------------------------------------------------------


def test_fail_below_requires_diff_exit_2() -> None:
    result = CliRunner().invoke(app, ["eval", "--fail-below"])
    assert result.exit_code == 2, result.output
    combined = result.output + (result.stderr if result.stderr else "")
    assert "--diff" in combined


# ---------------------------------------------------------------------------
# CLI integration (DB): exit 3 on regression, 0 when clean, in BOTH branches
# ---------------------------------------------------------------------------

_MINI_CORPUS = {
    "version": 1,
    "queries": [
        {
            "query": "failbelow alpha content",
            "category": "semantic",
            "expected_doc_ids": ["00000000-0000-0000-0000-000000000000"],
            "notes": "placeholder",
        },
        {
            "query": "failbelow beta content",
            "category": "people",
            "expected_doc_ids": ["00000000-0000-0000-0000-000000000000"],
            "notes": "placeholder",
        },
    ],
}


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> tuple[Path, Path]:
    """Seed a tiny corpus + DB, return (corpus_path, baselines_dir)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    for i in range(2):
        ingest_document(
            test_db,
            embedder=fake_embedder,  # type: ignore[arg-type]
            doc=ExtractedDoc(
                title=f"FailBelow Doc {i}",
                content=f"failbelow {'alpha' if i % 2 == 0 else 'beta'} content {i}",
                content_type="txt",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
        )
    corpus_path = tmp_path / "corpus.yaml"
    corpus_path.write_text(yaml.dump(_MINI_CORPUS), encoding="utf-8")
    baselines_dir = tmp_path / "baselines"
    monkeypatch.setattr("brain.cli._BASELINES_DIR", baselines_dir)
    return corpus_path, baselines_dir


def _record_and_bump(
    corpus_path: Path, baselines_dir: Path, *, bump: float, name: str
) -> None:
    """Record a baseline from the current run, then add ``bump`` to every
    per-query ndcg_at_5 so ``current - baseline`` regresses by exactly ``bump``.
    """
    r = CliRunner().invoke(
        app, ["eval", "--corpus", str(corpus_path), "--record-baseline", name]
    )
    assert r.exit_code == 0, r.output
    path = baselines_dir / f"{name}.json"
    data = json.loads(path.read_text())
    # Sanity: the placeholder expected ids match nothing → current metrics are 0.
    for result in data["results"]:
        assert result["ndcg_at_5"] == 0.0
        result["ndcg_at_5"] = round(result["ndcg_at_5"] + bump, 4)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_fail_below_clean_diff_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """A no-regression diff with --fail-below still exits 0."""
    corpus_path, baselines_dir = _prepare(monkeypatch, test_db, fake_embedder, tmp_path)
    r = CliRunner().invoke(
        app, ["eval", "--corpus", str(corpus_path), "--record-baseline", "clean"]
    )
    assert r.exit_code == 0, r.output
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "clean", "--diff",
         "--fail-below"],
    )
    assert result.exit_code == 0, result.output


def test_fail_below_boundary_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """A delta of exactly -0.0001 is the boundary → exit 0."""
    corpus_path, baselines_dir = _prepare(monkeypatch, test_db, fake_embedder, tmp_path)
    _record_and_bump(corpus_path, baselines_dir, bump=0.0001, name="boundary")
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "boundary", "--diff",
         "--fail-below"],
    )
    assert result.exit_code == 0, result.output


def test_fail_below_regression_exits_3_human(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """A -0.0002 regression exits 3 in the human (table) branch."""
    corpus_path, baselines_dir = _prepare(monkeypatch, test_db, fake_embedder, tmp_path)
    _record_and_bump(corpus_path, baselines_dir, bump=0.0002, name="regressed")
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "regressed", "--diff",
         "--fail-below"],
    )
    assert result.exit_code == 3, result.output


def test_fail_below_regression_exits_3_json(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """The gate also fires in the --json branch (plan caveat i)."""
    corpus_path, baselines_dir = _prepare(monkeypatch, test_db, fake_embedder, tmp_path)
    _record_and_bump(corpus_path, baselines_dir, bump=0.0002, name="regjson")
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "regjson", "--diff",
         "--fail-below", "--json"],
    )
    assert result.exit_code == 3, result.output
    # The JSON diff payload is still emitted before the non-zero exit.
    assert '"mean_ndcg_at_5_delta"' in result.output
