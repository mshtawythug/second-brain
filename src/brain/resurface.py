"""Spaced-repetition resurfacing: score older, unreviewed docs for revisit.

Pure on-the-fly scoring over existing signals (Plan 02) -- no new table, no
state drift. Each document is scored from three signals already in the schema:

1. **Age** -- ``coalesce(documents.sent_at, documents.ingested_at)``.
2. **Last access** -- ``MAX(interactions.at)`` over ``opened`` / ``clicked``
   rows (``NULL`` => never reviewed).
3. **Importance** -- tag count + summary presence.

``resurface_docs`` runs one read-only SQL query, computes ages in the DB (so
doc timestamps, last-access timestamps, and "now" share one clock), then scores
and sorts in Python. ``score_document`` is the pure-logic core, isolated so it
can be unit-tested without a database.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

from .config import Config

# Content types never worth resurfacing: action-item digests are operational,
# not knowledge to revisit. Excluded by the SQL guard.
_EXCLUDED_CONTENT_TYPES: tuple[str, ...] = ("krisp_action_items",)

# Length of the body preview returned with each item. Matches the search
# snippet convention; never emits the full body (privacy -- spec §6).
_SNIPPET_CHARS = 200


@dataclass(frozen=True)
class ResurfaceItem:
    """One scored document due for review.

    ``last_access_days`` is ``None`` when the document has never been opened or
    clicked (treated as "never reviewed" by the scorer). ``snippet`` is the
    first :data:`_SNIPPET_CHARS` characters of the body -- the full ``content``
    column is never emitted.
    """

    id: str
    title: str
    source_kind: str | None
    content_type: str
    tags: list[str]
    age_days: float
    last_access_days: float | None
    score: float
    snippet: str


def score_document(
    *,
    age_days: float,
    last_access_days: float | None,
    tag_count: int | None,
    has_summary: bool,
    age_halflife_days: float,
    access_halflife_days: float,
) -> float:
    """Compute the resurface score for one document.

    ``resurface_score = age_factor * access_staleness * importance_factor`` where

    - ``age_factor = 1 - 0.5^(age_days / age_halflife_days)`` -- the inverted
      ``search.py`` recency decay, so older docs score higher (saturates toward
      1.0 well beyond the half-life).
    - ``access_staleness = 1 - 0.5^(effective_access / access_halflife_days)``
      where ``effective_access`` is ``last_access_days`` when the doc has been
      opened, else ``age_days`` (a never-opened doc is treated as stale for its
      whole life). A doc opened today scores near 0; never-opened scores near 1.
    - ``importance_factor = 1.0 + 0.1*tag_count + 0.2*has_summary`` -- a mild
      additive boost for richer docs. No LLM call.

    All time inputs are in days. ``tag_count`` may be ``None`` (treated as 0).
    The half-lives are required positive floats (validated upstream by
    :class:`brain.config.Config`); this function does not re-validate them.
    """
    effective_access = last_access_days if last_access_days is not None else age_days
    age_factor = 1.0 - 0.5 ** (age_days / age_halflife_days)
    access_staleness = 1.0 - 0.5 ** (effective_access / access_halflife_days)
    importance_factor = 1.0 + 0.1 * (tag_count or 0) + (0.2 if has_summary else 0.0)
    return float(age_factor * access_staleness * importance_factor)


def resurface_docs(
    conn: psycopg.Connection[Any],
    *,
    cfg: Config,
    limit: int | None = None,
    min_age_days: int | None = None,
    source_kind: str | None = None,
) -> list[ResurfaceItem]:
    """Return the top scored documents due for review, highest score first.

    Pulls every non-draft, non-action-item document older than
    ``min_age_days`` (default :attr:`Config.resurface_min_age_days`), computes
    each document's age and last-access age in the DB, scores them with
    :func:`score_document`, and returns the top ``limit``
    (default :attr:`Config.resurface_limit`) by descending score. Ties break on
    older-first then id, for a deterministic order.

    Args:
        conn: Live Postgres connection (read-only query).
        cfg: Active config -- supplies the limit / min-age / half-life defaults.
        limit: Max items to return; ``None`` uses ``cfg.resurface_limit``.
        min_age_days: Exclude docs younger than this; ``None`` uses
            ``cfg.resurface_min_age_days``.
        source_kind: Optional ``sources.kind`` filter (e.g. ``"manual"``).

    Returns:
        Up to ``limit`` :class:`ResurfaceItem` rows, descending score. Empty
        when the corpus is empty or every doc is younger than ``min_age_days``.
    """
    effective_limit = cfg.resurface_limit if limit is None else limit
    effective_min_age = (
        cfg.resurface_min_age_days if min_age_days is None else min_age_days
    )
    # Validate the explicit override path. ``cfg`` values are already validated
    # by Config.load(), but a direct CLI/MCP/API caller can still pass a bad
    # value — and a bad one is silently wrong, not loud: limit=0 returns
    # nothing, limit<0 slices "all but the last N" via ``items[:limit]``, and a
    # negative min_age shifts the cutoff into the future (admitting brand-new /
    # future-dated docs). Reject both with a clear ValueError; the CLI maps it
    # to BadParameter and the MCP tool to INVALID_PARAMS.
    if effective_limit < 1:
        raise ValueError(f"limit must be an integer >= 1 (got {effective_limit})")
    if effective_min_age < 0:
        raise ValueError(
            f"min_age_days must be a non-negative integer (got {effective_min_age})"
        )

    # WHERE composition mirrors queries.list_documents: static SQL fragments
    # joined with " AND ", every user value bound via a %s placeholder.
    where = [
        "d.draft IS NOT TRUE",
        "d.content_type <> ALL(%s)",
        "coalesce(d.sent_at, d.ingested_at) IS NOT NULL",
        "coalesce(d.sent_at, d.ingested_at) < now() - make_interval(days => %s)",
    ]
    params: list[Any] = [list(_EXCLUDED_CONTENT_TYPES), effective_min_age]
    if source_kind:
        where.append("s.kind = %s")
        params.append(source_kind)

    query = sql.SQL(
        """
        SELECT
            d.id::text,
            d.title,
            d.content_type,
            d.tags,
            s.kind AS source_kind,
            left(d.content, {snippet_chars}) AS snippet,
            array_length(d.tags, 1) AS tag_count,
            (d.summary IS NOT NULL) AS has_summary,
            EXTRACT(
                EPOCH FROM (now() - coalesce(d.sent_at, d.ingested_at))
            ) / 86400.0 AS age_days,
            EXTRACT(EPOCH FROM (now() - MAX(i.at))) / 86400.0 AS last_access_days
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        LEFT JOIN interactions i
            ON i.document_id = d.id
            AND i.action IN ('opened', 'clicked')
        WHERE {where}
        GROUP BY d.id, s.kind
        """
    ).format(
        snippet_chars=sql.Literal(_SNIPPET_CHARS),
        where=sql.SQL(" AND ").join(sql.SQL(w) for w in where),
    )

    rows = conn.execute(query, params).fetchall()

    items: list[ResurfaceItem] = []
    for r in rows:
        age_days = float(r[8])
        last_access_days = float(r[9]) if r[9] is not None else None
        tag_count = int(r[6]) if r[6] is not None else None
        has_summary = bool(r[7])
        score = score_document(
            age_days=age_days,
            last_access_days=last_access_days,
            tag_count=tag_count,
            has_summary=has_summary,
            age_halflife_days=cfg.resurface_age_halflife_days,
            access_halflife_days=cfg.resurface_access_halflife_days,
        )
        items.append(
            ResurfaceItem(
                id=r[0],
                title=r[1],
                source_kind=r[4],
                content_type=r[2],
                tags=list(r[3] or []),
                age_days=age_days,
                last_access_days=last_access_days,
                score=score,
                snippet=r[5] or "",
            )
        )

    # Highest score first; ties resolve to older-first then id for determinism.
    items.sort(key=lambda it: (-it.score, -it.age_days, it.id))
    return items[:effective_limit]
