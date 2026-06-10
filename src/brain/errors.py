"""Project-specific exception hierarchy.

Internal helpers that can fail in user-visible ways raise these exceptions so
the CLI and MCP server layers can map them to their respective frameworks
(``typer.Exit`` / ``McpError``) without sharing framework-specific imports.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


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


class AgeBootstrapError(BrainError):
    """Raised when Apache AGE session/graph bootstrap fails (wave G0).

    Wraps the raw ``psycopg.Error`` from ``LOAD 'age'`` / ``CREATE EXTENSION
    age`` / ``create_graph`` so the public ``brain.db`` bootstrap helpers never
    leak a framework-specific exception to the CLI / MCP layers (repo rule: a
    custom exception inheriting :class:`BrainError`). The originating
    ``psycopg.Error`` is preserved as ``__cause__`` (``raise ... from e``) for
    diagnostics. The autocommit precondition violation in
    :func:`brain.db.bootstrap_age` is a separate, plain :class:`BrainError`
    (caller bug, not a DB failure)."""


class GraphBackendError(BrainError):
    """Raised when an Apache AGE graph-backend operation fails (wave G0-4).

    Wraps the raw ``psycopg.Error`` from a generated Cypher / catalog call so
    the :mod:`brain.graph_rag.backends` layer never leaks a framework-specific
    exception (repo rule: a custom exception inheriting :class:`BrainError`).
    The originating ``psycopg.Error`` is preserved as ``__cause__``
    (``raise ... from e``) for diagnostics.

    Also raised for caller-side precondition violations surfaced before the DB
    round-trip — an invalid graph name, a non-positive traversal depth /
    frontier cap, an out-of-range edge-weight floor, a cross-tenant payload, or
    an unparseable ``agtype`` result. Those are caller bugs (analogous to the
    autocommit precondition in :func:`brain.db.bootstrap_age`), not DB
    failures, but share the type so callers can ``except GraphBackendError``
    once.
    """


class GraphReconcileError(BrainError):
    """Raised on a precondition failure in the GraphRAG reconcile layer (wave G1).

    Surfaced by :mod:`brain.graph_rag.reconcile` before any graph write when the
    caller asks to reconcile a document that does not exist (so there is no
    ``documents.content_hash`` to anchor the per-aspect ``graph_index_state``
    watermark). A caller bug — analogous to the cross-tenant payload guard in
    :class:`GraphBackendError` — so it fails fast rather than silently writing a
    half-formed graph. Inherits :class:`BrainError` so the CLI / MCP layers can
    map it without a framework-specific import.
    """


class CooccurrenceError(BrainError):
    """Raised on invalid co-occurrence inputs (wave G1, GraphRAG).

    Surfaced by :mod:`brain.graph_rag.cooccur` before any DB round-trip when a
    derive-time parameter is degenerate — a non-positive sliding window (no pair
    could ever co-occur) or a non-positive max-entities cap. These are caller
    bugs (a misconfigured ``BRAIN_GRAPH_COOCCUR_WINDOW`` /
    ``BRAIN_GRAPH_MAX_ENTITIES_PER_DOC``), so they fail fast rather than silently
    producing an empty / wrong contribution set.
    """


class WeightingError(BrainError):
    """Raised on edge-weight inputs that cannot yield a normalized lift (wave G1).

    Surfaced by :mod:`brain.graph_rag.weighting` when the supplied counts are
    impossible for a real co-occurrence — a co-document count below 1 (no edge),
    a marginal document count below 1, a co-document count exceeding the rarer
    endpoint's marginal (you cannot co-occur in more documents than the rarer
    entity appears in), or a generic-document-frequency ratio outside ``(0, 1]``.
    The normalized lift is provably in ``(0, 1]`` once these are satisfied, so
    the DB ``CHECK (weight > 0 AND weight <= 1)`` can never be violated.
    """


class GraphTenantError(BrainError):
    """Raised when a graph operation resolves to an empty ``tenant_id`` (wave G2).

    GraphRAG is multi-tenant (spec §9 D9): every relational source-of-truth row,
    AGE vertex/edge property, and generated query is scoped by ``tenant_id``,
    which the schema declares ``TEXT NOT NULL``.
    :func:`brain.graph_rag.tenancy.resolve_tenant` raises this before any DB
    round-trip when neither an explicit ``--tenant`` override nor the configured
    ``BRAIN_GRAPH_TENANT`` default yields a non-empty id. A caller bug (analogous
    to the cross-tenant payload guard on :class:`GraphBackendError`), so it fails
    fast rather than scoping a query to an empty tenant.
    """


class GroupingError(BrainError):
    """Raised on invalid scoped-subgraph grouping parameters (wave G2).

    Surfaced by :mod:`brain.graph_rag.grouping` before any work when a
    grouping knob is degenerate — a ``min_edge_weight`` / ``bridge_keep_weight``
    outside ``[0.0, 1.0]`` or a non-positive ``theme_limit``. These are caller
    bugs (a misconfigured ``BRAIN_GRAPH_MIN_EDGE_WEIGHT`` /
    ``BRAIN_GRAPH_THEME_LIMIT``), so they fail fast rather than silently
    producing an empty / wrong theme set. The grouping itself is pure logic,
    never touching the DB, so this is the only failure mode.
    """


class GraphModeUnavailable(BrainError):
    """Raised when an explicit graph retrieval mode is not available in this wave.

    Specifically, an explicit ``--mode global`` (CLI) / ``mode='global'`` (MCP)
    request: global community-summary retrieval lands in G3, so the G2 core
    **REJECTS** it (never degrades — only the *auto* router degrades
    global→local; spec §17b decision 4). Raised by
    :func:`brain.graph_rag.router.route` (and surfaced through
    :func:`brain.graph_rag.retrieve.graph_rag_search`). The CLI maps it to
    ``typer.BadParameter`` (exit 2) and the MCP server to
    ``McpError(INVALID_PARAMS, ...)`` (waves G2-h/i). Inherits
    :class:`BrainError` so those layers map it without a framework-specific
    import.
    """


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


class ElicitError(BrainError):
    """Base class for tacit-knowledge elicitation failures."""


class ConnectError(BrainError):
    """Raised on invalid inputs in the ``brain connect`` auto-link layer (Plan 07).

    Surfaced by :mod:`brain.connect` before any DB round-trip when a scoring /
    refresh parameter is degenerate — a non-positive candidate limit or
    per-doc cap, or a confidence threshold outside ``(0.0, 1.0]``. These are
    caller bugs (a misconfigured ``BRAIN_CONNECT_*`` knob), so they fail fast
    rather than silently producing an empty / wrong suggestion set. Also raised
    when a suggestion-id prefix cannot be resolved to a single
    ``link_suggestions`` row. Inherits :class:`BrainError` so the CLI / MCP
    layers map it without a framework-specific import.
    """


class VaultNoteSyncError(BrainError):
    """Raised when authoring a vault note fails to resolve or index.

    Carries the per-file ``(path, reason)`` pairs (the same shape as
    :class:`~brain.vault.sync.SyncReport.errors`) so the CLI can print each
    one and exit non-zero — preserving ``brain note new``'s historical
    behavior — while a library caller (the elicit session loop) can inspect
    ``.errors`` programmatically.
    """

    def __init__(self, errors: Sequence[tuple[Path, str]]) -> None:
        joined = "; ".join(f"{path}: {reason}" for path, reason in errors)
        super().__init__(f"vault note sync failed: {joined}")
        self.errors: list[tuple[Path, str]] = list(errors)


class EnrichmentError(BrainError):
    """Raised when a per-document enrichment call fails unrecoverably.

    "Unrecoverable" means the caller must NOT retry within this transaction
    (e.g., the model returned malformed JSON twice in a row). The Q1-D
    post-ingest hook catches this, logs a warning, and lets the ingest
    commit with ``documents.summary`` still NULL — ``brain enrich --backfill``
    can pick the row up later.
    """


class OllamaUnavailable(EnrichmentError):
    """The Ollama server is unreachable / returned a connection error / 5xx.

    Distinct subclass of :class:`EnrichmentError` so the ingest hook can
    ``except OllamaUnavailable`` specifically — the message it logs guides
    the user to ``brain enrich --backfill`` once Ollama is back, while the
    ``brain enrich --backfill`` CLI surfaces it as a clear "is Ollama
    running?" error on the first row.
    """


class IngestAmbiguousSource(BrainError):
    """Raised when multiple documents share a single ``(source_kind, source_external_id)`` key.

    This normally cannot happen — ``sources(kind, external_id)`` is UNIQUE
    (migration 001), so one source row maps to exactly one document for
    Krisp/Slack stdin ingests. The edge case arises when ``brain rm`` deletes
    the document row but leaves the orphaned ``sources`` row behind, and two
    concurrent ingests then both INSERT against that orphaned row. The result
    is two ``documents`` rows sharing one source; the next re-ingest via
    ``(kind, external_id)`` raises this error so the user can resolve the
    duplicate manually rather than having ``--force`` silently pick one.
    """


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

