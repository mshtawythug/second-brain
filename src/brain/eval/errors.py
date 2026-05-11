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
