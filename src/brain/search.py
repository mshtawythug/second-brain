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
from functools import lru_cache
from typing import Any

import psycopg

from .ingest import Embedder
from .rank_fusion import rrf_contribution


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
class SearchDiagnostics:
    """Mutable out-parameter for cheap search-layer metrics.

    Passed to :func:`hybrid_search` via the ``diagnostics`` kwarg and populated
    in-place, so callers read the FTS-leg hit count WITHOUT changing the
    ``list[SearchResult]`` return contract every other caller depends on.

    ``fts_count`` is the number of FTS candidate chunks the lexical leg
    returned for the query. It is taken straight from ``len(fts_rows)`` — work
    the search already does — so reading it costs no extra query. The value is
    capped by the candidate limit + per-doc cap, so a positive count is NOT a
    true total; only the **zero** case is exact (``fts_count == 0`` iff no chunk
    matched the tsquery). That zero is the knowledge-gap signal for
    ``brain gaps``: the vector leg always returns nearest neighbours, so a
    lexical miss is otherwise invisible. ``None`` means the search never ran
    (the holder was created but not passed, or an exception preceded the FTS
    leg).
    """

    fts_count: int | None = None


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


# ---------------------------------------------------------------------------
# Query-embedding LRU cache (perf F1)
# ---------------------------------------------------------------------------

# In-process cache for query embeddings. The query embed call (e.g. Ollama
# Arctic) dominates search latency — ~115 ms warm / ~280 ms cold per the
# retrieval perf audit (2026-05-25). Identical query embeds recur within a
# single process: ``brain explain`` right after ``brain search``, an MCP
# multi-turn session. Those should not hit the embedder twice. A fresh CLI
# invocation starts cold, so the win is purely in-process / MCP. This is a
# module-level constant, NOT a Config knob (per the task scope).
_QUERY_EMBED_CACHE_SIZE = 256

# identity → embedder, populated on every :func:`_query_embed` call so the
# ``lru_cache``'d worker can recompute on a miss without taking the embedder
# (unhashable, per-instance) as a cache-key argument. Bounded by the number of
# distinct (class, model, dim) embedder identities seen in-process — at most a
# handful — so it never grows unbounded.
_embedder_registry: dict[str, Embedder] = {}


def _embedder_identity(embedder: Embedder) -> str:
    """Return a stable cache-key identity for ``embedder``.

    Combines the concrete class (module + qualname), the backend model name
    when the embedder exposes one (``_model`` on the Ollama/Voyage backends),
    and the output ``dim``. Two embedders that would yield *different* vectors
    for the same text — different model, backend, or dimensionality — MUST map
    to different identities so the query-embed cache never serves a vector
    computed by a different embedder/model.
    """
    cls = type(embedder)
    model = getattr(embedder, "_model", "")
    return f"{cls.__module__}.{cls.__qualname__}|{model}|{embedder.dim}"


@lru_cache(maxsize=_QUERY_EMBED_CACHE_SIZE)
def _cached_query_embed(
    identity: str, input_type: str, text: str
) -> tuple[float, ...]:
    """LRU-cached single-text embed keyed by ``(identity, input_type, text)``.

    The embedder is resolved from :data:`_embedder_registry` rather than passed
    as an argument, so every component of the cache key is hashable and the key
    is identity-scoped. Returns an immutable tuple — embeddings are lists
    (unhashable), and caching a mutable list would also let one caller corrupt
    another's vector.
    """
    embedder = _embedder_registry[identity]
    return tuple(embedder.embed([text], input_type=input_type)[0])


def _query_embed(
    embedder: Embedder, text: str, *, input_type: str = "query"
) -> list[float]:
    """Return the embedding for ``text`` via the in-process LRU cache.

    Registers ``embedder`` under its identity (so a cache miss can recompute),
    then returns a fresh ``list`` copy of the cached tuple — callers hand it to
    psycopg as a ``::vector`` parameter and must not mutate the shared cache
    entry. Behaviourally identical to ``embedder.embed([text],
    input_type=input_type)[0]`` apart from the caching.
    """
    identity = _embedder_identity(embedder)
    _embedder_registry[identity] = embedder
    return list(_cached_query_embed(identity, input_type, text))


def _ensure_utc(dt: datetime) -> datetime:
    """Stamp a naive datetime as UTC so ``timestamptz`` comparisons don't shift.

    ``--after 2026-01-01`` reaches the search layer as a *naive* midnight. Bound
    directly against a ``timestamptz`` column, Postgres interprets a naive
    literal in the **session** ``TimeZone``, shifting the boundary by the
    session's UTC offset — a doc sent at ``2026-01-01T03:00:00Z`` would fall
    *outside* ``--after 2026-01-01`` under an ``America/New_York`` session.
    Stamping UTC makes the boundary session-TZ-independent. Already-aware
    datetimes pass through unchanged. Mirrors the recency-boost idiom below
    (``recency_ts.replace(tzinfo=UTC)``).
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


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
    diagnostics: SearchDiagnostics | None = None,
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

    ``diagnostics`` (optional :class:`SearchDiagnostics`) is populated in place
    with the FTS-leg hit count (``fts_count``). ``None`` (default) skips it.
    See :class:`SearchDiagnostics` for why this is an out-parameter rather than
    a return-value change.

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
    # Auto-degrade to FTS-only when the active embedder produces no vectors
    # (the FTS-only ``NullEmbedder`` under ``BRAIN_EMBEDDER=none``). Duck-typed
    # via ``getattr`` so the real backends (Arctic / Qwen3 / Voyage) — which
    # never declare the flag — are unaffected, and EVERY caller (CLI, MCP,
    # library) degrades here rather than each re-implementing the check. This
    # also flows into ``matched_filters["fts_only"]`` below so ``explain`` shows
    # the effective mode.
    fts_only = fts_only or not getattr(embedder, "produces_embeddings", True)
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
        where_params.append(_ensure_utc(after))
    if before is not None:
        where_clauses.append("coalesce(d.sent_at, d.ingested_at) < %s")
        where_params.append(_ensure_utc(before))
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

    # No-filter fast path (perf F5 + F2). ``where_clauses`` always starts with
    # the literal ``"TRUE"``; every metadata filter appends a clause *and* a
    # param. So ``where_sql == "TRUE"`` (the common unfiltered CLI search)
    # means the ``documents`` JOIN supplies no column the FTS/vector legs
    # actually read — title/tags/source_kind/recency all come from the separate
    # ``doc_rows`` fetch below, and the inner JOIN on the ``document_id`` FK
    # can neither drop nor duplicate chunk rows. We therefore (F5) omit the
    # JOIN and (F2) force psycopg to prepare the now-static SQL so an
    # in-process / MCP repeated search reuses the plan (~15 ms planning saved).
    # The filtered path keeps the JOIN and leaves ``prepare=None`` (psycopg's
    # auto-prepare heuristic) since each distinct filter combo is a different
    # statement; a one-shot CLI invocation prepares-then-executes once, a
    # negligible no-op risk.
    has_filters = where_sql != "TRUE"
    prepare_flag: bool | None = None if has_filters else True
    join_clause = "JOIN documents d ON d.id = c.document_id" if has_filters else ""
    fts_filter = f" AND {where_sql}" if has_filters else ""

    tsquery = _build_tsquery(conn, query)

    # Two-level CTE (perf F3): the inner ``base`` computes ``ts_rank`` exactly
    # once per row as ``score``; ``ranked`` reuses that alias for both the
    # per-document window cap and the final ordering. The previous single-CTE
    # form computed ``ts_rank`` twice (score column + window ORDER BY) and bound
    # ``to_tsquery`` three times. The ``@@`` predicate is deliberately kept as a
    # direct inline ``to_tsquery(...)`` expression (not hoisted into a CTE) so
    # the GIN ``chunks_tsv_idx`` Bitmap Index Scan plan is provably unchanged.
    # The per-doc cap keeps the top PER_DOC_CHUNK_CAP chunks per ``document_id``
    # before the global LIMIT so one long doc can't fill the candidate slot.
    fts_sql = f"""
        WITH base AS (
            SELECT c.id, c.document_id, c.chunk_index, c.content,
                   ts_rank(c.tsv, to_tsquery('english', %s)) AS score
            FROM chunks c
            {join_clause}
            WHERE c.tsv @@ to_tsquery('english', %s){fts_filter}
        ),
        ranked AS (
            SELECT id, document_id, chunk_index, content, score,
                   ROW_NUMBER() OVER (
                       PARTITION BY document_id ORDER BY score DESC
                   ) AS rn
            FROM base
        )
        SELECT id, document_id, chunk_index, content, score
        FROM ranked
        WHERE rn <= {PER_DOC_CHUNK_CAP}
        ORDER BY score DESC
        LIMIT {CANDIDATE_LIMIT}
    """
    fts_rows = conn.execute(
        fts_sql, [tsquery, tsquery, *where_params], prepare=prepare_flag
    ).fetchall()

    # Surface the lexical-leg hit count to an opt-in caller (no extra query —
    # ``fts_rows`` is already materialized). ``fts_count == 0`` means the corpus
    # has no lexical trace of the query, which is the knowledge-gap signal that
    # the vector leg (always returns nearest neighbours) would otherwise mask.
    if diagnostics is not None:
        diagnostics.fts_count = len(fts_rows)

    vec_rows: list[Any] = []
    if not fts_only:
        q_emb = _query_embed(embedder, query)
        floor_pred = "1 - (c.embedding <=> %s::vector) >= %s"
        vec_params: list[Any]
        if has_filters:
            vec_where = f"WHERE {where_sql} AND {floor_pred}"
            vec_params = [q_emb, *where_params, q_emb, vector_sim_floor, q_emb]
        else:
            vec_where = f"WHERE {floor_pred}"
            vec_params = [q_emb, q_emb, vector_sim_floor, q_emb]
        vec_sql = f"""
            SELECT c.id, c.document_id, c.chunk_index, c.content,
                   1 - (c.embedding <=> %s::vector) AS score
            FROM chunks c
            {join_clause}
            {vec_where}
            ORDER BY c.embedding <=> %s::vector
            LIMIT {CANDIDATE_LIMIT}
        """
        vec_rows = conn.execute(
            vec_sql, vec_params, prepare=prepare_flag
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
        contrib = rrf_contribution(rank, k=RRF_K)
        rrf[cid] = rrf.get(cid, 0.0) + contrib
        if explain:
            rrf_fts[cid] = contrib
        chunk_meta[cid] = (str(row[1]), int(row[2]), row[3])
    for rank, row in enumerate(vec_rows):
        cid = str(row[0])
        contrib = rrf_contribution(rank, k=RRF_K)
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
        meta = docs.get(doc_id)
        if meta is None:
            # The document was deleted (e.g. `brain rm`) between the
            # chunk-ranking queries and the per-document metadata fetch above;
            # its chunk rows can still linger in ``by_doc``. Skip the now-
            # orphaned doc instead of KeyError-ing the whole search. Task 2.2.
            continue

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
    # Defensive floor. A non-positive ``limit`` reaching this far is a caller
    # bug — the CLI (Typer ``min=1``) and MCP (INVALID_PARAMS) boundaries reject
    # it first. Clamp to 1 so a stray negative never silently slices the tail
    # off the ranked list (``results[:-3]`` would drop the 3 lowest-ranked docs
    # and quietly return wrong data). Task 2.10.
    effective_limit = max(1, limit)
    return results[:effective_limit]


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
