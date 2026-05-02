"""Wiki-pipeline exceptions."""
from brain.errors import BrainError


class BrainWikiError(BrainError):
    """Base class for wiki-pipeline failures (build, swap, watch)."""


class BrainWikiBuildError(BrainWikiError):
    """The ``npx quartz build`` subprocess exited non-zero or never produced output."""
