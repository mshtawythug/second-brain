"""Eval-specific exception hierarchy (all inherit from BrainError)."""

from brain.errors import BrainError


class EvalError(BrainError):
    """Base class for all eval-package exceptions."""


class EvalMetricError(EvalError):
    """Raised when a metric function receives invalid inputs (e.g. empty expected set)."""


class EvalCorpusError(EvalError):
    """Raised on corpus YAML validation failures (unknown category, missing field, etc.)."""


class EvalBaselineError(EvalError):
    """Raised when a baseline file is missing, unreadable, or malformed."""


class DegradedBaselineError(EvalBaselineError):
    """Raised when asked to save a baseline from a run whose embed calls failed.

    A degraded row scores 0.0 because the embedder was unhealthy, not because
    retrieval missed; saved, it would become the reference every later
    ``--fail-below`` run is judged against.
    """


class StageSkippedBaselineError(DegradedBaselineError):
    """Raised when asked to save a baseline from a run in which a ranking stage skipped.

    A row whose stage (the reranker, RM-14a) skipped was scored in hybrid order,
    while ``config_signature`` names the stage, so it measured a pipeline the
    report does not describe. A subclass of :class:`DegradedBaselineError` so
    every caller that refuses a degraded run (``brain eval --record-baseline``,
    exit 1) refuses this one with no change of its own.
    """
