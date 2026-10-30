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
from .search import CANDIDATE_LIMIT, SearchResult, hybrid_search
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


@dataclass(frozen=True)
class Matcher:
    """What a check needs besides the connection.

    ``embedder`` is called only for a semantic watch, so a lexical check never builds one.
    ``search_fn`` is ``hybrid_search``; a test wraps it.
    """

    embedder: Callable[[], Embedder]
    vector_sim_floor: float
    min_cosine: float
    search_fn: SearchFn = hybrid_search


@dataclass(frozen=True)
class WatchMatch:
    """A document a check or peek found new for a watch."""

    document_id: str
    title: str
    source_kind: str | None


@dataclass(frozen=True)
class CheckResult:
    """One watch's check. ``started_at`` is ``None`` for a peek, which records nothing."""

    watch: str
    mode: str
    matches: list[WatchMatch]
    skipped: str | None = None
    started_at: datetime | None = None


def build_matcher(
    cfg: Config,
    *,
    embedder: Callable[[], Embedder] | None = None,
    env: Mapping[str, str] | None = None,
) -> Matcher:
    """The matcher every surface uses: the config's floor, the watch knob, a lazy embedder.

    ``embedder`` defaults to :func:`brain.embeddings.make_embedder` of ``cfg``; whichever it is,
    it is called at most once, and only if a semantic watch is checked. ``env`` defaults to
    ``os.environ`` (after ``Config.load()``, spec §2.3).
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

    return Matcher(
        embedder=_embedder,
        vector_sim_floor=cfg.vector_sim_floor,
        min_cosine=load_watch_settings(os.environ if env is None else env).min_cosine,
    )


class _Skip(Exception):
    """Internal: this watch cannot be checked now; carries the brief's wording."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _null_embedder() -> Embedder:
    from .embeddings import NullEmbedder

    return NullEmbedder()


def _search(
    conn: psycopg.Connection[Any],
    watch: SavedSearch,
    matcher: Matcher,
    *,
    updated_after: datetime | None,
    sensitivity: str | None = None,
) -> list[SearchResult]:
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
    outcome = run_search(
        matcher.search_fn,
        conn,
        embedder=embedder,
        query=watch.query,
        limit=CHECK_LIMIT,
        stages=(),
        fts_only=not semantic,
        explain=semantic,
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
        return outcome.results
    if outcome.fallback is not None:
        raise _Skip(SKIP_EMBEDDER)
    return [
        result
        for result in outcome.results
        if result.explain is not None
        and result.explain.vector_cosine is not None
        and result.explain.vector_cosine >= matcher.min_cosine
    ]


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
    notified: bool,
) -> set[str]:
    """Insert the hits that are new; return their document ids."""
    if not document_ids:
        return set()
    rows = conn.execute(
        """
        INSERT INTO saved_search_hits (saved_search_id, document_id, notified_at)
        SELECT %s, doc, CASE WHEN %s THEN NOW() END FROM unnest(%s::uuid[]) AS doc
        ON CONFLICT (saved_search_id, document_id) DO NOTHING
        RETURNING document_id::text
        """,
        (watch_id, notified, document_ids),
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
        ids = list(dict.fromkeys(r.document_id for r in [*current, *window]))
        seeded = _record(conn, watch.id, ids, notified=True)
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
            results = _search(
                conn, watch, matcher, updated_after=watch.last_checked_at - WINDOW_OVERLAP
            )
        except _Skip as exc:
            return CheckResult(
                watch=watch.name,
                mode=watch.mode,
                matches=[],
                skipped=exc.reason,
                started_at=started_at,
            )
        new = _record(conn, watch.id, [r.document_id for r in results], notified=False)
        conn.execute(
            "UPDATE saved_searches SET last_checked_at = GREATEST(last_checked_at, %s) "
            "WHERE id = %s",
            (started_at, watch.id),
        )
    return CheckResult(
        watch=watch.name,
        mode=watch.mode,
        matches=_matches(results, new),
        started_at=started_at,
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
        results = _search(
            conn,
            watch,
            matcher,
            updated_after=watch.last_checked_at - WINDOW_OVERLAP,
            sensitivity=sensitivity,
        )
    except _Skip as exc:
        return CheckResult(watch=watch.name, mode=watch.mode, matches=[], skipped=exc.reason)
    ids = [r.document_id for r in results]
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
        matches=_matches(results, set(ids) - known),
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
    "peek_enabled",
    "peek_watch",
]
