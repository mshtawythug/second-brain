"""A stage that SKIPS during ``brain eval`` makes its row unusable as a measurement (RM-14a).

A skipped reranker leaves that query in hybrid order while ``config_signature``
names the reranker, so the row measured a different pipeline from the one the
report claims: the PR #33 failure mode, by another route. Such a row is marked
(``EvalResult.stages_skipped``) and refused exactly like a degraded one:
``save_baseline`` will not write it, and ``diff_reports`` keeps it out of the
deltas (``degraded_queries``), which is what makes ``--fail-below`` refuse to
judge. All of it lives in ``brain.eval``; ``cli.py`` is unchanged.

Real Postgres (the 5434 test database). All data is synthetic.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from typer.testing import CliRunner

from brain import rerank_backends
from brain.cli import app
from brain.eval.baseline import (
    degraded_run_message,
    diff_reports,
    save_baseline,
    stage_skipped_run_message,
)
from brain.eval.corpus import EvalQuery
from brain.eval.errors import DegradedBaselineError, StageSkippedBaselineError
from brain.eval.runner import run_eval, skipped_stage_results
from brain.ingest import ExtractedDoc, ingest_document
from brain.rerank import RerankStage
from tests.rerank_fakes import FakeReranker, RaisingReranker

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://brain:brain@localhost:5434/second_brain_test"
)
TERM = "fernquill"


def _seed(conn: psycopg.Connection[Any], embedder: Any, n: int) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=f"Fernquill note {n}",
            content=f"The {TERM} register, entry {n}.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=f"manual:fernquill-{n}",
    )
    return str(result.document_id)


@pytest.fixture
def doc_ids(test_db: psycopg.Connection[Any], fake_embedder: Any) -> list[str]:
    return [_seed(test_db, fake_embedder, n) for n in range(3)]


def _query(expected: str, text: str = TERM) -> EvalQuery:
    return EvalQuery(query=text, expected_doc_ids=[expected], category="semantic")


def _stage(reranker: Any) -> RerankStage:
    return RerankStage(reranker=reranker, depth=20, max_chars=1500, timeout=1.0)


def _run(conn: Any, embedder: Any, ids: list[str], reranker: Any) -> Any:
    return run_eval(
        conn,
        embedder=embedder,
        queries=[_query(ids[0]), _query(ids[1], f"{TERM} entry")],
        stages=[_stage(reranker)],
    )


# --- save_baseline -------------------------------------------------------------------


def test_save_baseline_refuses_a_run_with_a_skipped_stage(
    test_db: psycopg.Connection[Any], fake_embedder: Any, doc_ids: list[str], tmp_path: Path
) -> None:
    report = _run(test_db, fake_embedder, doc_ids, RaisingReranker())
    path = tmp_path / "base.json"
    path.write_text("untouched\n", encoding="utf-8")

    with pytest.raises(StageSkippedBaselineError) as info:
        save_baseline(report, path=path)

    assert isinstance(info.value, DegradedBaselineError)  # the CLI's existing mapping
    message = str(info.value)
    assert "2 of 2" in message
    assert "ranking stage" in message
    assert "embed call" not in message
    assert TERM not in message
    assert path.read_text(encoding="utf-8") == "untouched\n"


def test_save_baseline_keeps_stages_skipped_out_of_the_file(
    test_db: psycopg.Connection[Any], fake_embedder: Any, doc_ids: list[str], tmp_path: Path
) -> None:
    report = _run(test_db, fake_embedder, doc_ids, FakeReranker(lambda _p: 0.0))
    path = tmp_path / "base.json"

    save_baseline(report, path=path)

    rows = json.loads(path.read_text(encoding="utf-8"))["results"]
    assert all("stages_skipped" not in row and "stage_ms" not in row for row in rows)
    assert skipped_stage_results(report) == []


def test_eval_json_carries_stages_skipped(
    test_db: psycopg.Connection[Any], fake_embedder: Any, doc_ids: list[str]
) -> None:
    report = _run(test_db, fake_embedder, doc_ids, RaisingReranker())

    rows = json.loads(json.dumps(dataclasses.asdict(report), default=str))["results"]

    assert [row["stages_skipped"] for row in rows] == [["rerank"], ["rerank"]]
    assert skipped_stage_results(report) == report.results


# --- diff_reports / --fail-below ----------------------------------------------------


def test_a_skipped_row_is_kept_out_of_the_deltas(
    test_db: psycopg.Connection[Any], fake_embedder: Any, doc_ids: list[str]
) -> None:
    baseline = _run(test_db, fake_embedder, doc_ids, FakeReranker(lambda _p: 0.0))
    current = _run(test_db, fake_embedder, doc_ids, RaisingReranker())

    diff = diff_reports(baseline, current)

    assert diff.per_query == []
    assert diff.degraded_queries == [TERM, f"{TERM} entry"]


# --- messages -------------------------------------------------------------------------


def test_the_stage_skip_message_names_the_count_and_not_the_embedder() -> None:
    message = stage_skipped_run_message(1, 31, action="refusing to record baseline 'ci'")
    assert message.startswith("refusing to record baseline 'ci'")
    assert "1 of 31" in message
    assert "ranking stage" in message
    assert "embed call" not in message


def test_the_generic_message_covers_both_causes() -> None:
    """The CLI passes only counts, so the line it prints must be true for either cause."""
    message = degraded_run_message(2, 31, action="cannot evaluate --fail-below")
    assert "2 of 31" in message
    assert "embed call" in message
    assert "ranking stage" in message
    assert "retry" in message


# --- end to end through `brain eval` (cli.py unchanged) ------------------------------


@pytest.fixture
def cli_eval(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: Any,
    doc_ids: list[str],
    tmp_path: Path,
) -> Path:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._BASELINES_DIR", tmp_path / "baselines")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    corpus = {
        "version": 1,
        "queries": [
            {"query": TERM, "category": "semantic", "expected_doc_ids": [doc_ids[0]]},
            {"query": f"{TERM} entry", "category": "semantic", "expected_doc_ids": [doc_ids[1]]},
        ],
    }
    path = tmp_path / "corpus.yaml"
    path.write_text(yaml.dump(corpus), encoding="utf-8")
    return path


def _skipping_reranker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_RERANKER", "llamacpp")
    monkeypatch.setattr(rerank_backends, "make_reranker", lambda _settings: RaisingReranker())


def test_record_baseline_refuses_a_skipping_reranker(
    cli_eval: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _skipping_reranker(monkeypatch)

    result = CliRunner().invoke(
        app, ["eval", "--corpus", str(cli_eval), "--record-baseline", "skipped"]
    )

    assert result.exit_code == 1, result.output
    assert "ranking stage" in result.stderr
    assert not (tmp_path / "baselines" / "skipped.json").exists()


def test_fail_below_refuses_a_skipping_reranker(
    cli_eval: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = CliRunner().invoke(
        app, ["eval", "--corpus", str(cli_eval), "--record-baseline", "base"]
    )
    assert recorded.exit_code == 0, recorded.output
    _skipping_reranker(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(cli_eval), "--baseline", "base", "--diff", "--fail-below"],
    )

    # Exit 1 ("could not judge"), not 0 (passed) and not 3 (regressed).
    assert result.exit_code == 1, result.output
    assert "cannot evaluate --fail-below" in result.stderr
    assert "ranking stage" in result.stderr
