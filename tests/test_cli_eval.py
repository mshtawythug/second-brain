"""Tests for the `brain eval` CLI command."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
import yaml
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
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


def test_brain_eval_unknown_category_raises_bad_parameter(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """--category with an unknown value exits 2 (BadParameter) instead of silently
    returning an empty report."""
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    result = CliRunner().invoke(
        app, ["eval", "--corpus", str(corpus_path), "--category", "bogus_cat"]
    )
    assert result.exit_code == 2, result.output
    assert "bogus_cat" in result.output


def test_brain_eval_json_record_baseline_no_stdout_pollution(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """--json --record-baseline must not emit 'baseline saved:' to stdout,
    which would corrupt the JSON stream."""
    baselines_dir = tmp_path / "baselines"
    baselines_dir.mkdir()
    monkeypatch.setattr("brain.cli._BASELINES_DIR", baselines_dir)
    _setup(monkeypatch, test_db, fake_embedder)
    corpus_path = _write_corpus(tmp_path, _MINI_CORPUS)
    result = CliRunner().invoke(
        app,
        ["eval", "--corpus", str(corpus_path), "--json", "--record-baseline", "noisy"],
    )
    assert result.exit_code == 0, result.output
    # result.stdout is stdout-only (Typer CliRunner separates streams internally).
    # With err=True on the typer.echo call, the "baseline saved:" notice goes to
    # stderr; stdout must be pure JSON.
    data = json.loads(result.stdout)
    assert "results" in data
    assert (baselines_dir / "noisy.json").exists()


# ---------------------------------------------------------------------------
# Degraded runs (an embed call raised OllamaEmbedError)
#
# Every failing query string below is unique to its test: brain.search caches
# query embeddings per embedder class, and a string that once succeeded through
# FlakyEmbedder would be served from cache instead of raising.
# ---------------------------------------------------------------------------


def _degraded_corpus(tmp_path: Path, queries: list[str], expected_id: str) -> Path:
    return _write_corpus(
        tmp_path,
        {
            "version": 1,
            "queries": [
                {"query": q, "category": "semantic", "expected_doc_ids": [expected_id]}
                for q in queries
            ],
        },
    )


def _seed_one(test_db: psycopg.Connection, fake_embedder: object, title: str) -> str:
    ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title,
            content=f"{title} body text",
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    row = test_db.execute("SELECT id::text FROM documents WHERE title = %s", (title,)).fetchone()
    assert row is not None
    return str(row[0])


def _wire_flaky(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, embedder: object
) -> Path:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: embedder)
    baselines_dir = tmp_path / "baselines"
    monkeypatch.setattr("brain.cli._BASELINES_DIR", baselines_dir)
    return baselines_dir


def test_record_baseline_refuses_a_degraded_run(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    make_flaky_embedder: object,
    tmp_path: Path,
) -> None:
    """Rule-12 regression: one embed timeout used to exit 0 AND save that query as
    0.0, poisoning the baseline every later --fail-below compares against."""
    doc_id = _seed_one(test_db, fake_embedder, "Degraded Record Doc")
    failing = "degrecord timed out query"
    corpus = _degraded_corpus(tmp_path, ["Degraded Record Doc body", failing], doc_id)
    baselines_dir = _wire_flaky(monkeypatch, tmp_path, make_flaky_embedder({failing}))  # type: ignore[operator]

    result = CliRunner().invoke(app, ["eval", "--corpus", str(corpus), "--record-baseline", "ci"])

    assert result.exit_code == 1, result.output
    # A clean typer.Exit, not an escaped DegradedBaselineError: CliRunner maps an
    # uncaught exception to exit 1 too, and in production that is a traceback.
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert not (baselines_dir / "ci.json").exists()
    assert "1 of 2" in result.stderr
    assert "refusing to record baseline 'ci'" in result.stderr
    assert failing not in result.stderr.split("refusing", 1)[1]


def test_refused_record_leaves_the_existing_baseline_untouched(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    make_flaky_embedder: object,
    tmp_path: Path,
) -> None:
    doc_id = _seed_one(test_db, fake_embedder, "Degraded Keep Doc")
    failing = "degkeep timed out query"
    corpus = _degraded_corpus(tmp_path, ["Degraded Keep Doc body", failing], doc_id)
    baselines_dir = _wire_flaky(monkeypatch, tmp_path, make_flaky_embedder({failing}))  # type: ignore[operator]
    baselines_dir.mkdir()
    existing = baselines_dir / "ci.json"
    existing.write_text('{"sentinel": true}\n', encoding="utf-8")

    result = CliRunner().invoke(app, ["eval", "--corpus", str(corpus), "--record-baseline", "ci"])

    assert result.exit_code == 1, result.output
    # A clean typer.Exit, not an escaped DegradedBaselineError: CliRunner maps an
    # uncaught exception to exit 1 too, and in production that is a traceback.
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert existing.read_text(encoding="utf-8") == '{"sentinel": true}\n'


def test_plain_run_still_exits_0_but_warns_with_the_count(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    make_flaky_embedder: object,
    tmp_path: Path,
) -> None:
    """Ad-hoc runs keep the tolerance; they just stop being silent about it."""
    doc_id = _seed_one(test_db, fake_embedder, "Degraded Plain Doc")
    failing = "degplain timed out query"
    corpus = _degraded_corpus(tmp_path, ["Degraded Plain Doc body", failing], doc_id)
    _wire_flaky(monkeypatch, tmp_path, make_flaky_embedder({failing}))  # type: ignore[operator]

    result = CliRunner().invoke(app, ["eval", "--corpus", str(corpus), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [r["degraded"] for r in payload["results"]] == [False, True]
    assert "1 of 2" in result.stderr


def _record_clean(
    monkeypatch: pytest.MonkeyPatch, embedder: object, corpus: Path, name: str
) -> None:
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: embedder)
    recorded = CliRunner().invoke(
        app, ["eval", "--corpus", str(corpus), "--record-baseline", name]
    )
    assert recorded.exit_code == 0, recorded.output


def test_diff_excludes_degraded_rows_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    make_flaky_embedder: object,
    tmp_path: Path,
) -> None:
    doc_id = _seed_one(test_db, fake_embedder, "Degraded Diff Doc")
    failing = "degdiff sometimes times out"
    corpus = _degraded_corpus(tmp_path, ["Degraded Diff Doc body", failing], doc_id)
    _wire_flaky(monkeypatch, tmp_path, fake_embedder)
    _record_clean(monkeypatch, fake_embedder, corpus, "base")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: make_flaky_embedder({failing}))  # type: ignore[operator]

    result = CliRunner().invoke(
        app, ["eval", "--corpus", str(corpus), "--baseline", "base", "--diff", "--json"]
    )

    assert result.exit_code == 0, result.output
    diff = json.loads(result.stdout)
    assert [d["query"] for d in diff["per_query"]] == ["Degraded Diff Doc body"]
    assert diff["degraded_queries"] == [failing]
    assert "1 of 2" in result.stderr


def _write_high_baseline(baselines_dir: Path, queries: list[str], name: str) -> None:
    """A baseline every current run regresses against (all metrics 1.0)."""
    from brain.eval import save_baseline
    from brain.eval.runner import CategorySummary, EvalReport, EvalResult

    results = [
        EvalResult(
            query=q,
            category="semantic",
            expected_doc_ids=["00000000-0000-0000-0000-000000000000"],
            actual_doc_ids=["00000000-0000-0000-0000-000000000000"],
            ndcg_at_5=1.0,
            mrr=1.0,
            recall_at_20=1.0,
        )
        for q in queries
    ]
    report = EvalReport(
        results=results,
        mean_ndcg_at_5=1.0,
        mean_mrr=1.0,
        mean_recall_at_20=1.0,
        per_category={
            "semantic": CategorySummary(
                category="semantic",
                count=len(results),
                mean_ndcg_at_5=1.0,
                mean_mrr=1.0,
                mean_recall_at_20=1.0,
            )
        },
        config_signature={},
        generated_at=datetime(2026, 10, 15, tzinfo=UTC),
    )
    save_baseline(report, path=baselines_dir / f"{name}.json")


@pytest.mark.parametrize("fail_all", [False, True], ids=["partial", "fully-degraded"])
def test_fail_below_refuses_to_judge_a_degraded_run(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    make_flaky_embedder: object,
    tmp_path: Path,
    fail_all: bool,
) -> None:
    """Exit 1, not 3 and not 0. Partial: the healthy row DOES regress against the
    all-1.0 baseline, so a refusal placed after the regression check would exit
    3. Fully degraded: nothing is comparable, so the deltas are 0 and a gate
    that only excluded degraded rows would PASS a run that measured nothing."""
    tag = "all" if fail_all else "part"
    healthy = f"deggate {tag} healthy query"
    failing = f"deggate {tag} timed out query"
    corpus = _degraded_corpus(
        tmp_path, [healthy, failing], "00000000-0000-0000-0000-000000000000"
    )
    fail_set = {healthy, failing} if fail_all else {failing}
    baselines_dir = _wire_flaky(monkeypatch, tmp_path, make_flaky_embedder(fail_set))  # type: ignore[operator]
    _write_high_baseline(baselines_dir, [healthy, failing], "base")

    result = CliRunner().invoke(
        app,
        [
            "eval", "--corpus", str(corpus),
            "--baseline", "base", "--diff", "--fail-below", "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    diff = json.loads(result.stdout)  # the report is still emitted first
    assert len(diff["degraded_queries"]) == (2 if fail_all else 1)
    assert "cannot evaluate --fail-below" in result.stderr


def test_fail_below_judges_normally_when_only_an_added_query_is_degraded(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    make_flaky_embedder: object,
    tmp_path: Path,
) -> None:
    """A degraded ADDED query (absent from the baseline) is outside the
    comparison: the gate runs its normal regression logic (exit 0 here, nothing
    regressed) instead of refusing, and the run still warns with the count."""
    doc_id = _seed_one(test_db, fake_embedder, "Degraded Added Doc")
    shared = "Degraded Added Doc body"
    added = "degadded new query times out"
    _wire_flaky(monkeypatch, tmp_path, fake_embedder)
    base_dir = tmp_path / "base-corpus"
    base_dir.mkdir()
    _record_clean(
        monkeypatch, fake_embedder, _degraded_corpus(base_dir, [shared], doc_id), "base"
    )
    corpus = _degraded_corpus(tmp_path, [shared, added], doc_id)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: make_flaky_embedder({added}))  # type: ignore[operator]

    result = CliRunner().invoke(
        app,
        [
            "eval", "--corpus", str(corpus),
            "--baseline", "base", "--diff", "--fail-below", "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    diff = json.loads(result.stdout)
    assert diff["degraded_queries"] == []
    assert diff["added_queries"] == [added]
    assert "1 of 2" in result.stderr
    assert "cannot evaluate --fail-below" not in result.stderr
