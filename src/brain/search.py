"""Hybrid search: FTS + vector via Reciprocal Rank Fusion.

Three Phase-D refinements live here in addition to the original RRF
combiner (see `docs/plans/2026-05-06-search-ranking-fix.md`):

1. **Per-document FTS candidate cap** (revision #1). The FTS leg is
   wrapped in a window-function CTE that keeps the top
   :data:`PER_DOC_CHUNK_CAP` chunks per ``document_id`` before the
   global ``LIMIT 50``, so a single long title-matching doc can no
   longer monopolize the candidate set.

2. **Compact-form query expansion** (revision #2).
   :func:`_build_tsquery` ORs the standard tokenization with the
   lowercase-concatenated form when the raw query has 2+ tokens, so
   `Example Group` matches a doc whose only relevant term is the
   single-token `[example-group]`.

3. **Vector cosine floor** (revisions #3 + #6). The vector leg
   filters out chunks below ``vector_sim_floor`` (default
   :data:`DEFAULT_VECTOR_SIM_FLOOR`, overridable via
   ``BRAIN_VECTOR_SIM_FLOOR`` in :mod:`brain.config`). Tuned
   empirically — see :mod:`brain.config` and
   ``tests/test_search_floor_default_excludes_known_bad.py``.

The fts_only path bypasses (3) entirely.
"""
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from .ingest import Embedder


@dataclass(frozen=True)
class SearchExplanation:
    """Per-document ranking diagnostic.

    Attached to :class:`SearchResult` when ``hybrid_search(..., explain=True)``.
    Fields are nullable where the corresponding leg didn't contribute — e.g. a
    doc that only appears in the FTS leg has ``vector_rank=None`` /
    ``vector_cosine=None`` / ``vector_rrf_contribution=0.0``.
    """

    fts_rank: int | None  # 1-indexed; None if the best chunk didn't appear in FTS
    fts_score: float | None  # ts_rank value; None if absent from FTS leg
    fts_rrf_contribution: float  # 1/(60+fts_rank) or 0.0
    vector_rank: int | None  # 1-indexed; None if absent from vector leg
    vector_cosine: float | None  # 1 - (embedding <=> query); None if absent
    vector_rrf_contribution: float  # 1/(60+vector_rank) or 0.0
    rrf_score: float  # raw RRF sum before recency boost
    recency_age_days: float | None  # None if recency disabled or no timestamp
    recency_boost: float  # 1.0 when disabled / unaffected
    final_score: float  # post-recency; matches SearchResult.score
    best_chunk_id: str  # UUID of the highest-scoring chunk for this doc
    best_chunk_index: int  # 0-based chunk index within the document
    matched_filters: dict[str, Any]  # {"source_kind", "tag", "since_days", "fts_only"}
    reranker_score: float | None = None  # Q3-A will populate; today always None


@dataclass
class SearchResult:
    """A single search hit grouped at document granularity with its best chunk."""

    document_id: str
    title: str
    source_kind: str | None
    snippet: str
    score: float
    content_type: str
    tags: list[str]
    explain: SearchExplanation | None = None  # opt-in; populated only when explain=True


RRF_K = 60
CANDIDATE_LIMIT = 50
SNIPPET_LENGTH = 400

# Maximum FTS chunks kept per document before the global candidate cut.
# K=3 retains overlap signal across body chunks while preventing a long
# title-matching doc (the live corpus has docs with 304+ chunks) from
# filling the entire 50-candidate slot. Per plan revision #1.
PER_DOC_CHUNK_CAP = 3

# Token regex for compact-form query expansion. Matches alphanumeric runs
# starting with a letter so we strip stray punctuation but preserve
# embedded digits (`v2`, `cto4u` etc.). See :func:`_build_tsquery`.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _build_tsquery(conn: psycopg.Connection, raw_query: str) -> str:
    """Return a ``to_tsquery``-compatible string for ``raw_query``.

    When the query has 2+ alphabetic tokens, ORs the standard
    ``plainto_tsquery`` form with the lowercase-concatenated compact
    form (e.g. ``Example Group`` → ``(cto & lunch) | ctolunch``). This
    catches docs whose only mention of the term is a single compact
    token like ``[example-group]`` that the English parser stems to
    ``ctolunch``.

    Returns an empty string for empty / pure-punctuation input —
    ``to_tsquery('')`` is a valid empty tsquery that matches nothing.
    """
    tokens = _TOKEN_RE.findall(raw_query)
    standard_row = conn.execute(
        "SELECT plainto_tsquery('english', %s)::text", (raw_query,)
    ).fetchone()
    standard = standard_row[0] if standard_row else ""
    if len(tokens) < 2 or not standard:
        return standard
    compact = "".join(tokens).lower()
    compact_row = conn.execute(
        "SELECT plainto_tsquery('english', %s)::text", (compact,)
    ).fetchone()
    compact_tsq = compact_row[0] if compact_row else ""
    if not compact_tsq or compact_tsq == standard:
        return standard
    return f"({standard}) | ({compact_tsq})"


def hybrid_search(
    conn: psycopg.Connection,
    *,
    embedder: Embedder,
    query: str,
    limit: int = 5,
    source_kind: str | None = None,
    tag: str | None = None,
    since_days: int | None = None,
    fts_only: bool = False,
    vector_sim_floor: float = 0.0,
    recency_halflife_days: float | None = None,
    snippet_context_tokens: int = 0,
    explain: bool = False,
    # — Q1-C metadata filters —
    person_keys: list[str] | None = None,
    person_display_name: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    content_type: str | None = None,
    thread_id: str | None = None,
    draft: bool | None = None,
    without_tag: str | None = None,
) -> list[SearchResult]:
    """Combine FTS and vector ranks via Reciprocal Rank Fusion.

    Each chunk receives ``1 / (K + rank)`` from each ranker it appears in
    (K=60). Per-document scores are the max across that document's chunks,
    and the highest-scoring chunk per document becomes the returned snippet.

    When ``fts_only`` is True, the vector leg (and the Ollama embed call) is
    skipped — useful when the embedding service is unavailable. The cosine
    floor (``vector_sim_floor``) only applies to the vector leg; FTS
    candidates are not filtered by it.

    ``vector_sim_floor`` filters chunks whose ``1 - cosine_distance`` is
    below the floor. Default ``0.0`` keeps backwards compatibility for
    direct callers; the CLI plumbs ``cfg.vector_sim_floor`` through.

    ``recency_halflife_days`` applies an exponential-decay boost after RRF:
    ``score *= 0.5 ** (age_days / halflife_days)`` where ``age_days`` comes
    from ``coalesce(sent_at, ingested_at)``. ``None`` (default) disables
    the boost. Future-dated rows get ``boost = 1.0`` (clamped, not boosted).

    ``snippet_context_tokens`` expands the best-matching chunk's snippet by
    pulling neighboring chunks (``chunk_index ± W``) from the same document
    and stitching them together up to the token budget. ``0`` (default)
    returns the single-chunk snippet unchanged.

    Q1-C metadata filters (all optional, default ``None`` = no filter):

    - ``person_keys`` — case-insensitive overlap against
      ``documents.participants``. Caller is responsible for resolving the
      ``--person <name>`` argument via
      :func:`brain.queries.resolve_person_to_keys` before calling
      ``hybrid_search`` (the resolver may raise
      :class:`brain.errors.PersonNotFound` / :class:`PersonAmbiguous`
      which the CLI / MCP layer maps to its framework's error type).
      ``person_display_name`` rides along into ``matched_filters`` for
      explain readability — it does not affect the SQL. Gmail stores
      participants in case-preserved form (``"Alice Doe <alice@x.com>"``)
      while the resolver returns lowercased keys, so the SQL lowercases
      each stored entry via ``unnest`` before comparing — at the cost of
      bypassing the GIN index on ``participants``, which is acceptable
      for a personal-corpus scale.
    - ``after`` / ``before`` — date-range predicate on
      ``coalesce(sent_at, ingested_at)``. Inclusive lower bound,
      exclusive upper bound (so ``after=X, before=X`` returns nothing).
    - ``content_type`` — exact match on ``documents.content_type``
      (``email``, ``email_thread``, ``note``, ``transcript``, …). NOT
      ``documents.kind`` (which is the vault/ingested tier enum).
    - ``thread_id`` — exact match on ``documents.thread_id`` (Gmail
      thread id; indexed via migration 007).
    - ``draft`` — three-state filter on ``documents.draft``: ``True``
      → drafts only, ``False`` → published only, ``None`` → both
      (default, matches pre-Q1-C behavior).
    - ``without_tag`` — exclude docs whose ``tags`` array contains the
      given tag. Combines with ``tag`` (AND) so callers can express
      "tagged X but not Y".
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
        # ``"Alice Doe <alice@x.com>"``); the resolver's keys are
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
    if after is not None:
        where_clauses.append("coalesce(d.sent_at, d.ingested_at) >= %s")
        where_params.append(after)
    if before is not None:
        where_clauses.append("coalesce(d.sent_at, d.ingested_at) < %s")
        where_params.append(before)
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
    where_sql = " AND ".join(where_clauses)

    tsquery = _build_tsquery(conn, query)

    # Per-doc cap CTE — keep the top PER_DOC_CHUNK_CAP chunks per
    # ``document_id`` (ranked by ts_rank) before the global LIMIT, so one
    # long doc can't fill the entire candidate slot.
    fts_sql = f"""
        WITH ranked AS (
            SELECT c.id, c.document_id, c.chunk_index, c.content,
                   ts_rank(c.tsv, to_tsquery('english', %s)) AS score,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.document_id
                       ORDER BY ts_rank(c.tsv, to_tsquery('english', %s)) DESC
                   ) AS rn
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.tsv @@ to_tsquery('english', %s) AND {where_sql}
        )
        SELECT id, document_id, chunk_index, content, score
        FROM ranked
        WHERE rn <= {PER_DOC_CHUNK_CAP}
        ORDER BY score DESC
        LIMIT {CANDIDATE_LIMIT}
    """
    fts_rows = conn.execute(
        fts_sql, [tsquery, tsquery, tsquery, *where_params]
    ).fetchall()

    vec_rows: list[Any] = []
    if not fts_only:
        q_emb = embedder.embed([query], input_type="query")[0]
        vec_sql = f"""
            SELECT c.id, c.document_id, c.chunk_index, c.content,
                   1 - (c.embedding <=> %s::vector) AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {where_sql}
              AND 1 - (c.embedding <=> %s::vector) >= %s
            ORDER BY c.embedding <=> %s::vector
            LIMIT {CANDIDATE_LIMIT}
        """
        vec_rows = conn.execute(
            vec_sql,
            [q_emb, *where_params, q_emb, vector_sim_floor, q_emb],
        ).fetchall()

    # Per-chunk rank tables (built only when explain=True; zero overhead otherwise).
    fts_rank_by_chunk: dict[str, int] = {}
    fts_score_by_chunk: dict[str, float] = {}
    vec_rank_by_chunk: dict[str, int] = {}
    vec_cosine_by_chunk: dict[str, float] = {}
    if explain:
        fts_rank_by_chunk = {str(row[0]): i + 1 for i, row in enumerate(fts_rows)}
        fts_score_by_chunk = {str(row[0]): float(row[4]) for row in fts_rows}
        vec_rank_by_chunk = {str(row[0]): i + 1 for i, row in enumerate(vec_rows)}
        vec_cosine_by_chunk = {str(row[0]): float(row[4]) for row in vec_rows}

    rrf: dict[str, float] = {}
    # Per-chunk RRF leg contributions (explain only).
    rrf_fts: dict[str, float] = {}
    rrf_vec: dict[str, float] = {}
    # chunk_id → (document_id, chunk_index, content)
    chunk_meta: dict[str, tuple[str, int, str]] = {}
    for rank, row in enumerate(fts_rows):
        cid = str(row[0])
        contrib = 1.0 / (RRF_K + rank + 1)
        rrf[cid] = rrf.get(cid, 0.0) + contrib
        if explain:
            rrf_fts[cid] = contrib
        chunk_meta[cid] = (str(row[1]), int(row[2]), row[3])
    for rank, row in enumerate(vec_rows):
        cid = str(row[0])
        contrib = 1.0 / (RRF_K + rank + 1)
        rrf[cid] = rrf.get(cid, 0.0) + contrib
        if explain:
            rrf_vec[cid] = contrib
        chunk_meta[cid] = (str(row[1]), int(row[2]), row[3])

    # document_id → (best_rrf_score, best_chunk_index, snippet_content, best_chunk_id)
    by_doc: dict[str, tuple[float, int, str, str]] = {}
    for cid, rrf_val in rrf.items():
        doc_id, chunk_idx, content = chunk_meta[cid]
        prev = by_doc.get(doc_id)
        if prev is None or rrf_val > prev[0]:
            by_doc[doc_id] = (rrf_val, chunk_idx, content, cid)

    if not by_doc:
        return []

    doc_ids = list(by_doc.keys())
    doc_rows = conn.execute(
        """
        SELECT d.id, d.title, d.content_type, d.tags, s.kind,
               coalesce(d.sent_at, d.ingested_at) AS recency_ts
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE d.id = ANY(%s)
        """,
        (doc_ids,),
    ).fetchall()
    docs = {str(r[0]): r for r in doc_rows}

    now = datetime.now(tz=UTC)
    results: list[SearchResult] = []
    for doc_id, (rrf_score, best_chunk_idx, snippet_content, best_cid) in by_doc.items():
        meta = docs[doc_id]

        score = rrf_score
        recency_age_days: float | None = None
        recency_boost_factor = 1.0

        # Recency boost: multiplicative decay over coalesce(sent_at, ingested_at).
        if recency_halflife_days is not None:
            recency_ts = meta[5]
            if recency_ts is not None:
                # Make the timestamp tz-aware if the DB returned a naive value.
                if recency_ts.tzinfo is None:
                    recency_ts = recency_ts.replace(tzinfo=UTC)
                recency_age_days = max(0.0, (now - recency_ts).total_seconds() / 86400.0)
                recency_boost_factor = 0.5 ** (recency_age_days / recency_halflife_days)
                score = rrf_score * recency_boost_factor

        # Snippet context expansion: pull neighboring chunks from the same doc.
        if snippet_context_tokens > 0:
            snippet_content = _expand_snippet_with_neighbors(
                conn,
                document_id=doc_id,
                best_chunk_index=best_chunk_idx,
                best_content=snippet_content,
                embedder=embedder,
                budget_tokens=snippet_context_tokens,
            )

        # Human table shows 120-char preview; JSON/MCP gets the full stitched
        # snippet (up to 4 × SNIPPET_LENGTH chars as a hard outer cap to guard
        # against a degenerate token-counter blowing out the MCP payload).
        if snippet_context_tokens > 0:
            snippet = snippet_content
        else:
            snippet = snippet_content[:SNIPPET_LENGTH]
        # Hard cap: 4 × SNIPPET_LENGTH prevents degenerate oversized payloads.
        if len(snippet) > 4 * SNIPPET_LENGTH:
            snippet = snippet[: 4 * SNIPPET_LENGTH]

        # Build the optional ranking diagnostic payload.
        explain_obj: SearchExplanation | None = None
        if explain:
            explain_obj = SearchExplanation(
                fts_rank=fts_rank_by_chunk.get(best_cid),
                fts_score=fts_score_by_chunk.get(best_cid),
                fts_rrf_contribution=rrf_fts.get(best_cid, 0.0),
                vector_rank=vec_rank_by_chunk.get(best_cid),
                vector_cosine=vec_cosine_by_chunk.get(best_cid),
                vector_rrf_contribution=rrf_vec.get(best_cid, 0.0),
                rrf_score=rrf_score,
                recency_age_days=recency_age_days,
                recency_boost=recency_boost_factor,
                final_score=score,
                best_chunk_id=best_cid,
                best_chunk_index=best_chunk_idx,
                matched_filters={
                    "source_kind": source_kind,
                    "tag": tag,
                    "since_days": since_days,
                    "fts_only": fts_only,
                    # Q1-C additions — datetimes serialize as ISO strings
                    # so the dict round-trips through JSON without a custom
                    # encoder. ``None`` values stay in the dict; the
                    # explain formatter skips them at render time.
                    "person_keys": list(person_keys) if person_keys else None,
                    "person_display_name": person_display_name,
                    "after": after.isoformat() if after is not None else None,
                    "before": before.isoformat() if before is not None else None,
                    "content_type": content_type,
                    "thread_id": thread_id,
                    "draft": draft,
                    "without_tag": without_tag,
                },
            )

        results.append(
            SearchResult(
                document_id=doc_id,
                title=meta[1],
                content_type=meta[2],
                tags=list(meta[3] or []),
                source_kind=meta[4],
                snippet=snippet,
                score=score,
                explain=explain_obj,
            )
        )
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]


# ---------------------------------------------------------------------------
# Snippet-context expansion helper
# ---------------------------------------------------------------------------

# Maximum number of neighbors on each side to fetch per finalist.
_NEIGHBOR_WINDOW = 2


def _expand_snippet_with_neighbors(
    conn: psycopg.Connection,
    *,
    document_id: str,
    best_chunk_index: int,
    best_content: str,
    embedder: Embedder,
    budget_tokens: int,
) -> str:
    """Expand a snippet by stitching neighboring chunks around the best match.

    Fetches up to :data:`_NEIGHBOR_WINDOW` chunks on each side of
    ``best_chunk_index`` within the same ``document_id``. Walks outward
    from the matched chunk, prepending the preceding neighbor and appending
    the following neighbor alternately, stopping when adding the next whole
    neighbor would exceed ``base_tokens + budget_tokens``. A neighbor is
    either included in full or not at all (no mid-chunk slicing).

    Returns the stitched string. The caller applies any final display
    truncation (e.g. 120-char table preview). A hard outer cap of
    ``4 × SNIPPET_LENGTH`` chars guards against a degenerate token-counter.
    """
    lo = max(0, best_chunk_index - _NEIGHBOR_WINDOW)
    hi = best_chunk_index + _NEIGHBOR_WINDOW
    neighbor_rows = conn.execute(
        """
        SELECT chunk_index, content
        FROM chunks
        WHERE document_id = %s
          AND chunk_index BETWEEN %s AND %s
        ORDER BY chunk_index
        """,
        (document_id, lo, hi),
    ).fetchall()

    # Index the fetched rows by chunk_index for O(1) lookup.
    by_idx: dict[int, str] = {int(r[0]): r[1] for r in neighbor_rows}

    # The matched chunk is always included in full.
    matched = by_idx.get(best_chunk_index, best_content)

    before: list[str] = []  # chunks with index < best, in ascending order
    after: list[str] = []   # chunks with index > best, in ascending order
    budget_used = 0

    # Walk outward alternately, consuming the token budget.
    prev_idx = best_chunk_index - 1
    next_idx = best_chunk_index + 1
    while budget_used < budget_tokens:
        added = False
        if prev_idx >= lo and prev_idx in by_idx:
            chunk = by_idx[prev_idx]
            cost = embedder.count_tokens(chunk)
            if budget_used + cost <= budget_tokens:
                before.insert(0, chunk)
                budget_used += cost
                prev_idx -= 1
                added = True
            else:
                prev_idx = -1  # stop prepending — budget exhausted
        if next_idx <= hi and next_idx in by_idx:
            chunk = by_idx[next_idx]
            cost = embedder.count_tokens(chunk)
            if budget_used + cost <= budget_tokens:
                after.append(chunk)
                budget_used += cost
                next_idx += 1
                added = True
            else:
                next_idx = hi + 1  # stop appending — budget exhausted
        if not added:
            break  # no more neighbors in range or budget fully spent

    parts = before + [matched] + after
    stitched = "\n\n".join(parts)

    # Hard outer cap.
    cap = 4 * SNIPPET_LENGTH
    return stitched[:cap] if len(stitched) > cap else stitched
