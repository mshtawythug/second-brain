"""Eval harness for second-brain hybrid search.

Public surface re-exported from sub-modules:

- :class:`EvalQuery` — a single query from the golden corpus
- :class:`EvalResult` / :class:`EvalReport` / :class:`CategorySummary` — run output types
- :func:`run_eval` — execute the harness against a live DB + embedder
- :func:`load_corpus` — parse a golden-corpus YAML file
- :class:`BaselineDiff` / :class:`QueryDiff` — diff data types
- :func:`save_baseline` / :func:`load_baseline` / :func:`diff_reports` — baseline I/O
- :class:`ConceptF1Report` / :class:`ConceptFixtureDoc` / :func:`concept_set_micro_f1`
  / :func:`normalize_concept_pairs` / :func:`load_concept_fixture` — GraphRAG
  concept-extractor eval gate (wave G2-j)
- :class:`LocalRetrievalScore` / :class:`ThemeRetrievalScore`
  / :func:`score_local_docs` / :func:`score_themes` — GraphRAG graph-retrieval
  eval scorers (wave G2-j)
- :class:`GraphDocEvalResult` / :class:`GraphThemesEvalResult`
  / :class:`GraphEvalReport` / :func:`run_graph_eval` — parallel GraphRAG
  graph-retrieval eval runner (wave G4-d; separate from the hybrid runner)
- :class:`GraphBaselineDiff` / :class:`GraphDocDiff` / :class:`GraphThemesDiff`
  / :func:`save_graph_baseline` / :func:`load_graph_baseline`
  / :func:`diff_graph_reports` — graph-eval canary baseline I/O (wave G4-d)
- :class:`EvalError` / :class:`EvalMetricError` / :class:`EvalCorpusError`
  / :class:`EvalBaselineError` — exception hierarchy
"""

from .answer_eval import (
    AnswerEvalCase,
    AnswerEvalReport,
    AnswerScore,
    answer_eval_report_to_dict,
    load_answer_corpus,
    run_answer_eval,
    score_answer,
)
from .baseline import BaselineDiff, QueryDiff, diff_reports, load_baseline, save_baseline
from .concept_extraction import (
    ConceptF1Report,
    ConceptFixtureDoc,
    ConceptPair,
    concept_set_micro_f1,
    load_concept_fixture,
    normalize_concept_pairs,
)
from .corpus import EvalQuery, load_corpus
from .errors import EvalBaselineError, EvalCorpusError, EvalError, EvalMetricError
from .graph_baseline import (
    GraphBaselineDiff,
    GraphDocDiff,
    GraphThemesDiff,
    diff_graph_reports,
    load_graph_baseline,
    save_graph_baseline,
)
from .graph_retrieval import (
    LocalRetrievalScore,
    ThemeRetrievalScore,
    score_local_docs,
    score_themes,
)
from .graph_runner import (
    GraphDocEvalResult,
    GraphEvalReport,
    GraphThemesEvalResult,
    run_graph_eval,
)
from .runner import CategorySummary, EvalReport, EvalResult, run_eval

__all__ = [
    # Corpus
    "EvalQuery",
    "load_corpus",
    # Answer-quality eval (Plan 06)
    "AnswerEvalCase",
    "AnswerScore",
    "AnswerEvalReport",
    "score_answer",
    "run_answer_eval",
    "load_answer_corpus",
    "answer_eval_report_to_dict",
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
    # GraphRAG concept-extractor eval gate (G2-j)
    "ConceptF1Report",
    "ConceptFixtureDoc",
    "ConceptPair",
    "concept_set_micro_f1",
    "load_concept_fixture",
    "normalize_concept_pairs",
    # GraphRAG graph-retrieval eval scorers (G2-j)
    "LocalRetrievalScore",
    "ThemeRetrievalScore",
    "score_local_docs",
    "score_themes",
    # GraphRAG graph-retrieval eval runner (G4-d)
    "GraphDocEvalResult",
    "GraphThemesEvalResult",
    "GraphEvalReport",
    "run_graph_eval",
    # GraphRAG graph-eval canary baseline I/O (G4-d)
    "GraphBaselineDiff",
    "GraphDocDiff",
    "GraphThemesDiff",
    "save_graph_baseline",
    "load_graph_baseline",
    "diff_graph_reports",
    # Errors
    "EvalError",
    "EvalMetricError",
    "EvalCorpusError",
    "EvalBaselineError",
]
