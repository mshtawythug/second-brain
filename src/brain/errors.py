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


class DraftSkipped(BrainError):
    """Raised when a Gmail message or thread is skipped because it's a draft.

    Drafts (labelIds containing ``DRAFT``) are unsent emails the user typed
    but never sent. Ingesting them pollutes search ("did I send X to Y?"
    returns drafts as evidence of sent messages → wrong answer). Callers
    catch this exception and increment a "skipped (drafts)" counter
    instead of treating it as a failure.
    """
