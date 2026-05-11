"""Eval harness for second-brain hybrid search.

Public surface re-exported from sub-modules:

- :class:`EvalQuery` — a single query from the golden corpus
- :class:`EvalResult` / :class:`EvalReport` / :class:`CategorySummary` — run output types
- :func:`run_eval` — execute the harness against a live DB + embedder
- :func:`load_corpus` — parse a golden-corpus YAML file
- :class:`BaselineDiff` / :class:`QueryDiff` — diff data types
- :func:`save_baseline` / :func:`load_baseline` / :func:`diff_reports` — baseline I/O
- :class:`EvalError` / :class:`EvalMetricError` / :class:`EvalCorpusError`
  / :class:`EvalBaselineError` — exception hierarchy
"""

from .baseline import BaselineDiff, QueryDiff, diff_reports, load_baseline, save_baseline
from .corpus import EvalQuery, load_corpus
from .errors import EvalBaselineError, EvalCorpusError, EvalError, EvalMetricError
from .runner import CategorySummary, EvalReport, EvalResult, run_eval

__all__ = [
    # Corpus
    "EvalQuery",
    "load_corpus",
    # Runner output
    "EvalResult",
    "EvalReport",
    "CategorySummary",
    "run_eval",
    # Baseline I/O
    "BaselineDiff",
    "QueryDiff",
    "save_baseline",
    "load_baseline",
    "diff_reports",
    # Errors
    "EvalError",
    "EvalMetricError",
    "EvalCorpusError",
    "EvalBaselineError",
]
