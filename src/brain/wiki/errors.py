"""Wiki-pipeline exceptions."""
from brain.errors import BrainError


class BrainWikiError(BrainError):
    """Base class for wiki-pipeline failures (build, swap, watch)."""


class BrainWikiBuildError(BrainWikiError):
    """The Quartz build subprocess exited non-zero, timed out, or the workspace is broken."""
