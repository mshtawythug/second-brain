"""Temporal evolution of a theme/entity — backs ``brain timeline`` (Plan 05).

Buckets entity mentions by document date to show how a theme or entity rose or
fell over time, with per-bucket co-topics, representative doc titles, and an
optional best-effort Ollama synthesis line.

Single reason to change: the temporal bucketing algorithm. Entity resolution,
person scoping, co-topic aggregation, and bucket formatting all live here; the
CLI (`brain.cli`) and MCP (`brain.mcp_server`) only orchestrate.

**Relational-only — no Apache AGE / Cypher.** The query joins the migration-012
relational source-of-truth tables (``graph_entity_mentions``,
``graph_edge_contributions``, ``graph_entities``) against ``documents``; these
are pure-SQL tables present even on the stock pgvector image. The temporal
anchor is ``COALESCE(documents.sent_at, documents.ingested_at)`` (event time
when known — emails / Krisp — else ingest time), computed inline; when the
optional migration-021 generated ``doc_date`` column is present it is used
instead (auto-detected via ``information_schema``).
"""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg

from .config import (
    _VALID_TIMELINE_GRANULARITIES,
    _VALID_TIMELINE_TRIMS,
    Config,
)
from .graph_rag.tenancy import resolve_tenant
from .queries import resolve_person_to_keys

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher

_logger = logging.getLogger(__name__)

# Number of co-topic entities surfaced per bucket (spec §3f step 3).
_COTOPIC_LIMIT = 3
# Entity-resolution fan-out cap — a free-text query that ILIKE-matches more than
# this many entities is almost certainly too broad; mirrors the
# ``relational.list_entities`` default LIMIT (spec §3f).
_ENTITY_RESOLVE_LIMIT = 50
# Auto-granularity: the finest of {month, quarter, year} that yields at least
# this many non-empty buckets wins; if none clears the bar, fall back to month
# (so a young/sparse corpus still shows the finest-grained view available).
_AUTO_MIN_BUCKETS = 3
# Token budget for the per-bucket DOCUMENT SUMMARIES bundle fed to the synthesis
# LLM. Caps the prompt so it stays fast on a local 8B model; the bundle is the
# grounding evidence, so a few hundred tokens of summaries is plenty.
_SYNTH_DOC_SUMMARY_BUDGET_TOKENS = 1200


@dataclass(frozen=True)
class EntityRow:
    """A resolved ``graph_entities`` row for the timeline seed set.

    Projected from ``graph_entities`` by :func:`_resolve_entities`. ``id`` is the
    UUID (as text); ``name`` / ``canonical_key`` / ``entity_type`` mirror the
    catalog row. Not the full :class:`brain.graph_rag.schema.GraphEntity` — the
    timeline only needs the identity + display name.
    """

    id: str
    name: str
    canonical_key: str
    entity_type: str


@dataclass(frozen=True)
class TimelineBucket:
    """One time bucket of entity activity (spec §3e).

    ``bucket`` is the human label (e.g. ``"2024-Q1"``); ``bucket_start`` is the
    ``date_trunc`` anchor (TZ-aware) used for sorting + JSON. ``doc_count`` is the
    distinct-document count (deduped across the matched entities at the bucket
    level), ``mention_count`` the summed mentions. ``doc_ids`` / ``doc_titles``
    are the bucket's documents; ``cotopics`` the top co-occurring entity names;
    ``synthesis`` the optional per-bucket Ollama summary (``None`` unless
    ``--synthesize`` ran and succeeded for this bucket).
    """

    bucket: str
    bucket_start: datetime
    doc_count: int
    mention_count: int
    doc_ids: list[str] = field(default_factory=list)
    doc_titles: list[str] = field(default_factory=list)
    cotopics: list[str] = field(default_factory=list)
    synthesis: str | None = None


@dataclass(frozen=True)
class TimelineContext:
    """The wire shape returned by :func:`build_timeline` (CLI ``--json`` / MCP).

    ``query`` is the free-text entity/theme; ``entity_names`` the resolved seed
    entities; ``granularity`` the RESOLVED concrete bucket width (always one of
    ``month`` / ``quarter`` / ``year`` — the ``auto`` sentinel is resolved before
    this is built); ``granularity_auto`` is ``True`` when that width was chosen
    automatically (vs. an explicit ``--granularity``). ``person`` is the resolved
    display name when ``--person`` scoped (else ``None``); ``buckets`` the
    ascending time series; ``buckets_omitted`` how many buckets ``--limit``
    trimmed (0 when untrimmed). An all-empty result (no entities / no docs)
    carries an empty ``buckets`` list — never an exception.
    """

    query: str
    tenant_id: str
    granularity: str
    granularity_auto: bool = False
    entity_names: list[str] = field(default_factory=list)
    person: str | None = None
    buckets: list[TimelineBucket] = field(default_factory=list)
    buckets_omitted: int = 0


# ---------------------------------------------------------------------------
# Pure helpers (no DB) — unit-tested directly.
# ---------------------------------------------------------------------------


def _validate_granularity(granularity: str) -> str:
    """Return ``granularity`` if valid, else raise ``ValueError``.

    Validated against the same ``{auto, month, quarter, year}`` set the config
    knob uses. ``auto`` is a sentinel resolved to a concrete width by
    :func:`_resolve_auto_granularity` once the matched docs' date span is known;
    the concrete value is later interpolated as the ``date_trunc`` field, so a
    closed whitelist keeps that interpolation safe.
    """
    normalized = granularity.strip().lower()
    if normalized not in _VALID_TIMELINE_GRANULARITIES:
        raise ValueError(
            "granularity must be one of auto/month/quarter/year "
            f"(got {granularity!r})"
        )
    return normalized


def _resolve_auto_granularity(dates: list[datetime]) -> str:
    """Pick the auto bucket width for the matched docs' date span (else month).

    Pure: given the distinct date-anchors, return the COARSEST width
    (iterating ``year`` → ``quarter`` → ``month``) that still produces at least
    :data:`_AUTO_MIN_BUCKETS` non-empty buckets. Coarsest-that-clears-the-bar
    keeps evolution visible (>=3 periods) while minimizing fragmentation — a
    2-year span shows ~8 quarters rather than ~24 months, while a 5-month span
    can only reach the bar at month granularity.

    Note on ordering: a finer width always yields >= as many buckets as a coarser
    one, so "the finest yielding >=3" would degenerate to always-month and make
    the fallback below unreachable. Coarsest-first is the reading consistent with
    the spec's ``{year, quarter, month}`` ordering AND its explicit
    "if even month yields <3, use month" fallback.

    When NO width clears the bar — a young or sparse corpus, e.g. the original
    ~5-month report where quarter collapsed to one bucket — fall back to
    ``month`` so the finest-grained (most informative) view is still shown rather
    than collapsing everything into one coarse bucket. An empty ``dates`` list
    yields ``month``.
    """
    for gran in ("year", "quarter", "month"):
        distinct = {_bucket_label(d, gran) for d in dates}
        if len(distinct) >= _AUTO_MIN_BUCKETS:
            return gran
    return "month"


def _validate_trim(trim: str) -> str:
    """Return ``trim`` if valid, else raise ``ValueError`` (oldest|sparsest)."""
    normalized = trim.strip().lower()
    if normalized not in _VALID_TIMELINE_TRIMS:
        raise ValueError(
            f"trim must be one of oldest/sparsest (got {trim!r})"
        )
    return normalized


def _parse_month(value: str, *, field_name: str) -> datetime:
    """Parse an ``YYYY-MM`` cutoff into a TZ-aware first-of-month ``datetime``.

    Raises ``ValueError`` (mapped to a CLI ``BadParameter`` / MCP
    ``INVALID_PARAMS`` upstream) on any non ``YYYY-MM`` string.
    """
    raw = value.strip()
    try:
        parsed = datetime.strptime(raw, "%Y-%m")
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be an ISO month (YYYY-MM); got {value!r}"
        ) from exc
    return parsed.replace(day=1, tzinfo=UTC)


def _add_one_month(moment: datetime) -> datetime:
    """Return the first instant of the month after ``moment`` (TZ preserved).

    Used to make ``--until YYYY-MM`` inclusive of the named month: the query
    upper bound becomes ``doc_date < first-of-next-month``.
    """
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1)
    return moment.replace(month=moment.month + 1)


def _bucket_label(bucket_start: datetime, granularity: str) -> str:
    """Format a ``date_trunc`` anchor into a human bucket label.

    ``month`` → ``"2024-03"``, ``quarter`` → ``"2024-Q2"``, ``year`` → ``"2024"``.
    """
    year = bucket_start.year
    if granularity == "year":
        return f"{year:04d}"
    if granularity == "quarter":
        quarter = (bucket_start.month - 1) // 3 + 1
        return f"{year:04d}-Q{quarter}"
    return f"{year:04d}-{bucket_start.month:02d}"


def _trim_buckets(
    buckets: list[TimelineBucket], limit: int, trim: str
) -> tuple[list[TimelineBucket], int]:
    """Trim ``buckets`` to ``limit`` per the ``trim`` strategy (spec §3f step 2).

    ``buckets`` must be sorted ascending by ``bucket_start``. ``oldest`` keeps the
    most-recent ``limit`` buckets (drops the earliest); ``sparsest`` drops the
    buckets with the fewest docs (ties broken by oldest-first removal). Returns
    ``(kept_sorted_ascending, omitted_count)``. A non-positive ``limit`` is a
    caller bug guarded upstream; here ``limit <= 0`` trims everything.
    """
    total = len(buckets)
    if total <= limit:
        return list(buckets), 0
    omitted = total - limit
    if trim == "sparsest":
        # Rank by (doc_count asc, bucket_start asc) and drop the first ``omitted``
        # — the fewest-doc buckets, oldest-first on ties.
        by_sparsity = sorted(
            buckets, key=lambda b: (b.doc_count, b.bucket_start)
        )
        keep = {id(b) for b in by_sparsity[omitted:]}
        kept = [b for b in buckets if id(b) in keep]
    else:  # "oldest" — keep the most recent ``limit`` (buckets is ascending)
        kept = buckets[omitted:]
    return kept, omitted


# ---------------------------------------------------------------------------
# DB helpers.
# ---------------------------------------------------------------------------


def _doc_date_column_exists(conn: psycopg.Connection[Any]) -> bool:
    """True iff the optional migration-021 ``documents.doc_date`` column exists.

    Detected via ``information_schema.columns`` so the timeline transparently
    uses the indexed generated column when present and falls back to the inline
    ``COALESCE`` expression otherwise (spec §3e / §6 — zero regression before
    migration 021 is applied).
    """
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'documents' AND column_name = 'doc_date'"
    ).fetchone()
    return row is not None


def _doc_date_expr(conn: psycopg.Connection[Any]) -> str:
    """Return the SQL doc-date expression (internal whitelist, never user input).

    ``d.doc_date`` when migration 021 is applied, else the inline
    ``COALESCE(d.sent_at, d.ingested_at)``. The returned string is one of two
    fixed literals — safe to interpolate into the bucketing SQL.
    """
    if _doc_date_column_exists(conn):
        return "d.doc_date"
    return "COALESCE(d.sent_at, d.ingested_at)"


def _resolve_entities(
    conn: psycopg.Connection[Any], tenant_id: str, query: str
) -> list[EntityRow]:
    """Resolve a free-text query to ``graph_entities`` via ILIKE (spec §3f step 1).

    A dedicated parameterized ILIKE against ``name`` / ``canonical_key`` (NOT
    ``relational.list_entities``, which has no name filter). Both placeholders
    receive ``f"%{query}%"`` — never f-stringed into the SQL. Returns up to
    :data:`_ENTITY_RESOLVE_LIMIT` matches ordered by name; an empty result is a
    normal "no entities found" outcome, not an error.
    """
    needle = f"%{query.strip()}%"
    rows = conn.execute(
        "SELECT id::text, name, canonical_key, entity_type "
        "FROM graph_entities "
        "WHERE tenant_id = %s AND (canonical_key ILIKE %s OR name ILIKE %s) "
        "ORDER BY name ASC, id ASC "
        "LIMIT %s",
        (tenant_id, needle, needle, _ENTITY_RESOLVE_LIMIT),
    ).fetchall()
    return [
        EntityRow(
            id=str(r[0]), name=str(r[1]), canonical_key=str(r[2]), entity_type=str(r[3])
        )
        for r in rows
    ]


def _owner_entity_ids(
    conn: psycopg.Connection[Any], tenant_id: str, owner_participants: frozenset[str]
) -> list[str]:
    """Resolve the corpus owner's participant keys to ``person`` entity ids.

    Mirrors :func:`brain.graph_rag.themes._person_entity_ids` (kept local to
    preserve the timeline module's single responsibility): a person entity's
    ``canonical_key`` is the lowercased People-Hub display name, which the owner
    key set carries alongside emails, so the name key matches and email keys
    harmlessly miss. Empty owner set → no ids.
    """
    keys = sorted(k for k in owner_participants if k)
    if not keys:
        return []
    rows = conn.execute(
        "SELECT id::text FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = 'person' "
        "AND lower(canonical_key) = ANY(%s)",
        (tenant_id, keys),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _scope_to_person(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    entity_ids: list[str],
    person_keys: list[str],
) -> list[str]:
    """Restrict to docs where the person co-appears as a participant (spec §3f).

    ``person_keys`` are ``PersonMatch.keys`` (participant-key strings) — NOT
    graph entity ids. Overlaps them against ``documents.participants`` and
    intersects with the documents that mention any seed entity. Returns the
    qualifying document-id set (as text); an empty set means the person never
    co-appears with the theme.

    The overlap is **case-insensitive**: ``documents.participants`` is written by
    ingest extractors in source-preserved case (Gmail emits
    ``"Alice Doe <alice@example.com>"``) while ``PersonMatch.keys`` are lowercased, so
    a plain ``&&`` would miss every mixed-case stored value. We unnest the array
    and ``lower()`` each element before comparing — the same pattern the hybrid
    search ``--person`` filter uses (:func:`brain.search.hybrid_search`).
    """
    if not entity_ids or not person_keys:
        return []
    rows = conn.execute(
        "SELECT id::text FROM documents "
        "WHERE EXISTS (SELECT 1 FROM unnest(participants) AS _p "
        "             WHERE lower(_p) = ANY(%s::text[])) "
        "AND id = ANY("
        "    SELECT document_id FROM graph_entity_mentions "
        "    WHERE entity_id = ANY(%s) AND tenant_id = %s"
        ")",
        (person_keys, entity_ids, tenant_id),
    ).fetchall()
    return [str(r[0]) for r in rows]


@dataclass(frozen=True)
class _RawBucket:
    """Intermediate bucket row from :func:`_query_buckets` (pre-enrichment)."""

    bucket_start: datetime
    doc_count: int
    mention_count: int
    doc_ids: list[str]


def _compose_doc_filter(
    entity_ids: list[str],
    tenant_id: str,
    *,
    date_expr: str,
    since: datetime | None,
    until: datetime | None,
    doc_scope: list[str] | None,
) -> tuple[list[str], list[Any]]:
    """Build the shared parameterized WHERE clauses for the bucketing queries.

    Both :func:`_query_buckets` (the grouped bucket query) and
    :func:`_distinct_doc_dates` (the auto-granularity probe) constrain the same
    ``graph_entity_mentions``↔``documents`` join the same way, so the clause +
    bound-param composition lives here once (DRY). ``date_expr`` is an internal
    whitelist literal; everything else is bound as ``%s`` parameters.
    """
    where = ["gem.entity_id = ANY(%s)", "gem.tenant_id = %s"]
    params: list[Any] = [entity_ids, tenant_id]
    if since is not None:
        where.append(f"{date_expr} >= %s")
        params.append(since)
    if until is not None:
        where.append(f"{date_expr} < %s")
        params.append(until)
    if doc_scope is not None:
        where.append("d.id = ANY(%s)")
        params.append(doc_scope)
    return where, params


def _distinct_doc_dates(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    entity_ids: list[str],
    *,
    date_expr: str,
    since: datetime | None,
    until: datetime | None,
    doc_scope: list[str] | None,
) -> list[datetime]:
    """Distinct document date-anchors for the matched set (auto-granularity probe).

    Runs the same join + WHERE as :func:`_query_buckets` but selects the distinct
    ``date_expr`` values (one per distinct anchor), so :func:`_resolve_auto_granularity`
    can count how many buckets each width would produce before the full grouped
    query runs. NULL anchors are dropped. Only invoked when granularity is
    ``auto``.
    """
    where, params = _compose_doc_filter(
        entity_ids,
        tenant_id,
        date_expr=date_expr,
        since=since,
        until=until,
        doc_scope=doc_scope,
    )
    sql = (
        f"SELECT DISTINCT {date_expr} AS d "
        "FROM graph_entity_mentions gem "
        "JOIN documents d ON d.id = gem.document_id "
        f"WHERE {' AND '.join(where)}"
    )
    rows = conn.execute(sql, params).fetchall()
    return [row[0] for row in rows if row[0] is not None]


def _query_buckets(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    entity_ids: list[str],
    *,
    granularity: str,
    date_expr: str,
    since: datetime | None,
    until: datetime | None,
    doc_scope: list[str] | None,
) -> list[_RawBucket]:
    """Run the temporal bucketing query (spec §3e).

    One query over all ``entity_ids`` with ``COUNT(DISTINCT d.id)`` /
    ``SUM(mention_count)`` / ``ARRAY_AGG(DISTINCT d.id)`` so a doc mentioned by
    several matched entities is counted once per bucket. ``date_expr`` is an
    internal whitelist literal; ``granularity`` is a validated ``date_trunc``
    field passed as a bound parameter. ``since`` / ``until`` (inclusive of the
    named month — ``until`` is already first-of-next-month) and ``doc_scope``
    (person filter) compose as extra parameterized clauses.
    """
    where, params = _compose_doc_filter(
        entity_ids,
        tenant_id,
        date_expr=date_expr,
        since=since,
        until=until,
        doc_scope=doc_scope,
    )
    sql = (
        f"SELECT date_trunc(%s, {date_expr}) AS bucket_start, "
        "       COUNT(DISTINCT d.id) AS doc_count, "
        "       COALESCE(SUM(gem.mention_count), 0) AS mention_count, "
        "       ARRAY_AGG(DISTINCT d.id::text) AS doc_ids "
        "FROM graph_entity_mentions gem "
        "JOIN documents d ON d.id = gem.document_id "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY 1 "
        "ORDER BY 1 ASC"
    )
    rows = conn.execute(sql, [granularity, *params]).fetchall()
    return [
        _RawBucket(
            bucket_start=row[0],
            doc_count=int(row[1]),
            mention_count=int(row[2]),
            doc_ids=[str(d) for d in (row[3] or [])],
        )
        for row in rows
    ]


def _top_cotopics(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    doc_ids: list[str],
    seed_entity_ids: list[str],
    owner_entity_ids: list[str],
) -> list[str]:
    """Top co-occurring entity names for a bucket's docs (spec §3f step 3).

    Aggregates ``graph_edge_contributions`` restricted to the bucket's docs and
    edges touching a seed entity; the non-seed endpoint is the co-topic. Excludes
    the seed entities and the corpus owner (same suppression as themes mode).
    Returns up to :data:`_COTOPIC_LIMIT` names by descending co-occurrence.
    """
    if not doc_ids or not seed_entity_ids:
        return []
    excluded = seed_entity_ids + owner_entity_ids
    rows = conn.execute(
        "SELECT ge.name, SUM(sub.cooccur) AS total FROM ("
        "    SELECT CASE WHEN gec.src_id = ANY(%s) THEN gec.dst_id "
        "                ELSE gec.src_id END AS co_id, "
        "           gec.cooccur_count AS cooccur "
        "    FROM graph_edge_contributions gec "
        "    WHERE gec.tenant_id = %s AND gec.document_id = ANY(%s) "
        "      AND (gec.src_id = ANY(%s) OR gec.dst_id = ANY(%s)) "
        ") sub "
        "JOIN graph_entities ge ON ge.id = sub.co_id AND ge.tenant_id = %s "
        "WHERE sub.co_id::text <> ALL(%s) "
        "GROUP BY ge.name "
        "ORDER BY total DESC, ge.name ASC "
        "LIMIT %s",
        (
            seed_entity_ids,
            tenant_id,
            doc_ids,
            seed_entity_ids,
            seed_entity_ids,
            tenant_id,
            excluded,
            _COTOPIC_LIMIT,
        ),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _bucket_doc_titles(
    conn: psycopg.Connection[Any], doc_ids: list[str]
) -> list[str]:
    """Project document titles for a bucket (spec §3f step 4).

    Ordered most-recent-first (by the inline doc-date anchor) then title for
    determinism. ``doc_ids`` come straight from :func:`_query_buckets`.
    """
    if not doc_ids:
        return []
    rows = conn.execute(
        "SELECT title FROM documents WHERE id = ANY(%s) "
        "ORDER BY COALESCE(sent_at, ingested_at) DESC, title ASC",
        (doc_ids,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _bucket_doc_summaries(
    conn: psycopg.Connection[Any], doc_ids: list[str]
) -> list[tuple[str, str | None]]:
    """Project ``(title, summary)`` pairs for a bucket's docs (synthesis grounding).

    Selects ``title`` + ``summary`` ONLY — never ``content`` — mirroring the
    audio bundle's summary-only privacy projection
    (:func:`brain.queries.fetch_document_summary`). Ordered most-recent-first by
    the inline doc-date anchor so the budget keeps the freshest summaries when it
    trims. ``summary`` is ``None`` for a not-yet-enriched doc; the budgeter falls
    back to the title.
    """
    if not doc_ids:
        return []
    rows = conn.execute(
        "SELECT title, summary FROM documents WHERE id = ANY(%s) "
        "ORDER BY COALESCE(sent_at, ingested_at) DESC, title ASC",
        (doc_ids,),
    ).fetchall()
    return [(str(r[0]), r[1]) for r in rows]


def _budget_doc_summaries(
    rows: list[tuple[str, str | None]],
    *,
    count_tokens: Callable[[str], int],
    max_tokens: int,
) -> list[str]:
    """Greedily project ``(title, summary)`` rows into a token-budgeted bundle.

    Each row becomes its ``summary`` (the grounding evidence) or, when the
    summary is NULL/blank, its ``title`` (the documented fallback). Rows are
    included most-recent-first while the running total stays within
    ``max_tokens`` (measured by the injected ``count_tokens`` so the budget is
    testable without tiktoken); the first row is always included even if it alone
    exceeds the budget, so the bundle is never empty when docs exist. Mirrors
    :func:`brain.audio.build_prompt`'s greedy budgeting pattern.
    """
    out: list[str] = []
    used = 0
    for title, summary in rows:
        text = summary.strip() if summary and summary.strip() else title
        cost = count_tokens(text)
        if out and used + cost > max_tokens:
            break
        out.append(text)
        used += cost
    return out


def _synthesize_buckets(
    buckets: list[TimelineBucket],
    *,
    entity_name: str,
    enricher: OllamaEnricher,
    synth_limit: int,
    fetch_summaries: Callable[[list[str]], list[str]],
) -> list[TimelineBucket]:
    """Attach best-effort, content-grounded Ollama summaries (spec §3f step 5).

    Synthesizes the top ``synth_limit`` buckets by ``doc_count`` (ties broken by
    most-recent ``bucket_start``), but iterates the buckets OLDEST→NEWEST so each
    synthesized bucket can be handed the chronologically previous bucket's label,
    co-topics, and (when that previous bucket was itself synthesized) its
    already-generated synthesis — enabling the model to state what CHANGED. Each
    chosen bucket's document summaries are fetched + token-budgeted via
    ``fetch_summaries`` (injected so this stays unit-testable without a DB) and
    passed as the model's primary grounding evidence.

    Best-effort: ``summarize_bucket`` never raises, returning ``None`` on Ollama
    failure, so the timeline still renders. Returns a new bucket list
    (immutability) preserving the original ascending order.
    """
    if synth_limit <= 0 or not buckets:
        return buckets
    densest = sorted(
        buckets, key=lambda b: (b.doc_count, b.bucket_start), reverse=True
    )[:synth_limit]
    chosen = {id(b) for b in densest}
    out: list[TimelineBucket] = []
    prev: TimelineBucket | None = None
    for bucket in buckets:  # ascending (oldest → newest)
        if id(bucket) in chosen:
            doc_summaries = fetch_summaries(bucket.doc_ids)
            summary = enricher.summarize_bucket(
                bucket_label=bucket.bucket,
                entity_name=entity_name,
                doc_titles=bucket.doc_titles,
                cotopics=bucket.cotopics,
                doc_summaries=doc_summaries,
                prev_bucket_label=prev.bucket if prev else None,
                prev_cotopics=prev.cotopics if prev else None,
                prev_synthesis=prev.synthesis if prev else None,
            )
            new_bucket = dataclasses.replace(bucket, synthesis=summary)
            out.append(new_bucket)
            prev = new_bucket
        else:
            out.append(bucket)
            prev = bucket
    return out


def build_timeline(
    conn: psycopg.Connection[Any],
    cfg: Config,
    query: str,
    *,
    granularity: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    person: str | None = None,
    synthesize: bool = False,
    enricher: OllamaEnricher | None = None,
    tenant: str | None = None,
) -> TimelineContext:
    """Build the temporal timeline for ``query`` (spec §3f — the orchestrator).

    Resolves the entity/theme to ``graph_entities`` (ILIKE), optionally scopes to
    a ``--person``'s co-documents, picks the bucket width (``auto`` — the default
    — chooses the finest of {year, quarter, month} yielding >=3 buckets, else
    month; an explicit value forces it), buckets mentions by document date,
    attaches per-bucket co-topics + doc titles, trims to ``limit``
    (``BRAIN_TIMELINE_TRIM`` end), and — when ``synthesize`` and an ``enricher``
    are supplied — adds a best-effort, content-grounded summary to the densest
    buckets (the docs' summaries feed the LLM, not just their titles).

    Graceful, never-raising on data shape: zero matching entities or zero docs
    yields an empty :class:`TimelineContext`. Only programmer/usage errors raise:
    a bad ``granularity`` / ``since`` / ``until`` raises ``ValueError``; an
    unknown / ambiguous ``person`` propagates ``PersonNotFound`` /
    ``PersonAmbiguous`` (mapped to clean CLI/MCP errors by the caller).
    """
    requested = _validate_granularity(granularity or cfg.timeline_granularity)
    auto = requested == "auto"
    # Concrete width for the empty-data early returns (auto with no docs → month).
    empty_gran = "month" if auto else requested
    trim = _validate_trim(cfg.timeline_trim)
    limit_n = cfg.timeline_limit if limit is None else limit
    if limit_n < 1:
        raise ValueError(f"limit must be a positive integer (got {limit_n})")
    tenant_id = resolve_tenant(cfg, tenant)
    since_dt = _parse_month(since, field_name="since") if since else None
    # ``until`` is inclusive of the named month: upper bound = first-of-next-month.
    until_dt = (
        _add_one_month(_parse_month(until, field_name="until")) if until else None
    )

    entities = _resolve_entities(conn, tenant_id, query)
    if not entities:
        return TimelineContext(
            query=query,
            tenant_id=tenant_id,
            granularity=empty_gran,
            granularity_auto=auto,
        )

    entity_ids = [e.id for e in entities]
    entity_names = [e.name for e in entities]

    person_display: str | None = None
    doc_scope: list[str] | None = None
    if person is not None:
        match = resolve_person_to_keys(conn, person)
        person_display = match.display_name
        doc_scope = _scope_to_person(conn, tenant_id, entity_ids, match.keys)
        if not doc_scope:
            _logger.warning(
                "timeline: no documents where %r co-appears as a participant",
                person,
            )
            return TimelineContext(
                query=query,
                tenant_id=tenant_id,
                granularity=empty_gran,
                granularity_auto=auto,
                entity_names=entity_names,
                person=person_display,
            )

    date_expr = _doc_date_expr(conn)
    # Auto-granularity: probe the matched docs' distinct date-anchors and pick the
    # coarsest width yielding >=3 buckets (else month) BEFORE the grouped query.
    if auto:
        dates = _distinct_doc_dates(
            conn,
            tenant_id,
            entity_ids,
            date_expr=date_expr,
            since=since_dt,
            until=until_dt,
            doc_scope=doc_scope,
        )
        gran = _resolve_auto_granularity(dates)
    else:
        gran = requested

    raw_buckets = _query_buckets(
        conn,
        tenant_id,
        entity_ids,
        granularity=gran,
        date_expr=date_expr,
        since=since_dt,
        until=until_dt,
        doc_scope=doc_scope,
    )
    if not raw_buckets:
        return TimelineContext(
            query=query,
            tenant_id=tenant_id,
            granularity=gran,
            granularity_auto=auto,
            entity_names=entity_names,
            person=person_display,
        )

    owner_ids = _owner_entity_ids(conn, tenant_id, cfg.owner_participants)
    enriched: list[TimelineBucket] = []
    for raw in raw_buckets:
        cotopics = _top_cotopics(conn, tenant_id, raw.doc_ids, entity_ids, owner_ids)
        titles = _bucket_doc_titles(conn, raw.doc_ids)
        enriched.append(
            TimelineBucket(
                bucket=_bucket_label(raw.bucket_start, gran),
                bucket_start=raw.bucket_start,
                doc_count=raw.doc_count,
                mention_count=raw.mention_count,
                doc_ids=raw.doc_ids,
                doc_titles=titles,
                cotopics=cotopics,
            )
        )

    kept, omitted = _trim_buckets(enriched, limit_n, trim)

    if synthesize and enricher is not None and cfg.timeline_synth_limit > 0:
        # Grounding evidence: the docs' SUMMARIES (title fallback), token-budgeted
        # so the prompt stays fast on a local 8B model. Injected as a closure so
        # ``_synthesize_buckets`` stays unit-testable without a DB.
        token_counter = enricher.count_tokens

        def _fetch_bucket_summaries(doc_ids: list[str]) -> list[str]:
            rows = _bucket_doc_summaries(conn, doc_ids)
            return _budget_doc_summaries(
                rows,
                count_tokens=token_counter,
                max_tokens=_SYNTH_DOC_SUMMARY_BUDGET_TOKENS,
            )

        kept = _synthesize_buckets(
            kept,
            entity_name=entity_names[0],
            enricher=enricher,
            synth_limit=cfg.timeline_synth_limit,
            fetch_summaries=_fetch_bucket_summaries,
        )

    return TimelineContext(
        query=query,
        tenant_id=tenant_id,
        granularity=gran,
        granularity_auto=auto,
        entity_names=entity_names,
        person=person_display,
        buckets=kept,
        buckets_omitted=omitted,
    )
