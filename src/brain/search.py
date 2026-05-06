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
from typing import Any

import psycopg

from .ingest import Embedder


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
    where_sql = " AND ".join(where_clauses)

    tsquery = _build_tsquery(conn, query)

    # Per-doc cap CTE — keep the top PER_DOC_CHUNK_CAP chunks per
    # ``document_id`` (ranked by ts_rank) before the global LIMIT, so one
    # long doc can't fill the entire candidate slot.
    fts_sql = f"""
        WITH ranked AS (
            SELECT c.id, c.document_id, c.content,
                   ts_rank(c.tsv, to_tsquery('english', %s)) AS score,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.document_id
                       ORDER BY ts_rank(c.tsv, to_tsquery('english', %s)) DESC
                   ) AS rn
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.tsv @@ to_tsquery('english', %s) AND {where_sql}
        )
        SELECT id, document_id, content, score
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
            SELECT c.id, c.document_id, c.content,
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

    rrf: dict[str, float] = {}
    chunk_meta: dict[str, tuple[str, str]] = {}  # chunk_id → (document_id, content)
    for rank, row in enumerate(fts_rows):
        cid = str(row[0])
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        chunk_meta[cid] = (str(row[1]), row[2])
    for rank, row in enumerate(vec_rows):
        cid = str(row[0])
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        chunk_meta[cid] = (str(row[1]), row[2])

    by_doc: dict[str, tuple[float, str]] = {}  # document_id → (best_score, snippet)
    for cid, score in rrf.items():
        doc_id, content = chunk_meta[cid]
        prev = by_doc.get(doc_id)
        if prev is None or score > prev[0]:
            by_doc[doc_id] = (score, content)

    if not by_doc:
        return []

    doc_ids = list(by_doc.keys())
    doc_rows = conn.execute(
        """
        SELECT d.id, d.title, d.content_type, d.tags, s.kind
        FROM documents d
        LEFT JOIN sources s ON s.id = d.source_id
        WHERE d.id = ANY(%s)
        """,
        (doc_ids,),
    ).fetchall()
    docs = {str(r[0]): r for r in doc_rows}

    results: list[SearchResult] = []
    for doc_id, (score, snippet) in by_doc.items():
        meta = docs[doc_id]
        results.append(
            SearchResult(
                document_id=doc_id,
                title=meta[1],
                content_type=meta[2],
                tags=list(meta[3] or []),
                source_kind=meta[4],
                snippet=snippet[:SNIPPET_LENGTH],
                score=score,
            )
        )
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]
