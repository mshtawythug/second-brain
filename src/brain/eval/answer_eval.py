"""Answer-quality eval for `brain ask` (Plan 06, Phase 3).

Measures synthesized answers against a golden fact-set with deterministic,
local-only scoring — substring + significant-token overlap, NO LLM judge in the
loop — so the harness stays reproducible and never requires Ollama to *score*
(the live ``ask_fn`` that produces answers may, but that is the caller's
concern). Mirrors the hybrid-search eval package's shape (``corpus`` loader +
dataclass report) so the two read consistently.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .errors import EvalCorpusError

if TYPE_CHECKING:
    from ..ask import AskResult

# Default answer-corpus path: eval/ -> brain/ -> src/ -> repo root, then into
# tests/eval/. Committed (unlike the hybrid golden corpus) because it is fully
# synthetic — no real doc IDs, names, or employers (CLAUDE.md rule 15).
_DEFAULT_ANSWER_CORPUS_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "eval"
    / "answer_corpus.yaml"
)

_VALID_ANSWER_CATEGORIES: frozenset[str] = frozenset(
    ("synthesis", "multi-hop", "person", "timeline")
)

_REQUIRED_ANSWER_FIELDS: frozenset[str] = frozenset(
    ("question", "expected_facts", "category")
)
_ALLOWED_ANSWER_FIELDS: frozenset[str] = frozenset(
    ("question", "expected_facts", "category", "notes")
)
_ANSWER_CORPUS_VERSION = 1

# A fact counts as covered when at least this fraction of its significant
# (non-stopword) tokens appear in the answer — a majority by default. Single
# distinctive tokens (ratio 1.0) and exact phrase substrings always pass.
_FACT_COVERAGE_THRESHOLD = 0.5

# Minimal English stopword set for significant-token extraction. Deliberately
# small — only the highest-frequency function words — so distinctive content
# words (the signal we score on) survive.
_STOPWORDS: frozenset[str] = frozenset(
    (
        "the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "with",
        "is", "are", "was", "were", "be", "been", "my", "i", "we", "you",
        "that", "this", "it", "as", "at", "by", "from", "about", "into",
    )
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class AnswerEvalCase:
    """One golden Q&A case from the answer corpus."""

    question: str
    expected_facts: list[str]  # short synthetic phrases; all should appear/be implied
    category: str  # one of _VALID_ANSWER_CATEGORIES
    notes: str = ""  # human-readable rationale, not graded


@dataclass(frozen=True)
class AnswerScore:
    """Per-case answer-quality score."""

    question: str
    category: str
    fact_recall: float  # fraction of expected_facts covered by the answer
    citation_count: int  # number of distinct citations the answer produced


@dataclass(frozen=True)
class AnswerEvalReport:
    """Aggregate answer-eval result over a corpus run."""

    scores: list[AnswerScore]
    mean_fact_recall: float
    mean_citation_count: float
    timestamp: str  # ISO-8601


def _significant_tokens(text: str) -> list[str]:
    """Lowercase content tokens with stopwords removed."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _fact_covered(answer_lower: str, answer_tokens: set[str], fact: str) -> bool:
    """Return True if ``fact`` is covered by the answer.

    Covered when the exact (lowercased) fact phrase is a substring of the
    answer, OR at least :data:`_FACT_COVERAGE_THRESHOLD` of the fact's
    significant tokens appear in the answer's token set.
    """
    if fact.strip().lower() in answer_lower:
        return True
    fact_tokens = _significant_tokens(fact)
    if not fact_tokens:
        # No content words to compare — fall back to the substring result above.
        return False
    present = sum(1 for t in fact_tokens if t in answer_tokens)
    return present / len(fact_tokens) >= _FACT_COVERAGE_THRESHOLD


def score_answer(answer: str, expected_facts: list[str]) -> float:
    """Fraction of ``expected_facts`` covered by ``answer`` in ``[0.0, 1.0]``.

    Deterministic substring + significant-token-overlap matching (no LLM). An
    empty ``expected_facts`` list scores ``1.0`` (nothing to miss).
    """
    if not expected_facts:
        return 1.0
    answer_lower = answer.lower()
    answer_tokens = set(_significant_tokens(answer))
    covered = sum(
        1
        for fact in expected_facts
        if _fact_covered(answer_lower, answer_tokens, fact)
    )
    return covered / len(expected_facts)


def run_answer_eval(
    cases: list[AnswerEvalCase],
    ask_fn: Callable[[str], AskResult],
    *,
    now: datetime | None = None,
) -> AnswerEvalReport:
    """Run ``ask_fn`` over every case and score the answers.

    ``ask_fn`` maps a question to an :class:`~brain.ask.AskResult` (typically a
    closure over ``ask_no_loop`` against the live brain). ``now`` overrides the
    report timestamp (defaults to the current UTC time) so tests stay
    deterministic.
    """
    scores: list[AnswerScore] = []
    for case in cases:
        result = ask_fn(case.question)
        scores.append(
            AnswerScore(
                question=case.question,
                category=case.category,
                fact_recall=score_answer(result.answer, case.expected_facts),
                citation_count=len(result.citations),
            )
        )
    if scores:
        mean_recall = sum(s.fact_recall for s in scores) / len(scores)
        mean_citations = sum(s.citation_count for s in scores) / len(scores)
    else:
        mean_recall = 0.0
        mean_citations = 0.0
    stamp = (now or datetime.now(UTC)).isoformat()
    return AnswerEvalReport(
        scores=scores,
        mean_fact_recall=mean_recall,
        mean_citation_count=mean_citations,
        timestamp=stamp,
    )


def load_answer_corpus(path: Path | None = None) -> list[AnswerEvalCase]:
    """Load + validate the answer-eval corpus YAML.

    Raises:
        EvalCorpusError: missing/malformed file, version mismatch, unknown
            category, missing required field, or empty ``expected_facts``.
    """
    if path is None:
        path = _DEFAULT_ANSWER_CORPUS_PATH

    if not path.exists():
        raise EvalCorpusError(f"answer corpus file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EvalCorpusError(
            f"answer corpus YAML parse error in {path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise EvalCorpusError(
            f"answer corpus file must be a YAML mapping, got {type(raw).__name__}"
        )

    version = raw.get("version")
    if version != _ANSWER_CORPUS_VERSION:
        raise EvalCorpusError(
            f"answer corpus version mismatch: expected {_ANSWER_CORPUS_VERSION}, "
            f"got {version!r}."
        )

    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list):
        raise EvalCorpusError("answer corpus file must have a 'cases' list")

    cases: list[AnswerEvalCase] = []
    for i, entry in enumerate(raw_cases):
        if not isinstance(entry, dict):
            raise EvalCorpusError(
                f"answer corpus case #{i + 1} must be a YAML mapping, "
                f"got {type(entry).__name__}"
            )

        missing = _REQUIRED_ANSWER_FIELDS - entry.keys()
        if missing:
            raise EvalCorpusError(
                f"answer corpus case #{i + 1} is missing required field(s): "
                f"{', '.join(sorted(missing))}"
            )

        unknown = entry.keys() - _ALLOWED_ANSWER_FIELDS
        if unknown:
            raise EvalCorpusError(
                f"answer corpus case #{i + 1} has unknown field(s): "
                f"{', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_ALLOWED_ANSWER_FIELDS))}"
            )

        category = entry["category"]
        if category not in _VALID_ANSWER_CATEGORIES:
            raise EvalCorpusError(
                f"answer corpus case #{i + 1} has unknown category {category!r}. "
                f"Valid: {', '.join(sorted(_VALID_ANSWER_CATEGORIES))}"
            )

        expected_facts = entry["expected_facts"]
        if not isinstance(expected_facts, list):
            raise EvalCorpusError(
                f"answer corpus case #{i + 1} 'expected_facts' must be a list, "
                f"got {type(expected_facts).__name__}"
            )
        if not expected_facts:
            raise EvalCorpusError(
                f"answer corpus case #{i + 1} ({entry['question']!r}) has empty "
                f"'expected_facts'"
            )

        cases.append(
            AnswerEvalCase(
                question=str(entry["question"]),
                expected_facts=[str(f) for f in expected_facts],
                category=category,
                notes=str(entry.get("notes", "")),
            )
        )

    return cases


def answer_eval_report_to_dict(report: AnswerEvalReport) -> dict[str, Any]:
    """JSON projection of an :class:`AnswerEvalReport` (CLI ``--json``)."""
    return {
        "mean_fact_recall": report.mean_fact_recall,
        "mean_citation_count": report.mean_citation_count,
        "timestamp": report.timestamp,
        "scores": [
            {
                "question": s.question,
                "category": s.category,
                "fact_recall": s.fact_recall,
                "citation_count": s.citation_count,
            }
            for s in report.scores
        ],
    }
