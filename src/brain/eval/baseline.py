"""Baseline save/load and diff helpers for eval reports.

Baselines are stored as JSON files (sorted keys, floats rounded to 4 decimals
for byte-stable diffs) via an atomic write so a crash can never leave a
half-written file.  Baseline names are validated against a strict allowlist
pattern to prevent path traversal.
"""

import dataclasses
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..vault._atomic import atomic_write_text
from .errors import EvalBaselineError
from .runner import CategorySummary, EvalReport, EvalResult

_BASELINE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _assert_baseline_name(name: str) -> None:
    """Validate that *name* is safe to use as a filename component.

    Raises:
        EvalBaselineError: When *name* contains characters outside
            ``[A-Za-z0-9_-]`` (e.g. slashes, dots, spaces).
    """
    if not _BASELINE_NAME_RE.fullmatch(name):
        raise EvalBaselineError(
            f"baseline name {name!r} is invalid; "
            f"use only letters, digits, hyphens, and underscores"
        )


def _round_floats(obj: Any, decimals: int = 4) -> Any:
    """Recursively round all floats in a JSON-compatible structure."""
    if isinstance(obj, float):
        return round(obj, decimals)
    if isinstance(obj, dict):
        return {k: _round_floats(v, decimals) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, decimals) for v in obj]
    return obj


def save_baseline(report: EvalReport, *, path: Path) -> None:
    """Write *report* to *path* as JSON, atomically.

    Keys are sorted and floats rounded to 4 decimal places so repeated
    runs over the same corpus produce byte-stable diffs.  The parent
    directory is created if it does not exist.

    Args:
        report: The eval report to persist.
        path: Destination file path (typically under ``tests/eval/baselines/``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = dataclasses.asdict(report)
    raw = _round_floats(raw)
    # datetime → ISO-8601 with Z suffix for UTC.
    raw["generated_at"] = report.generated_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    text = json.dumps(raw, sort_keys=True, default=str, indent=2) + "\n"
    atomic_write_text(path, text)


def load_baseline(path: Path) -> EvalReport:
    """Load an :class:`~brain.eval.runner.EvalReport` from a JSON baseline file.

    Args:
        path: Path to the baseline JSON file.

    Raises:
        EvalBaselineError: When the file is missing, contains invalid JSON,
            or has an unexpected structure.
    """
    if not path.exists():
        raise EvalBaselineError(f"baseline file not found: {path}")
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalBaselineError(
            f"baseline file contains invalid JSON: {path}: {exc}"
        ) from exc
    try:
        results = [
            EvalResult(
                query=r["query"],
                category=r["category"],
                expected_doc_ids=list(r["expected_doc_ids"]),
                actual_doc_ids=list(r["actual_doc_ids"]),
                ndcg_at_5=float(r["ndcg_at_5"]),
                mrr=float(r["mrr"]),
                recall_at_20=float(r["recall_at_20"]),
            )
            for r in data["results"]
        ]
        per_category = {
            k: CategorySummary(
                category=v["category"],
                count=int(v["count"]),
                mean_ndcg_at_5=float(v["mean_ndcg_at_5"]),
                mean_mrr=float(v["mean_mrr"]),
                mean_recall_at_20=float(v["mean_recall_at_20"]),
            )
            for k, v in data["per_category"].items()
        }
        generated_at_raw: str = data["generated_at"]
        # Python 3.11+ accepts the "Z" suffix in fromisoformat.
        generated_at = datetime.fromisoformat(generated_at_raw)
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        return EvalReport(
            results=results,
            mean_ndcg_at_5=float(data["mean_ndcg_at_5"]),
            mean_mrr=float(data["mean_mrr"]),
            mean_recall_at_20=float(data["mean_recall_at_20"]),
            per_category=per_category,
            config_signature=dict(data["config_signature"]),
            generated_at=generated_at,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise EvalBaselineError(
            f"baseline file has unexpected structure: {path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryDiff:
    """Per-query metric delta between a baseline and a current run."""

    query: str
    category: str
    ndcg_at_5_delta: float  # current - baseline; negative = regression
    mrr_delta: float
    recall_at_20_delta: float


@dataclass(frozen=True)
class BaselineDiff:
    """Aggregate diff between a baseline :class:`EvalReport` and a current run."""

    per_query: list[QueryDiff]
    mean_ndcg_at_5_delta: float
    mean_mrr_delta: float
    mean_recall_at_20_delta: float
    config_signature_changed: bool
    baseline_signature: dict[str, Any]
    current_signature: dict[str, Any]


def diff_reports(baseline: EvalReport, current: EvalReport) -> BaselineDiff:
    """Compute the delta between *baseline* and *current*.

    Queries are matched by query string.  A query present in one report but
    not the other contributes a delta of 0.0 for the missing side.

    Args:
        baseline: The reference (earlier) eval report.
        current: The new eval report to compare.

    Returns:
        A :class:`BaselineDiff` with per-query and aggregate deltas.
    """
    # Build lookup dicts keyed by query string.
    base_by_q = {r.query: r for r in baseline.results}
    curr_by_q = {r.query: r for r in current.results}

    all_queries = dict.fromkeys(list(base_by_q) + list(curr_by_q))
    per_query: list[QueryDiff] = []
    for q_str in all_queries:
        base_r = base_by_q.get(q_str)
        curr_r = curr_by_q.get(q_str)
        base_ndcg = base_r.ndcg_at_5 if base_r else 0.0
        base_mrr = base_r.mrr if base_r else 0.0
        base_recall = base_r.recall_at_20 if base_r else 0.0
        curr_ndcg = curr_r.ndcg_at_5 if curr_r else 0.0
        curr_mrr = curr_r.mrr if curr_r else 0.0
        curr_recall = curr_r.recall_at_20 if curr_r else 0.0
        category = (curr_r or base_r).category  # type: ignore[union-attr]
        per_query.append(
            QueryDiff(
                query=q_str,
                category=category,
                ndcg_at_5_delta=curr_ndcg - base_ndcg,
                mrr_delta=curr_mrr - base_mrr,
                recall_at_20_delta=curr_recall - base_recall,
            )
        )

    n = len(per_query)
    mean_ndcg_delta = sum(d.ndcg_at_5_delta for d in per_query) / n if n else 0.0
    mean_mrr_delta = sum(d.mrr_delta for d in per_query) / n if n else 0.0
    mean_recall_delta = sum(d.recall_at_20_delta for d in per_query) / n if n else 0.0

    return BaselineDiff(
        per_query=per_query,
        mean_ndcg_at_5_delta=mean_ndcg_delta,
        mean_mrr_delta=mean_mrr_delta,
        mean_recall_at_20_delta=mean_recall_delta,
        config_signature_changed=baseline.config_signature != current.config_signature,
        baseline_signature=dict(baseline.config_signature),
        current_signature=dict(current.config_signature),
    )
