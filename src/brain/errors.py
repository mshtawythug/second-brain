"""Project-specific exception hierarchy.

Internal helpers that can fail in user-visible ways raise these exceptions so
the CLI and MCP server layers can map them to their respective frameworks
(``typer.Exit`` / ``McpError``) without sharing framework-specific imports.
"""


class BrainError(Exception):
    """Base class for all brain-internal exceptions."""


class IdPrefixError(BrainError):
    """Base class for failures resolving a UUID prefix to a document id."""


class IdPrefixTooShort(IdPrefixError):
    """The supplied prefix is shorter than the 6-char minimum."""


class IdPrefixNotHex(IdPrefixError):
    """The supplied prefix contains characters other than hex digits / hyphens."""


class IdPrefixNotFound(IdPrefixError):
    """No document matches the supplied prefix."""

    def __init__(self, prefix: str) -> None:
        super().__init__(f"document not found: {prefix}")
        self.prefix = prefix


class IdPrefixAmbiguous(IdPrefixError):
    """Multiple documents match the supplied prefix."""

    def __init__(self, prefix: str) -> None:
        super().__init__(f"id prefix ambiguous: {prefix}")
        self.prefix = prefix


class DirectoryRefreshError(BrainError):
    """Raised when a Calendar / Contacts refresh fails (gws missing, JSON parse, etc.)."""


class InteractionError(BrainError):
    """Raised on invalid interaction inputs (unknown action / source).

    The DB-level ``CHECK`` constraints on ``interactions.action`` and
    ``interactions.source`` are the authoritative gate; this Python-side
    error gives Typer / MCP a clean message before the SQL round-trip
    when the enum value is obviously wrong (e.g., typo at the call site).
    """


class PersonAmbiguous(BrainError):
    """Multiple persons match a ``--person`` argument; caller must disambiguate."""

    def __init__(self, query: str, candidates: list[str]) -> None:
        candidate_list = ", ".join(candidates[:5])
        super().__init__(
            f"--person {query!r} matched {len(candidates)} people "
            f"(candidates: {candidate_list}). Use a more specific name."
        )
        self.query = query
        self.candidates = candidates


class PersonNotFound(BrainError):
    """No person matched the ``--person`` argument."""

    def __init__(self, query: str) -> None:
        super().__init__(f"--person {query!r} matched no one in the directory")
        self.query = query


class DraftSkipped(BrainError):
    """Reserved for future opt-in draft-skip paths.

    .. deprecated::
        No longer raised by the default Gmail ingest path (wave Q1-A,
        2026-05-11). All Gmail drafts are now ingested with
        ``documents.draft = TRUE`` so the wiki quarantine (P1.6,
        ``contentIndex.ts:397``) hides them from Quartz while
        ``brain search`` / ``brain show`` still surface them. This class
        is kept so future callers (e.g. a ``--skip-drafts`` flag) can
        raise it without a schema change.
    """
