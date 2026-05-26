"""End-to-end graph-retrieval eval (wave G2-j) — scored against synthetic golden.

Builds a small SYNTHETIC person+concept graph on the AGE test instance (port
5434, the default ``test_db``) via the real ``reconcile_document`` + ``AgeBackend``
(people pipeline + a deterministic fake concept extractor — no Ollama), then runs
``graph_rag_search`` for the local + themes cases in
``tests/eval/graph_retrieval_cases.py`` and scores them with the pure scorers in
:mod:`brain.eval.graph_retrieval`:

* **local** queries → ranked ``GraphContext.docs`` scored with the reused
  nDCG / MRR / Recall (:func:`~brain.eval.graph_retrieval.score_local_docs`);
* **themes-with-X** → ``GraphContext.themes`` keysets scored with the
  graph-appropriate Jaccard-matched theme-set F1
  (:func:`~brain.eval.graph_retrieval.score_themes`).

This is a normal ``test_db`` integration test (it needs live AGE but **no live
Ollama** — concepts come from a fake extractor), so it runs in the default suite
exactly like ``tests/test_graphrag_themes`` — it is NOT ``eval``-marked (the
``eval`` marker is reserved for the live-Ollama concept gate). All people /
topics / orgs are synthetic (no PII).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.eval.graph_baseline import (
    diff_graph_reports,
    load_graph_baseline,
    save_graph_baseline,
)
from brain.eval.graph_retrieval import score_local_docs, score_themes
from brain.eval.graph_runner import run_graph_eval
from brain.graph_rag import FUSE_MODE, LOCAL_MODE, THEMES_MODE, graph_rag_search
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.extract import ExtractedEntity
from brain.graph_rag.reconcile import ReconcileConfig, reconcile_document
from brain.vault.derived_links.directory import DirectoryStore
from tests.eval.graph_retrieval_cases import (
    CONCEPT_MARKERS,
    CORPUS_ROWS,
    DANA,
    LOCAL_CASES,
    MEAN_FUSE_RECALL_MIN,
    MEAN_LOCAL_RECALL_MIN,
    MEAN_THEMES_F1_MIN,
    OWNER,
    TENANT,
    THEMES_CASES,
)

# Suppression-disabled ratio (cap = round(N * 1.0) = N) — keeps the tiny corpus's
# concept entities eligible. Mirrors ``tests/test_graphrag_themes._NO_SUPPRESS``.
_NO_SUPPRESS = 1.0


def _make_cfg(**overrides: Any) -> Config:
    params: dict[str, Any] = {
        "database_url": Config.load().database_url,
        "graph_tenant_id": TENANT,
        "graph_depth": 2,
        "graph_frontier_cap": 200,
        "graph_min_edge_weight": 0.2,
        "graph_generic_df_ratio": _NO_SUPPRESS,
        "graph_theme_limit": 5,
    }
    params.update(overrides)
    return Config(**params)


class _FakeConceptExtractor:
    """Deterministic concept extractor keyed on a marker substring (no Ollama).

    Mirrors ``tests/test_graphrag_themes._FakeConceptExtractor``: emits each
    marker's concepts at adjacent word positions so they co-occur within the
    default window (forming a cluster).
    """

    def __init__(self, by_marker: dict[str, list[tuple[str, str, str]]]) -> None:
        self._by_marker = by_marker

    @property
    def version(self) -> str:
        return "fake-extractor@concepts-v1"

    def extract(self, text: str) -> list[ExtractedEntity]:
        out: list[ExtractedEntity] = []
        for marker, concepts in self._by_marker.items():
            if marker in text:
                for position, (etype, key, name) in enumerate(concepts):
                    out.append(
                        ExtractedEntity(
                            entity_type=etype,
                            canonical_key=key,
                            display_name=name,
                            positions=(position,),
                        )
                    )
        return out


def _seed_directory(
    conn: psycopg.Connection[Any], pairs: Sequence[tuple[str, str]]
) -> None:
    store = DirectoryStore(conn)
    for name, email in pairs:
        store.upsert_pair(display_name=name, email=email, source="gmail")


def _seed_gmail_doc(
    conn: psycopg.Connection[Any],
    *,
    external_id: str,
    participants: Sequence[tuple[str, str]],
    content: str,
) -> str:
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('gmail', %s, '{}'::jsonb) RETURNING id",
        (external_id,),
    ).fetchone()
    assert src_row is not None
    from_hdr = f"{participants[0][0]} <{participants[0][1]}>"
    to_hdr = ", ".join(f"{n} <{e}>" for n, e in participants[1:])
    metadata = {"from": from_hdr, "to": to_hdr, "thread_id": external_id}
    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        "INSERT INTO documents "
        "(source_id, title, content, content_hash, content_type, metadata) "
        "VALUES (%s, %s, %s, %s, 'email', %s::jsonb) RETURNING id::text",
        (src_row[0], external_id, salted, content_hash, json.dumps(metadata)),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _add_chunk(
    conn: psycopg.Connection[Any], embedder: Any, document_id: str, content: str
) -> None:
    vec = embedder.embed([content], input_type="document")[0]
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s, 0, %s, %s)",
        (document_id, content, vec),
    )


def _backend(conn: psycopg.Connection[Any]) -> AgeBackend:
    backend = AgeBackend()
    backend.bootstrap(conn)
    return backend


def _build_corpus(
    conn: psycopg.Connection[Any], backend: AgeBackend, embedder: Any
) -> dict[str, str]:
    """Reconcile the synthetic corpus; return external_id -> document_id."""
    _seed_directory(conn, [DANA, OWNER])
    extractor = _FakeConceptExtractor(CONCEPT_MARKERS)
    # owner_keys empty so the owner is a real co-mentioned person entity; the
    # themes search excludes it via cfg.owner_participants instead.
    rcfg = ReconcileConfig(
        tenant_id=TENANT,
        generic_df_ratio=_NO_SUPPRESS,
        concepts_enabled=True,
        owner_keys=frozenset(),
    )
    docs: dict[str, str] = {}
    for row in CORPUS_ROWS:
        doc = _seed_gmail_doc(
            conn,
            external_id=row.external_id,
            participants=[DANA, OWNER],
            content=row.body,
        )
        _add_chunk(conn, embedder, doc, row.body)
        reconcile_document(conn, doc, backend=backend, config=rcfg, extractor=extractor)
        docs[row.external_id] = doc
    return docs


def test_local_retrieval_eval_meets_thresholds(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Local graph retrieval scores at/above the synthetic golden thresholds."""
    backend = _backend(test_db)
    docs = _build_corpus(test_db, backend, fake_embedder)
    cfg = _make_cfg()

    recalls: list[float] = []
    for case in LOCAL_CASES:
        ctx = graph_rag_search(
            test_db, cfg, case.query, backend=backend, mode=LOCAL_MODE
        )
        actual = [doc.document_id for doc in ctx.docs]
        expected = [docs[ext] for ext in case.expected_doc_external_ids]
        score = score_local_docs(actual, expected)
        assert score.recall_at_k >= case.min_recall, (
            f"local query {case.query!r}: recall={score.recall_at_k:.4f} "
            f"< {case.min_recall} (actual={actual}, expected={expected})"
        )
        assert score.ndcg_at_k >= case.min_ndcg, (
            f"local query {case.query!r}: ndcg={score.ndcg_at_k:.4f} < {case.min_ndcg}"
        )
        recalls.append(score.recall_at_k)

    mean_recall = sum(recalls) / len(recalls)
    assert mean_recall >= MEAN_LOCAL_RECALL_MIN, (
        f"mean local recall {mean_recall:.4f} < {MEAN_LOCAL_RECALL_MIN}"
    )


def test_themes_retrieval_eval_meets_thresholds(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Themes-with-X retrieval surfaces the expected clusters (Jaccard F1)."""
    backend = _backend(test_db)
    _build_corpus(test_db, backend, fake_embedder)
    cfg = _make_cfg(owner_participants=frozenset({OWNER[0]}))

    f1s: list[float] = []
    for case in THEMES_CASES:
        ctx = graph_rag_search(
            test_db,
            cfg,
            "",
            backend=backend,
            mode=THEMES_MODE,
            person=case.person,
        )
        actual = [
            {entity.canonical_key for entity in theme.entities}
            for theme in ctx.themes
        ]
        score = score_themes(actual, [set(ks) for ks in case.expected_theme_keysets])
        assert score.f1 >= case.min_f1, (
            f"themes person={case.person!r}: f1={score.f1:.4f} < {case.min_f1} "
            f"(actual={actual}, expected={[set(k) for k in case.expected_theme_keysets]})"
        )
        f1s.append(score.f1)

    mean_f1 = sum(f1s) / len(f1s)
    assert mean_f1 >= MEAN_THEMES_F1_MIN, (
        f"mean themes F1 {mean_f1:.4f} < {MEAN_THEMES_F1_MIN}"
    )


# --------------------------------------------------------------------------- #
# Parallel graph-eval runner (wave G4-d; spec §17d Q3)
# --------------------------------------------------------------------------- #
def _run_graph_eval(
    conn: psycopg.Connection[Any], backend: AgeBackend, embedder: Any, docs: dict[str, str]
) -> Any:
    """Drive the parallel graph-eval runner over the synthetic corpus.

    One cfg serves all modes: ``owner_participants`` is set for the themes owner
    exclusion; local/fuse ignore it. ``embedder`` (the pre-warmed instance —
    perf-T4 G5) feeds the fuse hybrid leg's vector arm.
    """
    cfg = _make_cfg(owner_participants=frozenset({OWNER[0]}))
    return run_graph_eval(
        conn,
        cfg,
        backend=backend,
        local_cases=LOCAL_CASES,
        themes_cases=THEMES_CASES,
        external_id_to_doc_id=docs,
        embedder=embedder,
        include_fuse=True,
        backend_name="age-test",
    )


def test_run_graph_eval_meets_thresholds(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The parallel graph-eval runner scores at/above the synthetic thresholds.

    BLOCKING thresholds live HERE (spec §17d Q3 — not a committed ``ci.json`` +
    ``--fail-below``): mean local recall + nDCG, mean fuse recall (recall-only —
    the fuse hybrid leg may interleave non-seed docs), mean themes F1.
    """
    backend = _backend(test_db)
    docs = _build_corpus(test_db, backend, fake_embedder)
    report = _run_graph_eval(test_db, backend, fake_embedder, docs)

    assert report.mean_local_recall_at_k >= MEAN_LOCAL_RECALL_MIN, (
        f"mean local recall {report.mean_local_recall_at_k:.4f} < {MEAN_LOCAL_RECALL_MIN}"
    )
    assert report.mean_local_ndcg_at_k >= 0.99, (
        f"mean local ndcg {report.mean_local_ndcg_at_k:.4f} < 0.99"
    )
    assert report.mean_fuse_recall_at_k >= MEAN_FUSE_RECALL_MIN, (
        f"mean fuse recall {report.mean_fuse_recall_at_k:.4f} < {MEAN_FUSE_RECALL_MIN}"
    )
    assert report.mean_themes_f1 >= MEAN_THEMES_F1_MIN, (
        f"mean themes F1 {report.mean_themes_f1:.4f} < {MEAN_THEMES_F1_MIN}"
    )

    # Shape: one local + one fuse doc-result per LOCAL_CASES query; one themes
    # result per THEMES_CASES person.
    assert len(report.doc_results) == 2 * len(LOCAL_CASES)
    assert len(report.themes_results) == len(THEMES_CASES)
    assert {r.mode for r in report.doc_results} == {LOCAL_MODE, FUSE_MODE}


def test_run_graph_eval_baseline_round_trips(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """A recorded graph baseline reloads identically + self-diffs to zero (canary)."""
    backend = _backend(test_db)
    docs = _build_corpus(test_db, backend, fake_embedder)
    report = _run_graph_eval(test_db, backend, fake_embedder, docs)

    path = tmp_path / "graph-canary.json"
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
    assert len(loaded.doc_results) == len(report.doc_results)
    assert len(loaded.themes_results) == len(report.themes_results)

    diff = diff_graph_reports(loaded, report)
    assert diff.config_signature_changed is False
    assert diff.mean_local_recall_at_k_delta == pytest.approx(0.0, abs=1e-4)
    assert diff.mean_fuse_recall_at_k_delta == pytest.approx(0.0, abs=1e-4)
    assert diff.mean_themes_f1_delta == pytest.approx(0.0, abs=1e-4)
    assert len(diff.per_doc) == len(report.doc_results)
    assert len(diff.per_themes) == len(report.themes_results)
