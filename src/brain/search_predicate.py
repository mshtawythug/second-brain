"""Shared WHERE/JOIN construction for every leg of hybrid search."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def _ensure_utc(dt: datetime) -> datetime:
    """Stamp a naive datetime as UTC so ``timestamptz`` comparisons don't shift.

    ``--after 2026-01-01`` reaches the search layer as a *naive* midnight. Bound
    directly against a ``timestamptz`` column, Postgres interprets a naive
    literal in the **session** ``TimeZone``, shifting the boundary by the
    session's UTC offset — a doc sent at ``2026-01-01T03:00:00Z`` would fall
    *outside* ``--after 2026-01-01`` under an ``America/New_York`` session.
    Stamping UTC makes the boundary session-TZ-independent. Already-aware
    datetimes pass through unchanged. Mirrors the recency-boost idiom in
    :mod:`brain.search` (``recency_ts.replace(tzinfo=UTC)``).
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class SearchPredicate:
    """Immutable, fully-built SQL predicate shared by all search legs.

    ``where_sql`` is a parameterized fragment (``%s`` placeholders only — no
    user text is ever interpolated); ``where_params`` are its bound values in
    positional order. ``join_clause`` / ``fts_filter`` are the ONLY fields any
    caller may splice into SQL text with an f-string, and both are assembled
    exclusively from module-level literals plus ``%s`` — see
    :func:`build_predicate`.

    Existing as one object is what stops predicate drift: the FTS leg, the
    vector leg, the total-count query and the facet rollup all consume the
    same instance, so a filter can never apply to the ranked set but not to
    the number printed beside it.
    """

    where_sql: str  # e.g. "TRUE AND d.content_type = %s"
    where_params: tuple[Any, ...]  # immutable; callers splat into a list
    has_filters: bool  # where_sql != "TRUE"
    join_clause: str  # "" or "JOIN documents d ON d.id = c.document_id"
    fts_filter: str  # "" or f" AND {where_sql}"
    prepare_flag: bool | None  # True on the no-filter fast path, else None


def build_predicate(
    *,
    source_kind: str | None = None,
    tag: str | None = None,
    since_days: int | None = None,
    person_keys: list[str] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    content_type: str | None = None,
    thread_id: str | None = None,
    draft: bool | None = None,
    without_tag: str | None = None,
    updated_after: datetime | None = None,
    updated_before: datetime | None = None,
    sensitivity: str | None = None,
) -> SearchPredicate:
    """Build the shared metadata predicate for one search.

    Every parameter is keyword-only and defaults to ``None`` (= no filter), so
    a later wave can append a filter without touching any existing call site.

    All thirteen filters bind their value as a ``%s`` parameter; not one caller
    value is ever concatenated into ``where_sql``.

    ``sensitivity`` (F6) is deliberately OPT-IN and defaults to ``None``, meaning
    "both tiers" — the pre-026 behaviour. That default is the whole reason F6 can
    add a filter here without a ``tests/eval/baselines/ci.json`` re-record: with
    it, ``where_sql`` stays the literal ``"TRUE"``, so the no-filter fast path
    (no JOIN, prepared statement on) is bit-for-bit what it was and no ranked
    result set moves.

    Note what this filter is NOT. The local CLI is INSIDE the trust boundary by
    design, exactly as ``draft`` is: confidential documents are not hidden from
    an unfiltered ``brain search``. This exists so a user can ask "show me only
    what I've marked" — it is a lens, not an access control.
    """
    where_clauses = ["TRUE"]
    where_params: list[Any] = []
    if source_kind:
        where_clauses.append("d.source_id IN (SELECT id FROM sources WHERE kind=%s)")
        where_params.append(source_kind)
    if tag:
        where_clauses.append("%s = ANY(d.tags)")
        where_params.append(tag)
    if since_days:
        where_clauses.append("d.ingested_at >= NOW() - make_interval(days => %s)")
        where_params.append(since_days)
    if person_keys:
        # Case-insensitive overlap. ``documents.participants`` is written
        # by ingest extractors in source-preserved case (Gmail emits
        # ``"Alice Doe <alice@example.com>"``); the resolver's keys are
        # lowercased + expanded. A plain ``&&`` overlap would miss every
        # mixed-case stored value, so we unnest the array and lower each
        # element before comparing. Empty ``keys`` is "no filter" — the
        # resolver itself raises PersonNotFound on no match, so an empty
        # list here can only be a caller's explicit "no person filter"
        # intent.
        where_clauses.append(
            "EXISTS (SELECT 1 FROM unnest(d.participants) AS _p "
            "WHERE lower(_p) = ANY(%s::text[]))"
        )
        where_params.append(person_keys)
    # -- date-range block. Two independent axes, deliberately not merged:
    # -- ``after`` / ``before`` bind the DOCUMENT's own date, while
    # -- ``updated_after`` / ``updated_before`` (F9) bind the last time the
    # -- user's knowledge in it changed. For an email or a transcript
    # -- ``coalesce`` prefers ``sent_at``, so the edit dimension is
    # -- unreachable through the first pair at any layer. Both pairs are
    # -- inclusive lower / exclusive upper, so ``X..X`` returns nothing.
    if after is not None:
        where_clauses.append("coalesce(d.sent_at, d.ingested_at) >= %s")
        where_params.append(_ensure_utc(after))
    if before is not None:
        where_clauses.append("coalesce(d.sent_at, d.ingested_at) < %s")
        where_params.append(_ensure_utc(before))
    if updated_after is not None:
        where_clauses.append("d.updated_at >= %s")
        where_params.append(_ensure_utc(updated_after))
    if updated_before is not None:
        where_clauses.append("d.updated_at < %s")
        where_params.append(_ensure_utc(updated_before))
    if content_type is not None:
        where_clauses.append("d.content_type = %s")
        where_params.append(content_type)
    if thread_id is not None:
        where_clauses.append("d.thread_id = %s")
        where_params.append(thread_id)
    if draft is not None:
        where_clauses.append("d.draft = %s")
        where_params.append(draft)
    if without_tag is not None:
        where_clauses.append("NOT (%s = ANY(d.tags))")
        where_params.append(without_tag)
    if sensitivity is not None:
        where_clauses.append("d.sensitivity = %s")
        where_params.append(sensitivity)
    where_sql = " AND ".join(where_clauses)

    # No-filter fast path (perf F5 + F2). ``where_clauses`` always starts with
    # the literal ``"TRUE"``; every metadata filter appends a clause *and* a
    # param. So ``where_sql == "TRUE"`` (the common unfiltered CLI search)
    # means the ``documents`` JOIN supplies no column the FTS/vector legs
    # actually read — title/tags/source_kind/recency all come from the separate
    # ``doc_rows`` fetch — and the inner JOIN on the ``document_id`` FK can
    # neither drop nor duplicate chunk rows. We therefore (F5) omit the JOIN
    # and (F2) force psycopg to prepare the now-static SQL so an in-process /
    # MCP repeated search reuses the plan (~15 ms planning saved). The filtered
    # path keeps the JOIN and leaves ``prepare=None`` (psycopg's auto-prepare
    # heuristic) since each distinct filter combo is a different statement; a
    # one-shot CLI invocation prepares-then-executes once, a negligible no-op
    # risk.
    has_filters = where_sql != "TRUE"
    return SearchPredicate(
        where_sql=where_sql,
        where_params=tuple(where_params),
        has_filters=has_filters,
        join_clause=(
            "JOIN documents d ON d.id = c.document_id" if has_filters else ""
        ),
        fts_filter=f" AND {where_sql}" if has_filters else "",
        prepare_flag=None if has_filters else True,
    )
