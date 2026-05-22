"""Parallel graph-retrieval eval runner (wave G4-d; spec §17d Q3).

A SEPARATE runner + report from the hybrid :func:`brain.eval.runner.run_eval`:
the graph paths report two different metric shapes — local/fuse return a *ranked
document list* (nDCG@k / MRR / Recall@k) while themes-with-X returns *entity
clusters* (set precision / recall / F1) — and neither set fits
:class:`~brain.eval.runner.EvalReport`'s nDCG@5/MRR/Recall@20-only model. So G4
adds this parallel runner + :class:`GraphEvalReport` + its own baseline path
(:mod:`brain.eval.graph_baseline`) rather than a new ``_VALID_CATEGORIES`` entry
on the hybrid runner (spec §17d Q3).

Golden corpus = the existing G2-j synthetic fixture
(``tests/eval/graph_retrieval_cases.py``) — *reused, not recommitted*. The cases
are **injected** (this module lives in ``src`` and must never import from
``tests``): the synthetic-graph integration test builds the graph on the AGE test
DB, then passes its ``LOCAL_CASES`` / ``THEMES_CASES`` + the
external-id→document-id mapping in. Local-/fuse-doc scoring reuses
:func:`brain.eval.graph_retrieval.score_local_docs`; themes scoring reuses
:func:`~brain.eval.graph_retrieval.score_themes` — no new metric is invented.

No CLI surface and no committed baseline (spec §17d Q3): the ``brain eval`` CLI
runs the hybrid golden corpus against a live brain, but the graph cases are
synthetic-corpus-specific (they expect a graph the CLI cannot build on prod), so
a ``brain eval --graph`` flag does not fit and is intentionally omitted (YAGNI).
The blocking thresholds live in the synthetic-graph integration test + the
``-m benchmark`` gate, NOT a committed ``ci.json`` + ``--fail-below`` (that
flag+baseline precedent belongs to a separate roadmap and does not exist here).
The record/diff baseline (:mod:`brain.eval.graph_baseline`) is a **canary** that
round-trips in tests.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import psycopg

from ..graph_rag import FUSE_MODE, LOCAL_MODE, THEMES_MODE, graph_rag_search
from .graph_retrieval import score_local_docs, score_themes

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..config import Config
    from ..graph_rag.backends.base import GraphBackend
    from ..ingest import Embedder


class LocalCaseLike(Protocol):
    """Structural shape of a local/fuse graph-eval case (G2-j ``GraphLocalCase``).

    Read-only properties so any frozen dataclass with these attributes (the
    committed ``tests/eval/graph_retrieval_cases.GraphLocalCase``) satisfies it
    without this ``src`` module importing the ``tests`` fixture.
    """

    @property
    def query(self) -> str: ...

    @property
    def expected_doc_external_ids(self) -> tuple[str, ...]: ...


class ThemesCaseLike(Protocol):
    """Structural shape of a themes graph-eval case (G2-j ``GraphThemesCase``)."""

    @property
    def person(self) -> str: ...

    @property
    def expected_theme_keysets(self) -> tuple[frozenset[str], ...]: ...


@dataclass(frozen=True)
class GraphDocEvalResult:
    """Ranked-doc metrics for one local- or fuse-mode graph query.

    ``mode`` is :data:`~brain.graph_rag.LOCAL_MODE` or
    :data:`~brain.graph_rag.FUSE_MODE` — both return ``GraphContext.docs`` (a
    ranked :class:`~brain.search.SearchResult` list), so both are scored with the
    reused ranking metrics via :func:`brain.eval.graph_retrieval.score_local_docs`.
    """

    mode: str
    query: str
    expected_doc_ids: list[str]
    actual_doc_ids: list[str]
    ndcg_at_k: float
    mrr: float
    recall_at_k: float
    ndcg_k: int
    recall_k: int


@dataclass(frozen=True)
class GraphThemesEvalResult:
    """Theme-set precision / recall / F1 for one themes-with-X graph query.

    Scored by greedy best-Jaccard cluster matching via
    :func:`brain.eval.graph_retrieval.score_themes`. Keysets are stored as sorted
    ``list[list[str]]`` (not ``set``/``frozenset``) so the report is
    JSON-serializable + byte-stable for baseline diffs.
    """

    person: str
    expected_theme_keysets: list[list[str]]
    actual_theme_keysets: list[list[str]]
    precision: float
    recall: float
    f1: float
    matched: int
    n_expected: int
    n_actual: int


@dataclass(frozen=True)
class GraphEvalReport:
    """Full graph-eval run: per-case results + per-mode aggregate means.

    ``doc_results`` holds the local + (optional) fuse ranked-doc results;
    ``themes_results`` the themes cluster-set results. The aggregate means are
    split by mode (local vs fuse vs themes) because the metric families differ.
    ``config_signature`` captures the caps + flags so a baseline diff can flag a
    config change (mirroring :class:`brain.eval.runner.EvalReport`).
    """

    doc_results: list[GraphDocEvalResult]
    themes_results: list[GraphThemesEvalResult]
    mean_local_ndcg_at_k: float
    mean_local_mrr: float
    mean_local_recall_at_k: float
    mean_fuse_ndcg_at_k: float
    mean_fuse_mrr: float
    mean_fuse_recall_at_k: float
    mean_themes_precision: float
    mean_themes_recall: float
    mean_themes_f1: float
    config_signature: dict[str, Any]
    generated_at: datetime


def _mean(values: Iterable[float]) -> float:
    """Arithmetic mean; 0.0 for an empty sequence (mirrors ``run_eval``)."""
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def run_graph_eval(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    backend: GraphBackend,
    local_cases: Sequence[LocalCaseLike],
    themes_cases: Sequence[ThemesCaseLike],
    external_id_to_doc_id: Mapping[str, str],
    embedder_factory: Callable[[], Embedder] | None = None,
    include_fuse: bool = False,
    ndcg_k: int = 5,
    recall_k: int = 20,
    backend_name: str = "unknown",
) -> GraphEvalReport:
    """Run the graph-retrieval eval over ``local_cases`` + ``themes_cases``.

    Drives :func:`brain.graph_rag.graph_rag_search` once per case per mode and
    scores each result with the reused G2-j scorers, mirroring ``run_eval``'s
    conventions (one search call per case, aggregate means, config signature)
    while staying a SEPARATE runner (spec §17d Q3).

    * **local** — ``mode='local'`` per local case; ``GraphContext.docs`` scored
      with :func:`~brain.eval.graph_retrieval.score_local_docs`.
    * **fuse** (when ``include_fuse``) — ``mode='fuse'`` per local case (same
      query + expected docs as local; spec §17d Q1 fuse is a ranked-doc mode);
      the hybrid leg's vector arm is fed by ``embedder_factory`` (FTS-only when
      absent — never-raise).
    * **themes** — ``mode='themes'`` per themes case; ``GraphContext.themes``
      keysets scored with :func:`~brain.eval.graph_retrieval.score_themes`.

    Args:
        conn: Live psycopg connection to the AGE test DB (or any built graph).
        cfg: Config carrying the graph caps + ``owner_participants`` (themes
            owner exclusion); a single cfg serves all modes (local/fuse ignore
            ``owner_participants``).
        backend: The :class:`~brain.graph_rag.backends.base.GraphBackend`.
        local_cases: Local/fuse cases (injected G2-j ``LOCAL_CASES``).
        themes_cases: Themes cases (injected G2-j ``THEMES_CASES``).
        external_id_to_doc_id: Maps each case's ``expected_doc_external_ids`` to
            the seeded document UUIDs (the corpus builder returns this).
        embedder_factory: Hybrid-leg embedder factory for fuse; ``None`` runs the
            fuse hybrid leg FTS-only.
        include_fuse: When ``True``, also run + score ``mode='fuse'`` per local
            case.
        ndcg_k: nDCG cutoff (default 5).
        recall_k: Recall cutoff (default 20).
        backend_name: Recorded in ``config_signature`` for baseline diffs.

    Returns:
        A frozen :class:`GraphEvalReport`.
    """
    doc_results: list[GraphDocEvalResult] = []
    themes_results: list[GraphThemesEvalResult] = []

    for case in local_cases:
        expected = [external_id_to_doc_id[ext] for ext in case.expected_doc_external_ids]
        doc_results.append(
            _score_doc_mode(
                conn,
                cfg,
                case.query,
                backend=backend,
                mode=LOCAL_MODE,
                expected=expected,
                ndcg_k=ndcg_k,
                recall_k=recall_k,
            )
        )
        if include_fuse:
            doc_results.append(
                _score_doc_mode(
                    conn,
                    cfg,
                    case.query,
                    backend=backend,
                    mode=FUSE_MODE,
                    expected=expected,
                    ndcg_k=ndcg_k,
                    recall_k=recall_k,
                    embedder_factory=embedder_factory,
                )
            )

    for tcase in themes_cases:
        ctx = graph_rag_search(
            conn, cfg, "", backend=backend, mode=THEMES_MODE, person=tcase.person
        )
        actual_keysets = [
            sorted({entity.canonical_key for entity in theme.entities})
            for theme in ctx.themes
        ]
        expected_keysets = [sorted(ks) for ks in tcase.expected_theme_keysets]
        score = score_themes(actual_keysets, expected_keysets)
        themes_results.append(
            GraphThemesEvalResult(
                person=tcase.person,
                expected_theme_keysets=expected_keysets,
                actual_theme_keysets=actual_keysets,
                precision=score.precision,
                recall=score.recall,
                f1=score.f1,
                matched=score.matched,
                n_expected=score.n_expected,
                n_actual=score.n_actual,
            )
        )

    local = [r for r in doc_results if r.mode == LOCAL_MODE]
    fuse = [r for r in doc_results if r.mode == FUSE_MODE]

    config_signature: dict[str, Any] = {
        "graph_depth": cfg.graph_depth,
        "graph_frontier_cap": cfg.graph_frontier_cap,
        "graph_min_edge_weight": cfg.graph_min_edge_weight,
        "graph_theme_limit": cfg.graph_theme_limit,
        "backend": backend_name,
        "include_fuse": include_fuse,
        "ndcg_k": ndcg_k,
        "recall_k": recall_k,
    }

    return GraphEvalReport(
        doc_results=doc_results,
        themes_results=themes_results,
        mean_local_ndcg_at_k=_mean(r.ndcg_at_k for r in local),
        mean_local_mrr=_mean(r.mrr for r in local),
        mean_local_recall_at_k=_mean(r.recall_at_k for r in local),
        mean_fuse_ndcg_at_k=_mean(r.ndcg_at_k for r in fuse),
        mean_fuse_mrr=_mean(r.mrr for r in fuse),
        mean_fuse_recall_at_k=_mean(r.recall_at_k for r in fuse),
        mean_themes_precision=_mean(r.precision for r in themes_results),
        mean_themes_recall=_mean(r.recall for r in themes_results),
        mean_themes_f1=_mean(r.f1 for r in themes_results),
        config_signature=config_signature,
        generated_at=datetime.now(tz=UTC),
    )


def _score_doc_mode(
    conn: psycopg.Connection[Any],
    cfg: Config,
    query: str,
    *,
    backend: GraphBackend,
    mode: str,
    expected: list[str],
    ndcg_k: int,
    recall_k: int,
    embedder_factory: Callable[[], Embedder] | None = None,
) -> GraphDocEvalResult:
    """Run one ranked-doc mode (local/fuse) for ``query`` and score its docs."""
    ctx = graph_rag_search(
        conn,
        cfg,
        query,
        backend=backend,
        mode=mode,
        embedder_factory=embedder_factory,
    )
    actual = [doc.document_id for doc in ctx.docs]
    score = score_local_docs(actual, expected, ndcg_k=ndcg_k, recall_k=recall_k)
    return GraphDocEvalResult(
        mode=mode,
        query=query,
        expected_doc_ids=expected,
        actual_doc_ids=actual,
        ndcg_at_k=score.ndcg_at_k,
        mrr=score.mrr,
        recall_at_k=score.recall_at_k,
        ndcg_k=ndcg_k,
        recall_k=recall_k,
    )
