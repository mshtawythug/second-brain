"""Tests for the `brain eval` CLI command."""

import json
import os
from pathlib import Path

import psycopg
import pytest
import yaml
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)

# A minimal two-query corpus YAML used by most tests.
_MINI_CORPUS = {
    "version": 1,
    "queries": [
        {
            "query": "evalcli alpha content",
            "category": "semantic",
            "expected_doc_ids": ["00000000-0000-0000-0000-000000000000"],
            "notes": "placeholder",
        },
        {
            "query": "evalcli beta content",
            "category": "people",
            "expected_doc_ids": ["00000000-0000-0000-0000-000000000000"],
            "notes": "placeholder",
        },
    ],
}


def _write_corpus(tmp_path: Path, corpus: dict) -> Path:
    """Write a corpus dict to a YAML file and return its path."""
    p = tmp_path / "test_corpus.yaml"
    p.write_text(yaml.dump(corpus), encoding="utf-8")
    return p


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    *,
    n_docs: int = 2,
) -> None:
    """Wire test DB + fake embedder and seed n_docs documents."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    for i in range(n_docs):
        ingest_document(
            test_db,
            embedder=fake_embedder,  # type: ignore[arg-type]
            doc=ExtractedDoc(
                title=f"EvalCLI Doc {i}",
                content=f"evalcli {'alpha' if i % 2 == 0 else 'beta'} content document {i}",
                content_type="txt",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
        )


# ---------------------------------------------------------------------------
# Happy path — table output
# ---------------------------------------------------------------------------


def test_brain_eval_runs_and_prints_table(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """brain eval runs successfully and prints a Rich table with metric columns."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    result = CliRunner().invoke(app, ["eval", "--corpus", str(corpus_path)])
    assert result.exit_code == 0, result.output
    # Metric headers must be visible in the table.
    assert "nDCG" in result.output
    assert "MRR" in result.output
    assert "Recall" in result.output


def test_brain_eval_category_filter_reduces_rows(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """--category people filters out non-people queries."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    result = CliRunner().invoke(
        app, ["eval", "--corpus", str(corpus_path), "--category", "people", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # With --category people we should only see the one 'people' query.
    for r in payload["results"]:
        assert r["category"] == "people"


def test_brain_eval_limit_caps_results(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """--limit 1 returns exactly one query result."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    result = CliRunner().invoke(
        app, ["eval", "--corpus", str(corpus_path), "--limit", "1", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["results"]) == 1


def test_brain_eval_json_output_is_valid(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """brain eval --json emits valid JSON matching the EvalReport shape."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    result = CliRunner().invoke(app, ["eval", "--corpus", str(corpus_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "results" in payload
    assert "mean_ndcg_at_5" in payload
    assert "mean_mrr" in payload
    assert "mean_recall_at_20" in payload
    assert "per_category" in payload
    assert "config_signature" in payload
    assert "generated_at" in payload
    assert isinstance(payload["results"], list)


def test_brain_eval_record_baseline_writes_file(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """--record-baseline NAME creates <baselines_dir>/<NAME>.json."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    baselines_dir = tmp_path / "baselines"
    monkeypatch.setattr("brain.cli._BASELINES_DIR", baselines_dir)

    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--record-baseline", "test-run"],
    )
    assert result.exit_code == 0, result.output
    baseline_file = baselines_dir / "test-run.json"
    assert baseline_file.exists(), f"baseline file not created: {baseline_file}"
    data = json.loads(baseline_file.read_text())
    assert "results" in data


def test_brain_eval_diff_against_baseline(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Record a baseline then diff; with identical config the deltas are 0."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    baselines_dir = tmp_path / "baselines"
    monkeypatch.setattr("brain.cli._BASELINES_DIR", baselines_dir)

    # Record baseline.
    r1 = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--record-baseline", "base"],
    )
    assert r1.exit_code == 0, r1.output

    # Diff against it.
    r2 = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "base", "--diff", "--json"],
    )
    assert r2.exit_code == 0, r2.output
    diff = json.loads(r2.output)
    assert "per_query" in diff
    assert "mean_ndcg_at_5_delta" in diff
    # Immediately after recording, deltas should be 0.0.
    assert diff["mean_ndcg_at_5_delta"] == pytest.approx(0.0, abs=1e-9)


def test_brain_eval_baseline_name_validator(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """--baseline ../etc/passwd exits 2 (BadParameter) due to invalid name."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--baseline", "../etc/passwd", "--diff"],
    )
    assert result.exit_code == 2, result.output


def test_brain_eval_diff_without_baseline_fails(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """--diff without --baseline exits 2 (BadParameter)."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    result = CliRunner().invoke(
        app, ["eval", "--corpus", str(corpus_path), "--diff"]
    )
    assert result.exit_code == 2, result.output


def test_brain_eval_record_and_diff_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Combining --record-baseline and --diff exits 2 (BadParameter)."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "--corpus", str(corpus_path),
            "--record-baseline", "mybase",
            "--baseline", "mybase",
            "--diff",
        ],
    )
    assert result.exit_code == 2, result.output
