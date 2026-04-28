"""Hybrid search: FTS + vector via Reciprocal Rank Fusion."""
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
) -> list[SearchResult]:
    """Combine FTS and vector ranks via Reciprocal Rank Fusion.

    Each chunk receives ``1 / (K + rank)`` from each ranker it appears in
    (K=60). Per-document scores are the max across that document's chunks,
    and the highest-scoring chunk per document becomes the returned snippet.

    When ``fts_only`` is True, the vector leg (and the Ollama embed call) is
    skipped — useful when the embedding service is unavailable.
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

    fts_sql = f"""
        SELECT c.id, c.document_id, c.content,
               ts_rank(c.tsv, plainto_tsquery('english', %s)) AS score
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.tsv @@ plainto_tsquery('english', %s) AND {where_sql}
        ORDER BY score DESC
        LIMIT {CANDIDATE_LIMIT}
    """
    fts_rows = conn.execute(fts_sql, [query, query, *where_params]).fetchall()

    vec_rows: list[Any] = []
    if not fts_only:
        q_emb = embedder.embed([query], input_type="query")[0]
        vec_sql = f"""
            SELECT c.id, c.document_id, c.content,
                   1 - (c.embedding <=> %s::vector) AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {where_sql}
            ORDER BY c.embedding <=> %s::vector
            LIMIT {CANDIDATE_LIMIT}
        """
        vec_rows = conn.execute(vec_sql, [q_emb, *where_params, q_emb]).fetchall()

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
