"""Baseline save/load + diff for the graph-eval report (wave G4-d; spec §17d Q3).

The graph analogue of :mod:`brain.eval.baseline` for
:class:`brain.eval.graph_runner.GraphEvalReport`. A SEPARATE baseline path from
the hybrid ``EvalReport`` baseline because the two report shapes differ (graph
carries local/fuse ranked-doc metrics + themes set P/R/F1; spec §17d Q3).

This baseline is a **canary** — it round-trips in tests and is intentionally NOT
committed as a ``ci.json``, and there is **no** ``--fail-below`` gate (spec §17d
Q3): the blocking thresholds live in the synthetic-graph integration test, not a
committed-baseline CI gate. ``diff_graph_reports`` is for local/manual
regression inspection.

Conventions mirror :mod:`brain.eval.baseline`: atomic write, sorted keys, floats
rounded to 4 decimals for byte-stable diffs. The :func:`brain.eval.baseline._round_floats`
helper is reused (one rounding implementation for both baseline shapes).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..vault._atomic import atomic_write_text
from .baseline import _round_floats
from .errors import EvalBaselineError
from .graph_runner import GraphDocEvalResult, GraphEvalReport, GraphThemesEvalResult


def save_graph_baseline(report: GraphEvalReport, *, path: Path) -> None:
    """Write *report* to *path* as JSON, atomically.

    Keys are sorted and floats rounded to 4 decimals so repeated runs over the
    same synthetic corpus produce byte-stable diffs. The parent directory is
    created if missing. The caller owns *path* (no embedded baseline-name
    validation — there is no CLI surface for graph baselines; spec §17d Q3).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = dataclasses.asdict(report)
    raw = _round_floats(raw)
    raw["generated_at"] = report.generated_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    text = json.dumps(raw, sort_keys=True, default=str, indent=2) + "\n"
    atomic_write_text(path, text)


def load_graph_baseline(path: Path) -> GraphEvalReport:
    """Load a :class:`~brain.eval.graph_runner.GraphEvalReport` from *path*.

    Raises:
        EvalBaselineError: When the file is missing, contains invalid JSON, or
            has an unexpected structure.
    """
    if not path.exists():
        raise EvalBaselineError(f"graph baseline file not found: {path}")
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalBaselineError(
            f"graph baseline file contains invalid JSON: {path}: {exc}"
        ) from exc
    try:
        doc_results = [
            GraphDocEvalResult(
                mode=r["mode"],
                query=r["query"],
                expected_doc_ids=list(r["expected_doc_ids"]),
                actual_doc_ids=list(r["actual_doc_ids"]),
                ndcg_at_k=float(r["ndcg_at_k"]),
                mrr=float(r["mrr"]),
                recall_at_k=float(r["recall_at_k"]),
                ndcg_k=int(r["ndcg_k"]),
                recall_k=int(r["recall_k"]),
            )
            for r in data["doc_results"]
        ]
        themes_results = [
            GraphThemesEvalResult(
                person=r["person"],
                expected_theme_keysets=[list(ks) for ks in r["expected_theme_keysets"]],
                actual_theme_keysets=[list(ks) for ks in r["actual_theme_keysets"]],
                precision=float(r["precision"]),
                recall=float(r["recall"]),
                f1=float(r["f1"]),
                matched=int(r["matched"]),
                n_expected=int(r["n_expected"]),
                n_actual=int(r["n_actual"]),
            )
            for r in data["themes_results"]
        ]
        generated_at = datetime.fromisoformat(data["generated_at"])
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        return GraphEvalReport(
            doc_results=doc_results,
            themes_results=themes_results,
            mean_local_ndcg_at_k=float(data["mean_local_ndcg_at_k"]),
            mean_local_mrr=float(data["mean_local_mrr"]),
            mean_local_recall_at_k=float(data["mean_local_recall_at_k"]),
            mean_fuse_ndcg_at_k=float(data["mean_fuse_ndcg_at_k"]),
            mean_fuse_mrr=float(data["mean_fuse_mrr"]),
            mean_fuse_recall_at_k=float(data["mean_fuse_recall_at_k"]),
            mean_themes_precision=float(data["mean_themes_precision"]),
            mean_themes_recall=float(data["mean_themes_recall"]),
            mean_themes_f1=float(data["mean_themes_f1"]),
            config_signature=dict(data["config_signature"]),
            generated_at=generated_at,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise EvalBaselineError(
            f"graph baseline file has unexpected structure: {path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphDocDiff:
    """Per-case ranked-doc metric delta (current - baseline; negative = worse)."""

    mode: str
    query: str
    ndcg_at_k_delta: float
    mrr_delta: float
    recall_at_k_delta: float


@dataclass(frozen=True)
class GraphThemesDiff:
    """Per-person themes metric delta (current - baseline; negative = worse)."""

    person: str
    precision_delta: float
    recall_delta: float
    f1_delta: float


@dataclass(frozen=True)
class GraphBaselineDiff:
    """Aggregate diff between two :class:`GraphEvalReport`s."""

    per_doc: list[GraphDocDiff]
    per_themes: list[GraphThemesDiff]
    mean_local_ndcg_at_k_delta: float
    mean_local_mrr_delta: float
    mean_local_recall_at_k_delta: float
    mean_fuse_ndcg_at_k_delta: float
    mean_fuse_mrr_delta: float
    mean_fuse_recall_at_k_delta: float
    mean_themes_precision_delta: float
    mean_themes_recall_delta: float
    mean_themes_f1_delta: float
    config_signature_changed: bool
    baseline_signature: dict[str, Any]
    current_signature: dict[str, Any]


def diff_graph_reports(
    baseline: GraphEvalReport, current: GraphEvalReport
) -> GraphBaselineDiff:
    """Compute the delta between *baseline* and *current*.

    Doc results are matched by ``(mode, query)``; themes results by ``person``.
    A case present in one report but not the other contributes a 0.0 baseline
    side (mirroring :func:`brain.eval.baseline.diff_reports`).
    """
    base_doc = {(r.mode, r.query): r for r in baseline.doc_results}
    curr_doc = {(r.mode, r.query): r for r in current.doc_results}
    per_doc: list[GraphDocDiff] = []
    for key in dict.fromkeys(list(base_doc) + list(curr_doc)):
        b = base_doc.get(key)
        c = curr_doc.get(key)
        per_doc.append(
            GraphDocDiff(
                mode=key[0],
                query=key[1],
                ndcg_at_k_delta=(c.ndcg_at_k if c else 0.0) - (b.ndcg_at_k if b else 0.0),
                mrr_delta=(c.mrr if c else 0.0) - (b.mrr if b else 0.0),
                recall_at_k_delta=(c.recall_at_k if c else 0.0)
                - (b.recall_at_k if b else 0.0),
            )
        )

    base_themes = {r.person: r for r in baseline.themes_results}
    curr_themes = {r.person: r for r in current.themes_results}
    per_themes: list[GraphThemesDiff] = []
    for person in dict.fromkeys(list(base_themes) + list(curr_themes)):
        b_t = base_themes.get(person)
        c_t = curr_themes.get(person)
        per_themes.append(
            GraphThemesDiff(
                person=person,
                precision_delta=(c_t.precision if c_t else 0.0)
                - (b_t.precision if b_t else 0.0),
                recall_delta=(c_t.recall if c_t else 0.0) - (b_t.recall if b_t else 0.0),
                f1_delta=(c_t.f1 if c_t else 0.0) - (b_t.f1 if b_t else 0.0),
            )
        )

    return GraphBaselineDiff(
        per_doc=per_doc,
        per_themes=per_themes,
        mean_local_ndcg_at_k_delta=current.mean_local_ndcg_at_k
        - baseline.mean_local_ndcg_at_k,
        mean_local_mrr_delta=current.mean_local_mrr - baseline.mean_local_mrr,
        mean_local_recall_at_k_delta=current.mean_local_recall_at_k
        - baseline.mean_local_recall_at_k,
        mean_fuse_ndcg_at_k_delta=current.mean_fuse_ndcg_at_k
        - baseline.mean_fuse_ndcg_at_k,
        mean_fuse_mrr_delta=current.mean_fuse_mrr - baseline.mean_fuse_mrr,
        mean_fuse_recall_at_k_delta=current.mean_fuse_recall_at_k
        - baseline.mean_fuse_recall_at_k,
        mean_themes_precision_delta=current.mean_themes_precision
        - baseline.mean_themes_precision,
        mean_themes_recall_delta=current.mean_themes_recall
        - baseline.mean_themes_recall,
        mean_themes_f1_delta=current.mean_themes_f1 - baseline.mean_themes_f1,
        config_signature_changed=baseline.config_signature != current.config_signature,
        baseline_signature=dict(baseline.config_signature),
        current_signature=dict(current.config_signature),
    )
