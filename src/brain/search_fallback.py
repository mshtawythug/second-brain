"""Text-only (FTS) fallback for user-facing search when the query embed fails.

The user's decision (plan ``docs/plans/2026-10-23-search-fts-fallback.md``):
when the embedder is unreachable, ``brain search``, ``brain explain`` and MCP
``brain_search``, (RM-01) ``brain recall`` / MCP ``brain_recall``, and (RM-02)
the retrieval of ``brain ask`` / MCP ``brain_ask``, return text-only matches and
SAY SO, instead of failing.

This module is the one place that policy lives, and it adds no search code of
its own: on an :class:`~brain.errors.EmbedError` it calls the surface's search
function again with ``fts_only=True``, the same path ``--fts-only`` and the
``BRAIN_EMBEDDER=none`` ``NullEmbedder`` already take. Both transient and
permanent embed failures fall back; the notice tells them apart, because "retry
later" is the wrong advice for a model that was never pulled. A query longer
than the embedder's context window (``OllamaEmbedContextOverflow``) is permanent
too, but it is not a configuration fault, so it gets its own notice. Anything
that is
not an ``EmbedError`` (a database error, a bug) propagates unchanged.

Only the user-facing surfaces call this. ``brain eval`` deliberately does not:
its degraded-row semantics (``brain.eval.runner``) depend on an embed failure
staying visible, and ``tests/test_search_fallback_eval_untouched.py`` pins that.

Every string here is class-only (:func:`brain.errors.describe_failure`): an
exception message can carry text the user did not choose to print.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, fields
from time import perf_counter
from typing import Any

import psycopg

from .errors import EmbedError, describe_failure
from .ingest import Embedder
from .search import SearchDiagnostics, SearchResult

logger = logging.getLogger(__name__)

#: The value of the ``fallback`` JSON key when a search fell back.
FALLBACK_MODE = "fts_only"

#: A surface's search function: ``hybrid_search`` or a patch point wrapping it.
SearchFn = Callable[..., list[SearchResult]]


@dataclass(frozen=True)
class SearchFallback:
    """Why a search fell back: the failure's class, and whether a retry may help.

    ``context_overflow`` marks an ``OllamaEmbedContextOverflow``: the query
    itself is longer than ``BRAIN_OLLAMA_EMBED_NUM_CTX`` (the transport sends
    ``truncate: false``). It is never transient, and "check your configuration"
    would be the wrong advice for it.
    """

    reason: str
    transient: bool
    context_overflow: bool = False

    @classmethod
    def from_error(cls, exc: EmbedError) -> SearchFallback:
        """Build from the embed failure, keeping only content-free facts."""
        # Imported here, not at module top: ``brain.embeddings`` pulls httpx and
        # tiktoken, and this leaf is imported by ``brain.format_search`` (and so
        # by ``brain ui``), which otherwise never loads them. A failure is the
        # only time this runs.
        from .embeddings import OllamaEmbedContextOverflow

        return cls(
            reason=describe_failure(exc),
            transient=exc.transient,
            context_overflow=isinstance(exc, OllamaEmbedContextOverflow),
        )

    @property
    def note(self) -> str:
        """The one-line notice every surface shows (stderr, JSON, MCP)."""
        if self.context_overflow:
            return (
                f"query is longer than the embedder's context window "
                f"(BRAIN_OLLAMA_EMBED_NUM_CTX; {self.reason}); showing text-only "
                "(FTS) matches; shorten the query for semantic ranking."
            )
        if self.transient:
            return (
                f"embedding backend unavailable ({self.reason}); showing "
                "text-only (FTS) matches. Semantic ranking is off for this "
                "search and comes back when the embedder does."
            )
        return (
            f"embedding failed ({self.reason}); showing text-only (FTS) "
            "matches. This looks like a configuration problem (model not "
            "pulled? bad API key?) that a retry will not fix; run 'brain doctor'."
        )


@dataclass(frozen=True)
class FallbackSearch:
    """A search's results, and the fallback that produced them (``None`` if none)."""

    results: list[SearchResult]
    fallback: SearchFallback | None


def fallback_json(fallback: SearchFallback | None) -> dict[str, Any]:
    """Project ``fallback`` as four always-present, nullable JSON keys.

    Always present so a consumer reads a stable shape: ``null`` means the vector
    leg was attempted normally (or deliberately skipped with ``fts_only``), not
    that the key is missing. Shared by the CLI envelope and MCP so the two
    cannot describe the same fallback differently.
    """
    if fallback is None:
        return {
            "fallback": None,
            "fallback_reason": None,
            "fallback_transient": None,
            "fallback_note": None,
        }
    return {
        "fallback": FALLBACK_MODE,
        "fallback_reason": fallback.reason,
        "fallback_transient": fallback.transient,
        "fallback_note": fallback.note,
    }


def _reset(diagnostics: SearchDiagnostics) -> None:
    """Return every field to its default, so nothing from the failed attempt survives.

    Defaults are read off a FRESH instance rather than ``Field.default``, which
    is ``MISSING`` for a ``default_factory`` field.
    """
    fresh = type(diagnostics)()
    for field in fields(diagnostics):
        setattr(diagnostics, field.name, getattr(fresh, field.name))


def search_with_fts_fallback(
    search_fn: SearchFn,
    conn: psycopg.Connection[Any],
    *,
    embedder: Embedder,
    query: str,
    **kwargs: Any,
) -> FallbackSearch:
    """Run ``search_fn``; on an embed failure, run it again text-only.

    ``kwargs`` are passed through unchanged, except that the retry forces
    ``fts_only=True``. The retry reuses ``conn``: ``hybrid_search`` embeds the
    query in Python after its FTS leg, so a failed embed never aborts the
    transaction.

    A ``diagnostics`` holder in ``kwargs`` is reset before the retry, and its
    ``total_ms`` is then set to the wall clock of BOTH attempts. A failed embed
    is often a multi-second timeout, and a footer reading ``total 40ms`` would
    hide exactly the wait the user just sat through. ``embed_ms`` stays
    ``None``, so the existing ``fts_only`` meta key reads ``true``.

    Logged at INFO, not WARNING: the CLI configures no logging, so Python's
    last-resort handler would print a WARNING to stderr as an unformatted second
    copy of the surface's own notice. MCP logs at INFO by default.
    """
    started = perf_counter()
    try:
        results = search_fn(conn, embedder=embedder, query=query, **kwargs)
    except EmbedError as exc:
        logger.info(
            "query embed failed; searching text-only: %s", describe_failure(exc)
        )
        fallback = SearchFallback.from_error(exc)
    else:
        return FallbackSearch(results=results, fallback=None)

    diagnostics = kwargs.get("diagnostics")
    if isinstance(diagnostics, SearchDiagnostics):
        _reset(diagnostics)
    results = search_fn(
        conn, embedder=embedder, query=query, **{**kwargs, "fts_only": True}
    )
    if isinstance(diagnostics, SearchDiagnostics):
        diagnostics.total_ms = (perf_counter() - started) * 1000.0
    return FallbackSearch(results=results, fallback=fallback)
