"""The failures ``brain ask`` maps to a clean error, and their fixed, class-only sentences (RM-02).

One place for the CLI ``ask`` command and MCP ``brain_ask`` (and, later, the UI's
Agent tab, RM-18a) so the three cannot describe the same failure differently.

Every sentence is fixed text plus at most a class name: an exception message can
carry text the user did not choose to print (a chat error embeds the model's
reply, a psycopg error can quote row values), so it is never interpolated. That
is DoD item 6 of the roadmap spec; the handlers this replaced printed ``{exc}``.

An embed failure in the hybrid leg never reaches here: ``ask`` falls back to
text-only retrieval (:mod:`brain.search_fallback`). What remains is a graph mode
that embeds the query outside the hybrid leg.
"""
from __future__ import annotations

from .errors import (
    ChatOutputTruncated,
    EmbedError,
    EnrichmentError,
    GraphBackendError,
    GraphTenantError,
    OllamaContextOverflow,
    OllamaUnavailable,
)

#: The exception families ``ask`` maps to one clean error. ``EnrichmentError``
#: covers ``OllamaUnavailable``, ``OllamaContextOverflow`` and ``ChatOutputTruncated``.
ASK_FAILURES: tuple[type[BaseException], ...] = (
    EnrichmentError,
    GraphTenantError,
    GraphBackendError,
    EmbedError,
)


def describe_ask_failure(exc: BaseException) -> str:
    """One fixed, class-only sentence per failure family; never the exception message.

    The three ``EnrichmentError`` subclasses are tested before their base, so
    each keeps its own advice.
    """
    if isinstance(exc, OllamaUnavailable):
        return (
            "Ollama is not available. Start it (for example `brew services start "
            "ollama`) and retry."
        )
    if isinstance(exc, OllamaContextOverflow):
        return (
            "the question and the retrieved documents did not fit the chat model's "
            "context window; ask a narrower question or lower `--limit`"
        )
    if isinstance(exc, ChatOutputTruncated):
        return (
            "the model's answer was cut off at its output budget; retry, or ask a "
            "narrower question"
        )
    name = type(exc).__name__
    if isinstance(exc, EnrichmentError):
        return f"answer synthesis failed ({name})"
    if isinstance(exc, (GraphTenantError, GraphBackendError)):
        return f"graph retrieval failed ({name})"
    if isinstance(exc, EmbedError):
        if exc.transient:
            return "the embedding model is unavailable; retry later"
        return (
            "the embedding model is not usable (not pulled, or a bad key); run "
            "`brain doctor`"
        )
    return f"ask failed ({name})"
