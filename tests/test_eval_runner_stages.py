"""``brain eval`` applies ranking stages itself, and never falls back (RM-14a, spec §2.7).

``run_eval(stages=...)`` runs its own ``hybrid_search``, then
``search_pipeline.apply_stages``, and scores the reordered rows. Each stage's
``signature()`` joins ``config_signature``; each row records ``stage_ms``.
With no stage, the signature is exactly the four keys it always had, which is
what keeps the committed ``ci.json`` valid while the reranker ships off.

Every ``run_eval`` call here passes ``stages`` or ``env`` explicitly, so a
``BRAIN_RERANKER`` exported in the developer's shell cannot change a result.
Real Postgres (the 5434 test database); all data is synthetic.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import unittest.mock
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import rerank_backends
from brain.embeddings import OllamaEmbedError
from brain.eval.baseline import load_baseline, save_baseline
from brain.eval.corpus import EvalQuery
from brain.eval.runner import run_eval
from brain.ingest import ExtractedDoc, ingest_document
from brain.rerank import RerankStage
from tests.rerank_fakes import FakeReranker, RaisingReranker, title_scorer

TERM = "brindlewort"
#: The four keys ``config_signature`` had before any stage existed.
BASE_SIGNATURE = {
    "recency_halflife_days": None,
    "snippet_context_tokens": 0,
    "vector_sim_floor": 0.0,
    "embedder": "unknown",
}


def _seed(conn: psycopg.Connection[Any], embedder: Any, n: int) -> str:
    title = f"Brindle note {n}"
    ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=f"The {TERM} ledger lists item {n} twice: {TERM} {TERM}.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=f"manual:brindle-{n}",
    )
    row = conn.execute("SELECT id::text FROM documents WHERE title = %s", (title,)).fetchone()
    assert row is not None
    return str(row[0])


@pytest.fixture
def three(test_db: psycopg.Connection[Any], fake_embedder: Any) -> dict[str, str]:
    """Three matching notes, by title → id."""
    return {f"Brindle note {n}": _seed(test_db, fake_embedder, n) for n in range(3)}


def _query(expected: str) -> EvalQuery:
    return EvalQuery(query=TERM, expected_doc_ids=[expected], category="keyword")


def _stage(reranker: Any, depth: int = 20) -> RerankStage:
    return RerankStage(reranker=reranker, depth=depth, max_chars=1500, timeout=1.0)


def test_no_stage_leaves_the_signature_and_the_rows_as_they_were(
    test_db: psycopg.Connection[Any], fake_embedder: Any, three: dict[str, str]
) -> None:
    report = run_eval(
        test_db, embedder=fake_embedder, queries=[_query(three["Brindle note 0"])], stages=()
    )

    assert report.config_signature == BASE_SIGNATURE
    assert list(report.config_signature) == list(BASE_SIGNATURE)
    assert report.results[0].stage_ms == {}


def test_a_stage_reorders_what_is_scored_and_records_its_time(
    test_db: psycopg.Connection[Any], fake_embedder: Any, three: dict[str, str]
) -> None:
    # The expected document is put first by the reranker, whatever hybrid did.
    target = "Brindle note 2"
    reranker = FakeReranker(title_scorer([target]), name="fake", model="fake-model")

    report = run_eval(
        test_db,
        embedder=fake_embedder,
        queries=[_query(three[target])],
        stages=[_stage(reranker, depth=7)],
    )

    (row,) = report.results
    assert row.actual_doc_ids[0] == three[target]
    assert row.mrr == 1.0
    assert set(row.stage_ms) == {"rerank"}
    assert row.stage_ms["rerank"] >= 0.0
    assert report.config_signature == {
        **BASE_SIGNATURE,
        "reranker": "fake",
        "reranker_model": "fake-model",
        "rerank_depth": 7,
    }


def test_the_fetch_is_deep_enough_for_the_stage_and_truncated_to_the_limit(
    test_db: psycopg.Connection[Any], fake_embedder: Any, three: dict[str, str]
) -> None:
    reranker = FakeReranker(lambda _p: 0.0)

    report = run_eval(
        test_db,
        embedder=fake_embedder,
        queries=[_query(three["Brindle note 0"])],
        limit_per_query=2,
        stages=[_stage(reranker, depth=20)],
    )

    # The stage saw all three matches (it asked for 20), the report keeps two.
    assert len(reranker.passages_sent) == 3
    assert len(report.results[0].actual_doc_ids) == 2


def test_a_skipped_stage_scores_hybrid_order_and_is_logged_class_only(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    three: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    baseline = run_eval(
        test_db, embedder=fake_embedder, queries=[_query(three["Brindle note 0"])], stages=()
    )

    with caplog.at_level("WARNING", logger="brain.eval.runner"):
        report = run_eval(
            test_db,
            embedder=fake_embedder,
            queries=[_query(three["Brindle note 0"])],
            stages=[_stage(RaisingReranker())],
        )

    assert report.results[0].actual_doc_ids == baseline.results[0].actual_doc_ids
    messages = [r.getMessage() for r in caplog.records]
    assert any("rerank" in m and "RerankError" in m for m in messages)
    assert not any(TERM in m for m in messages)


def test_a_transient_embed_failure_is_still_a_degraded_row(
    test_db: psycopg.Connection[Any], fake_embedder: Any, three: dict[str, str]
) -> None:
    reranker = FakeReranker(lambda _p: 1.0)
    timeout = OllamaEmbedError("synthetic", transient=True)

    # A query text no other test embeds: ``hybrid_search`` caches query vectors.
    query = EvalQuery(
        query=f"{TERM} degraded-row probe",
        expected_doc_ids=[three["Brindle note 0"]],
        category="keyword",
    )

    with unittest.mock.patch.object(fake_embedder, "embed", side_effect=timeout):
        report = run_eval(
            test_db, embedder=fake_embedder, queries=[query], stages=[_stage(reranker)]
        )

    (row,) = report.results
    assert row.degraded is True
    assert row.actual_doc_ids == []
    assert row.stage_ms == {}
    # Never fell back to text-only matching, so the stage had nothing to see.
    assert reranker.calls == []


def test_stages_none_builds_them_from_the_env_dict(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    three: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Any] = []

    def fake_make(settings: Any) -> FakeReranker:
        built.append(settings)
        return FakeReranker(lambda _p: 0.0, name=settings.backend, model=settings.model)

    monkeypatch.setattr(rerank_backends, "make_reranker", fake_make)
    # The process environment says nothing; only the dict does.
    monkeypatch.delenv("BRAIN_RERANKER", raising=False)

    report = run_eval(
        test_db,
        embedder=fake_embedder,
        queries=[_query(three["Brindle note 0"])],
        env={"BRAIN_RERANKER": "llamacpp", "BRAIN_RERANK_DEPTH": "9"},
    )

    assert len(built) == 1
    assert report.config_signature == {
        **BASE_SIGNATURE,
        "reranker": "llamacpp",
        "reranker_model": "bge-reranker-v2-m3",
        "rerank_depth": 9,
    }
    assert set(report.results[0].stage_ms) == {"rerank"}


def test_stages_none_and_env_none_read_the_process_environment(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    three: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§2.3's carve-out: ``eval_cmd`` passes neither, so ``os.environ`` is read."""
    monkeypatch.setattr(
        rerank_backends,
        "make_reranker",
        lambda settings: FakeReranker(lambda _p: 0.0, name="env", model="m"),
    )
    monkeypatch.setenv("BRAIN_RERANKER", "llamacpp")

    # ``env=None`` spelled out (the default) so the guard below can tell this
    # deliberate read of the process environment from a forgotten one.
    report = run_eval(
        test_db, embedder=fake_embedder, queries=[_query(three["Brindle note 0"])], env=None
    )

    assert report.config_signature["reranker"] == "env"


def test_an_exported_reranker_does_not_reach_a_run_with_explicit_stages(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    three: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_RERANKER", "llamacpp")

    report = run_eval(
        test_db, embedder=fake_embedder, queries=[_query(three["Brindle note 0"])], stages=()
    )

    assert report.config_signature == BASE_SIGNATURE


def test_stage_ms_is_not_written_into_a_baseline(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    three: dict[str, str],
    tmp_path: Path,
) -> None:
    report = run_eval(
        test_db,
        embedder=fake_embedder,
        queries=[_query(three["Brindle note 0"])],
        stages=[_stage(FakeReranker(lambda _p: 0.0))],
    )
    path = tmp_path / "synthetic.json"

    save_baseline(report, path=path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert all("stage_ms" not in row for row in raw["results"])
    assert load_baseline(path).results[0].stage_ms == {}
    # ``brain eval --json`` emits ``dataclasses.asdict(report)``: it does carry it.
    assert "stage_ms" in dataclasses.asdict(report)["results"][0]


def _run_eval_calls_without_stages(source: str) -> list[int]:
    """Line numbers of ``run_eval(...)`` calls that pass neither ``stages`` nor ``env``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "run_eval":
            continue
        if not {kw.arg for kw in node.keywords} & {"stages", "env"}:
            lines.append(node.lineno)
    return lines


def test_every_test_of_run_eval_passes_stages_or_env() -> None:
    """Spec §2.3: an exported ``BRAIN_RERANKER`` must not change any test's result."""
    tests_dir = Path(__file__).parent
    offenders = {
        path.name: lines
        for path in sorted(tests_dir.rglob("*.py"))
        if (lines := _run_eval_calls_without_stages(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_the_guard_finds_a_bare_call() -> None:
    source = "run_eval(conn, embedder=e, queries=q)\nrun_eval(conn, stages=())\n"
    assert _run_eval_calls_without_stages(source) == [1]
