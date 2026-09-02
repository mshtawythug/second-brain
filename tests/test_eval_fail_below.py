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
from brain.eval.baseline import (
    BaselineDiff,
    QueryDiff,
    changed_config_keys,
    mean_metrics_regressed,
)
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


# ---------------------------------------------------------------------------
# changed_config_keys — attribute a metric shift to the config, not to quality
#
# A changed retrieval config moves every metric exactly as a regression does,
# and --fail-below returns the SAME exit 3 for both. Without naming the keys,
# the exit code is the only evidence and it cannot distinguish the two cases.
# Exit codes are deliberately NOT changed by any of this — only the message.
# ---------------------------------------------------------------------------


def _diff_with_config(baseline_sig: dict, current_sig: dict) -> BaselineDiff:
    """A BaselineDiff with no metric movement but the given config signatures."""
    return BaselineDiff(
        per_query=[],
        mean_ndcg_at_5_delta=0.0,
        mean_mrr_delta=0.0,
        mean_recall_at_20_delta=0.0,
        config_signature_changed=baseline_sig != current_sig,
        baseline_signature=baseline_sig,
        current_signature=current_sig,
        added_queries=[],
        removed_queries=[],
    )


def test_changed_config_keys_empty_when_identical() -> None:
    sig = {"embedder": "arctic", "vector_sim_floor": 0.25}
    assert changed_config_keys(_diff_with_config(sig, dict(sig))) == []


def test_changed_config_keys_names_the_differing_key() -> None:
    before = {"embedder": "arctic", "vector_sim_floor": 0.25}
    after = {"embedder": "voyage", "vector_sim_floor": 0.25}
    assert changed_config_keys(_diff_with_config(before, after)) == [
        ("embedder", "arctic", "voyage")
    ]


def test_changed_config_keys_reports_a_key_added_to_current() -> None:
    """A newly-added knob must surface, not be skipped for missing a baseline."""
    changed = changed_config_keys(_diff_with_config({}, {"reranker": "on"}))
    assert changed == [("reranker", None, "on")]


def test_changed_config_keys_reports_a_key_dropped_from_current() -> None:
    """A removed/renamed knob must surface too."""
    changed = changed_config_keys(_diff_with_config({"reranker": "on"}, {}))
    assert changed == [("reranker", "on", None)]


def test_changed_config_keys_is_sorted_for_deterministic_output() -> None:
    before = {"zeta": 1, "alpha": 1, "mid": 1}
    after = {"zeta": 2, "alpha": 2, "mid": 2}
    assert [k for k, _, _ in changed_config_keys(_diff_with_config(before, after))] == [
        "alpha",
        "mid",
        "zeta",
    ]


def _record_with_config(
    corpus_path: Path, baselines_dir: Path, *, name: str, embedder: str, bump: float
) -> None:
    """Record a baseline, then rewrite its embedder name and bump ndcg by ``bump``."""
    r = CliRunner().invoke(
        app, ["eval", "--corpus", str(corpus_path), "--record-baseline", name]
    )
    assert r.exit_code == 0, r.output
    path = baselines_dir / f"{name}.json"
    data = json.loads(path.read_text())
    data["config_signature"]["embedder"] = embedder
    for result in data["results"]:
        result["ndcg_at_5"] = round(result["ndcg_at_5"] + bump, 4)
    path.write_text(json.dumps(data), encoding="utf-8")


def _combined(result) -> str:
    return result.output + (result.stderr if result.stderr else "")


def test_config_change_with_regression_still_exits_3_and_names_the_keys(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Exit code unchanged (3); the output now says the config moved too."""
    corpus_path, baselines_dir = _prepare(monkeypatch, test_db, fake_embedder, tmp_path)
    _record_with_config(
        corpus_path, baselines_dir, name="cfgreg", embedder="some-other-backend",
        bump=0.0002,
    )
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "cfgreg", "--diff",
         "--fail-below"],
    )
    assert result.exit_code == 3, result.output
    out = _combined(result)
    assert "retrieval config changed" in out
    assert "embedder" in out
    assert "some-other-backend" in out
    assert "re-record the baseline" in out


def test_config_change_without_regression_exits_0_but_still_warns(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Reporting, not policy: a config change alone never changes the exit code."""
    corpus_path, baselines_dir = _prepare(monkeypatch, test_db, fake_embedder, tmp_path)
    _record_with_config(
        corpus_path, baselines_dir, name="cfgclean", embedder="some-other-backend",
        bump=0.0,
    )
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "cfgclean", "--diff",
         "--fail-below"],
    )
    assert result.exit_code == 0, result.output
    out = _combined(result)
    assert "retrieval config changed" in out
    # The exit-3 attribution line must NOT appear when nothing regressed.
    assert "re-record the baseline" not in out


def test_no_config_change_emits_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """A pure quality regression must not be muddied by a spurious config notice."""
    corpus_path, baselines_dir = _prepare(monkeypatch, test_db, fake_embedder, tmp_path)
    _record_and_bump(corpus_path, baselines_dir, bump=0.0002, name="puredrop")
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "puredrop", "--diff",
         "--fail-below"],
    )
    assert result.exit_code == 3, result.output
    assert "retrieval config changed" not in _combined(result)


def test_exit_3_points_at_the_baseline_readme(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Every exit 3 must name where to look.

    The commonest cause of an exit 3 is a baseline recorded against a different
    document set, not a worse ranker — and the exit code alone cannot say so.
    Fires on ANY regression, config change or not.
    """
    corpus_path, baselines_dir = _prepare(monkeypatch, test_db, fake_embedder, tmp_path)
    _record_and_bump(corpus_path, baselines_dir, bump=0.0002, name="ptr")
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "ptr", "--diff",
         "--fail-below"],
    )
    assert result.exit_code == 3, result.output
    assert "tests/eval/baselines/README.md" in _combined(result)


def test_baseline_readme_exists_and_is_tracked() -> None:
    """The pointer above must not dangle."""
    readme = Path(__file__).resolve().parents[1] / "tests" / "eval" / "baselines" / "README.md"
    assert readme.is_file(), f"{readme} is referenced by `brain eval`'s exit-3 output"
    body = readme.read_text(encoding="utf-8")
    # The three caveats a spurious exit 3 is usually caused by.
    assert "keyed to one machine" in body
    assert "clock-dependent" in body
    assert "retrieval config moved" in body
