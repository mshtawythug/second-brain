"""Matching a watch: seeding it, checking it, and peeking at it (RM-15).

A check is one search through :func:`brain.search_pipeline.run_search` with the watch's saved
filters plus ``updated_after = last_checked_at - 1 h`` (the F9 edit-time filter): ``updated_at``
moves on insert and on a real edit, never on maintenance (migration 025), so the window is
exactly "new or changed knowledge". Without it, new documents would compete with every old
match for the search's 50 candidate slots. The hour of overlap covers an ingest whose
transaction started before the previous check and committed after it.

**One check is one transaction** (:func:`check_watch`): lock the watch row (``FOR UPDATE``, so a
manual ``brain watch check`` and the 07:00 brief serialize), search, record with
``INSERT ... ON CONFLICT DO NOTHING RETURNING`` (the returned ids ARE the new hits, so a racing
check cannot report one twice), then ``last_checked_at = GREATEST(last_checked_at, start)`` with
the transaction's start time, so it never moves backwards. The caller passes an autocommit
connection, or one with no transaction open, so the lock is released when the check returns.

**Lexical** watches (the default) search full-text only: deterministic, and they work with the
embedder down. **Semantic** watches run the hybrid search with ``explain=True`` (the only way
``vector_cosine`` is filled) and keep a result whose cosine reaches
``BRAIN_WATCH_MIN_COSINE``. A semantic check that cannot embed (the search fell back to
full-text, or the backend is ``none``) records nothing and leaves ``last_checked_at`` alone, so
the next check retries the same window. So does a check whose saved ``person`` no longer
resolves.

A check runs no ranking stages (``stages=()``): a watch asks whether a document matches, which a
reranker cannot change.
"""
from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from .config import Config, ConfigError
from .errors import BrainError, PersonAmbiguous, PersonNotFound
from .ingest import Embedder
from .queries import resolve_person_to_keys
from .saved_search import (
    MODE_SEMANTIC,
    SavedSearch,
    SavedSearchError,
    SavedSearchNotFound,
    WatchFilters,
    create_watch,
    get_watch,
    list_watches,
    lock_watch,
)
from .search import CANDIDATE_LIMIT, SearchDiagnostics, SearchResult, hybrid_search
from .search_fallback import SearchFn
from .search_pipeline import run_search
from .source_kinds import validate_source_kind
from .tags import normalize_tag

#: How far before ``last_checked_at`` a check's window starts (the ticket's hour).
WINDOW_OVERLAP = timedelta(hours=1)
#: Rows a check fetches: each ranking leg's own candidate cap, so asking for more finds nothing.
CHECK_LIMIT = CANDIDATE_LIMIT

#: Why a check was skipped, as the brief prints it after the watch name.
SKIP_EMBEDDER = "skipped, embedder unavailable"
SKIP_PERSON = "skipped, its person filter matches no one"
#: The knob is read only for a semantic watch, so a bad value costs that watch, never the brief.
SKIP_MIN_COSINE = "skipped, BRAIN_WATCH_MIN_COSINE is invalid"


@dataclass(frozen=True)
class Matcher:
    """What a check needs besides the connection.

    ``embedder`` is called only for a semantic watch, so a lexical check never builds one.
    ``min_cosine`` is a number, or a callable read only when a semantic watch is checked (so a
    bad ``BRAIN_WATCH_MIN_COSINE`` skips semantic watches and nothing else). ``search_fn`` is
    ``hybrid_search``; a test wraps it.
    """

    embedder: Callable[[], Embedder]
    vector_sim_floor: float
    min_cosine: float | Callable[[], float]
    search_fn: SearchFn = hybrid_search


@dataclass(frozen=True)
class WatchMatch:
    """A document a check or peek found new for a watch."""

    document_id: str
    title: str
    source_kind: str | None


@dataclass(frozen=True)
class CheckResult:
    """One watch's check. ``started_at`` is ``None`` for a peek, which records nothing.

    A check fetches at most :data:`CHECK_LIMIT` documents, so a bigger change in one window
    (a bulk import) cannot all be recorded. ``unrecorded`` is how many more documents matched
    a LEXICAL watch than the check fetched (an exact count); ``capped`` says a SEMANTIC check
    filled the limit, so more may exist (the vector leg has no total). Either way the rest are
    never recorded: the window moves on, and :func:`limit_note` tells the owner.
    """

    watch: str
    mode: str
    matches: list[WatchMatch]
    skipped: str | None = None
    started_at: datetime | None = None
    query: str = ""
    unrecorded: int = 0
    capped: bool = False


@dataclass(frozen=True)
class _Found:
    results: list[SearchResult]
    unrecorded: int = 0
    capped: bool = False


def build_matcher(
    cfg: Config,
    *,
    embedder: Callable[[], Embedder] | None = None,
    env: Mapping[str, str] | None = None,
) -> Matcher:
    """The matcher every surface uses: the config's floor, the watch knob, a lazy embedder.

    ``embedder`` defaults to :func:`brain.embeddings.make_embedder` of ``cfg``; whichever it is,
    it is called at most once, and only if a semantic watch is checked. The knob is read from
    ``env`` (default ``os.environ``, after ``Config.load()``, spec §2.3) only then too, so
    building a matcher never raises.
    """
    from .settings.watch import load_watch_settings

    built: list[Embedder] = []

    def _embedder() -> Embedder:
        if not built:
            if embedder is not None:
                built.append(embedder())
            else:
                from .embeddings import make_embedder

                built.append(make_embedder(cfg))
        return built[0]

    def _min_cosine() -> float:
        return load_watch_settings(os.environ if env is None else env).min_cosine

    return Matcher(
        embedder=_embedder,
        vector_sim_floor=cfg.vector_sim_floor,
        min_cosine=_min_cosine,
    )


def limit_note(result: CheckResult, *, with_count: bool) -> str | None:
    """The brief's and ``brain watch check``'s line for a check that hit :data:`CHECK_LIMIT`.

    ``with_count=False`` is for the published page: the count is taken over both tiers, so it
    would say that withheld documents exist (the membership oracle F6 avoids).
    """
    if result.unrecorded:
        more = f"{result.unrecorded} more" if with_count else "more"
        return (
            f"{more} matched than a check can record "
            f"(`brain search {shlex.quote(result.query)}` lists them)"
        )
    if result.capped:
        return f"{CHECK_LIMIT} checked, more may exist"
    return None


class _Skip(Exception):
    """Internal: this watch cannot be checked now; carries the brief's wording."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _null_embedder() -> Embedder:
    from .embeddings import NullEmbedder

    return NullEmbedder()


def _cosine_floor(matcher: Matcher) -> float:
    if not callable(matcher.min_cosine):
        return matcher.min_cosine
    try:
        return matcher.min_cosine()
    except ConfigError as exc:
        raise _Skip(SKIP_MIN_COSINE) from exc


def _search(
    conn: psycopg.Connection[Any],
    watch: SavedSearch,
    matcher: Matcher,
    *,
    updated_after: datetime | None,
    sensitivity: str | None = None,
) -> _Found:
    """The watch's matches, best first; raises :class:`_Skip` when it cannot be checked."""
    filters = watch.filters
    person = None
    if filters.person is not None:
        try:
            person = resolve_person_to_keys(conn, filters.person)
        except (PersonNotFound, PersonAmbiguous) as exc:
            raise _Skip(SKIP_PERSON) from exc
    semantic = watch.mode == MODE_SEMANTIC
    if semantic:
        floor = _cosine_floor(matcher)
        try:
            embedder = matcher.embedder()
        # Building the embedder can refuse its configuration (``ConfigError``) or its backend
        # (``BrainError``); either way this watch cannot be checked now, and the brief must
        # still render.
        except (BrainError, ConfigError) as exc:
            raise _Skip(SKIP_EMBEDDER) from exc
        if not getattr(embedder, "produces_embeddings", True):
            raise _Skip(SKIP_EMBEDDER)
    else:
        embedder = _null_embedder()
    # The lexical total is exact and uncapped; the vector leg has none (``SearchDiagnostics``).
    diagnostics = SearchDiagnostics()
    outcome = run_search(
        matcher.search_fn,
        conn,
        embedder=embedder,
        query=watch.query,
        limit=CHECK_LIMIT,
        stages=(),
        fts_only=not semantic,
        explain=semantic,
        diagnostics=diagnostics,
        total_count=not semantic,
        vector_sim_floor=matcher.vector_sim_floor,
        source_kind=filters.source,
        tag=filters.tag,
        person_keys=person.keys if person else None,
        person_display_name=person.display_name if person else None,
        content_type=filters.kind,
        without_tag=filters.without_tag,
        updated_after=updated_after,
        sensitivity=sensitivity,
    )
    if not semantic:
        total = diagnostics.total_documents or 0
        return _Found(outcome.results, unrecorded=max(0, total - len(outcome.results)))
    if outcome.fallback is not None:
        raise _Skip(SKIP_EMBEDDER)
    kept = [
        result
        for result in outcome.results
        if result.explain is not None
        and result.explain.vector_cosine is not None
        and result.explain.vector_cosine >= floor
    ]
    return _Found(kept, capped=len(outcome.results) >= CHECK_LIMIT)


def _matches(results: list[SearchResult], keep: set[str] | None = None) -> list[WatchMatch]:
    return [
        WatchMatch(document_id=r.document_id, title=r.title, source_kind=r.source_kind)
        for r in results
        if keep is None or r.document_id in keep
    ]


def _record(
    conn: psycopg.Connection[Any],
    watch_id: str,
    document_ids: list[str],
    *,
    seed: bool,
) -> set[str]:
    """Insert the hits that are new; return their document ids.

    ``seed=True`` writes SEEDS (``add``'s current matches): notified, and ``seeded``, so the
    terminal reads leave them out (:data:`brain.saved_search.NOT_SEED_SQL`).
    """
    if not document_ids:
        return set()
    rows = conn.execute(
        """
        INSERT INTO saved_search_hits (saved_search_id, document_id, notified_at, seeded)
        SELECT %s, doc, CASE WHEN %s THEN NOW() END, %s FROM unnest(%s::uuid[]) AS doc
        ON CONFLICT (saved_search_id, document_id) DO NOTHING
        RETURNING document_id::text
        """,
        (watch_id, seed, seed, document_ids),
    ).fetchall()
    return {row[0] for row in rows}


def add_watch(
    conn: psycopg.Connection[Any],
    *,
    name: str,
    query: str,
    filters: WatchFilters,
    mode: str,
    matcher: Matcher,
) -> tuple[SavedSearch, int]:
    """Validate, create and seed a watch in one transaction; return it and how many it seeded.

    The filters are validated the way ``brain search`` validates them: the source kind
    (:func:`~brain.source_kinds.validate_source_kind`) and the person
    (:func:`~brain.queries.resolve_person_to_keys`); tags are normalised as stored tags are. The
    seed is every current match (the top :data:`CHECK_LIMIT`) plus every match in the first
    check's window, marked notified, so the first brief is not a flood and an immediate check
    reports nothing.

    Raises:
        SavedSearchError: a bad name, query or mode, a taken name, or a semantic watch whose seed
            search cannot embed.
        InvalidSourceKind, PersonNotFound, PersonAmbiguous: a filter the caller must fix.
    """
    if filters.source is not None:
        validate_source_kind(filters.source)
    filters = WatchFilters(
        source=filters.source,
        tag=normalize_tag(filters.tag) if filters.tag is not None else None,
        person=filters.person,
        kind=filters.kind,
        without_tag=(
            normalize_tag(filters.without_tag) if filters.without_tag is not None else None
        ),
    )
    with conn.transaction():
        if filters.person is not None:
            resolve_person_to_keys(conn, filters.person)
        watch = create_watch(conn, name=name, query=query, filters=filters, mode=mode)
        try:
            current = _search(conn, watch, matcher, updated_after=None)
            window = _search(
                conn,
                watch,
                matcher,
                updated_after=watch.last_checked_at - WINDOW_OVERLAP,
            )
        except _Skip as exc:
            raise SavedSearchError(
                f"cannot seed watch {watch.name!r} ({exc.reason}); nothing was saved"
            ) from exc
        ids = list(dict.fromkeys(r.document_id for r in [*current.results, *window.results]))
        seeded = _record(conn, watch.id, ids, seed=True)
    return watch, len(seeded)


def check_watch(conn: psycopg.Connection[Any], name: str, matcher: Matcher) -> CheckResult:
    """Check one watch and record its new hits (one transaction; see the module docstring).

    Runs even when the watch is paused: naming it is an explicit request.

    Raises:
        SavedSearchNotFound: no watch has this name (or it was removed meanwhile).
    """
    with conn.transaction():
        watch, started_at = lock_watch(conn, name)
        try:
            found = _search(
                conn, watch, matcher, updated_after=watch.last_checked_at - WINDOW_OVERLAP
            )
        except _Skip as exc:
            return CheckResult(
                watch=watch.name,
                mode=watch.mode,
                matches=[],
                skipped=exc.reason,
                started_at=started_at,
                query=watch.query,
            )
        new = _record(conn, watch.id, [r.document_id for r in found.results], seed=False)
        conn.execute(
            "UPDATE saved_searches SET last_checked_at = GREATEST(last_checked_at, %s) "
            "WHERE id = %s",
            (started_at, watch.id),
        )
    return CheckResult(
        watch=watch.name,
        mode=watch.mode,
        matches=_matches(found.results, new),
        started_at=started_at,
        query=watch.query,
        unrecorded=found.unrecorded,
        capped=found.capped,
    )


def peek_watch(
    conn: psycopg.Connection[Any],
    name: str,
    matcher: Matcher,
    *,
    sensitivity: str | None = None,
) -> CheckResult:
    """What a check would report now, without recording anything or moving the window.

    ``sensitivity`` is the F6 lens in the search predicate (MCP passes ``"normal"``).
    """
    watch = get_watch(conn, name)
    try:
        found = _search(
            conn,
            watch,
            matcher,
            updated_after=watch.last_checked_at - WINDOW_OVERLAP,
            sensitivity=sensitivity,
        )
    except _Skip as exc:
        return CheckResult(
            watch=watch.name, mode=watch.mode, matches=[], skipped=exc.reason, query=watch.query
        )
    ids = [r.document_id for r in found.results]
    known = {
        row[0]
        for row in conn.execute(
            "SELECT document_id::text FROM saved_search_hits "
            "WHERE saved_search_id = %s AND document_id = ANY(%s::uuid[])",
            (watch.id, ids),
        ).fetchall()
    }
    return CheckResult(
        watch=watch.name,
        mode=watch.mode,
        matches=_matches(found.results, set(ids) - known),
        query=watch.query,
        unrecorded=found.unrecorded,
        capped=found.capped,
    )


def check_enabled(conn: psycopg.Connection[Any], matcher: Matcher) -> list[CheckResult]:
    """Check every enabled watch, by name. A watch removed meanwhile is passed over."""
    results: list[CheckResult] = []
    for watch in list_watches(conn, enabled_only=True):
        try:
            results.append(check_watch(conn, watch.name, matcher))
        except SavedSearchNotFound:
            continue
    return results


def peek_enabled(
    conn: psycopg.Connection[Any], matcher: Matcher, *, sensitivity: str | None = None
) -> list[CheckResult]:
    """Peek at every enabled watch, by name."""
    return [
        peek_watch(conn, watch.name, matcher, sensitivity=sensitivity)
        for watch in list_watches(conn, enabled_only=True)
    ]


__all__ = [
    "CHECK_LIMIT",
    "SKIP_EMBEDDER",
    "SKIP_MIN_COSINE",
    "SKIP_PERSON",
    "WINDOW_OVERLAP",
    "CheckResult",
    "Matcher",
    "SavedSearchError",
    "WatchMatch",
    "add_watch",
    "build_matcher",
    "check_enabled",
    "check_watch",
    "limit_note",
    "peek_enabled",
    "peek_watch",
]
