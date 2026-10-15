"""Candidate legs (FTS + vector) for the related-docs ranking in :mod:`brain.related`.

Extracted from ``related.py``, which was over the 800-line ceiling, in the change
that fixed the vector leg (``docs/specs/2026-10-15-related-connect-vector-leg-design.md``).
The FTS leg moved unchanged.

**The vector leg uses design D**, the shape :mod:`brain.search_vector_leg` uses
(see that module for the full argument). The old query put every filter, the F6
confidential predicate and the cosine floor in one ``WHERE`` under
``ORDER BY embedding <=> q LIMIT 50`` with pgvector's default settings. The HNSW
index served it and handed over at most ``hnsw.ef_search`` (40) rows, and the
filters applied afterwards. On the live corpus (2026-10-15) it returned 25-39
candidates where 50 existed, for a recall of 0.69, and a confidential chunk the
gate dropped had still used up one of the 40 slots. Now:

- the eligibility filters **and the F6 predicate** sit inside a ``MATERIALIZED``
  CTE, so the iterative scan walks until ``CANDIDATE_LIMIT`` *eligible* chunks
  are found and a confidential chunk never reaches the outer query;
- the cosine floor sits outside, written ``dist <= 1 - floor``. Inside, an
  off-corpus source would walk the index up to ``hnsw.max_scan_tuples``, and the
  ``<=`` form rejects a zero-norm (NaN-distance) row;
- the outer query re-sorts by ``dist + 0, id``, because ``relaxed_order`` may
  return rows slightly out of order and the ``id`` makes the order total;
- the query runs under :data:`brain.hnsw_scan.RELAXED_SCAN_SETTINGS` (measured
  recall 1.0 over 60 live sources).
"""
from __future__ import annotations

from typing import Any

import psycopg

from .hnsw_scan import RELAXED_SCAN_SETTINGS, ScanSettings, scoped_settings
from .search import CANDIDATE_LIMIT, PER_DOC_CHUNK_CAP
from .sensitivity import not_confidential_sql

#: Settings applied around the vector leg's query. Looked up at call time, so a
#: test can substitute a lower ``ef_search`` to recreate the live corpus's regime.
_SCAN_SETTINGS: ScanSettings = RELAXED_SCAN_SETTINGS


def confidential_clause(exclude_confidential: bool) -> str:
    """``AND <predicate>`` for the candidate legs and source eligibility, or ``""``.

    A frozen literal rather than a bound ``%s`` because the legs bind their
    other parameters POSITIONALLY: a conditional extra parameter must be
    inserted at the right index by every caller or silently bind the wrong
    value, and the wrong direction here only ADDS rows, so no test would see
    it. Same reasoning :func:`brain.sensitivity.not_confidential_sql` records.
    """
    if not exclude_confidential:
        return ""
    return f"AND {not_confidential_sql('d')}"


def fts_candidates(
    conn: psycopg.Connection[Any],
    *,
    tsquery: str,
    exclude_doc_id: str,
    exclude_confidential: bool,
) -> list[tuple[str, str, str]]:
    """Return up to ``CANDIDATE_LIMIT`` (chunk_id, document_id, content) rows
    from the per-doc-capped FTS leg. Empty list when ``tsquery`` is empty.

    Mirrors the per-doc-cap CTE in :func:`brain.search.hybrid_search` but
    with the eligibility filters from the related-docs pipeline (non-draft,
    non-NULL ``vault_path``) and an explicit self-exclusion.
    """
    if not tsquery:
        return []
    # ts_rank weights array (Postgres float4[] order is ``{D, C, B, A}``).
    # Override the runtime defaults ``{0.1, 0.2, 0.4, 1.0}`` to de-emphasize
    # title (A) and promote body content (C). Migration 009's chunks.tsv
    # weighting is A=title_text, B=tags_text, C=content+search_extras,
    # so A=0.1 keeps short commodity-token title matches like "min" or
    # "refer" from outranking distinctive body matches like "person-b" or
    # "topic-ih"; C=0.8 ensures title-form distinctive tokens reach the
    # candidate set even when they only appear in body content.
    # Phase F.D — see ``docs/plans/2026-05-06-related-docs-rebuild.md``.
    sql = f"""
        WITH ranked AS (
            SELECT c.id::text AS id,
                   c.document_id::text AS document_id,
                   c.content AS content,
                   ts_rank(
                       '{{0.05, 0.8, 0.3, 0.1}}'::float4[],
                       c.tsv,
                       %s::tsquery
                   ) AS score,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.document_id
                       ORDER BY ts_rank(
                           '{{0.05, 0.8, 0.3, 0.1}}'::float4[],
                           c.tsv,
                           %s::tsquery
                       ) DESC
                   ) AS rn
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.tsv @@ %s::tsquery
              AND d.draft = FALSE
              AND d.vault_path IS NOT NULL
              AND d.id <> %s::uuid
              {confidential_clause(exclude_confidential)}
        )
        SELECT id, document_id, content
        FROM ranked
        WHERE rn <= {PER_DOC_CHUNK_CAP}
        ORDER BY score DESC
        LIMIT {CANDIDATE_LIMIT}
    """
    rows = conn.execute(sql, (tsquery, tsquery, tsquery, exclude_doc_id)).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2] or "")) for r in rows]


def vector_candidates_sql(
    *,
    src_embedding: Any,
    exclude_doc_id: str,
    vector_sim_floor: float,
    exclude_confidential: bool,
) -> tuple[str, list[Any]]:
    """Return ``(sql, params)`` for the vector leg's candidate query.

    Rows are ``(chunk_id, document_id, content)``, best first. Every value is
    bound as a ``%s`` parameter; only the module constant ``CANDIDATE_LIMIT`` and
    the frozen F6 literal are formatted in. Run it via :func:`vector_candidates`,
    which applies the scan settings it relies on.
    """
    sql = f"""
        WITH nn AS MATERIALIZED (
            SELECT c.id, c.document_id, c.content,
                   c.embedding <=> %s::vector AS dist
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.draft = FALSE
              AND d.vault_path IS NOT NULL
              AND d.id <> %s::uuid
              {confidential_clause(exclude_confidential)}
              AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> %s::vector
            LIMIT {CANDIDATE_LIMIT}
        )
        SELECT id::text, document_id::text, content
        FROM nn
        WHERE dist <= 1 - %s
        ORDER BY dist + 0, id"""
    return sql, [src_embedding, exclude_doc_id, src_embedding, vector_sim_floor]


def vector_candidates(
    conn: psycopg.Connection[Any],
    *,
    src_embedding: Any,
    exclude_doc_id: str,
    vector_sim_floor: float,
    exclude_confidential: bool,
) -> list[tuple[str, str, str]]:
    """Return up to ``CANDIDATE_LIMIT`` (chunk_id, document_id, content) rows
    from the cosine-floor-gated vector leg. Empty list when
    ``src_embedding`` is ``None``.
    """
    if src_embedding is None:
        return []
    sql, params = vector_candidates_sql(
        src_embedding=src_embedding,
        exclude_doc_id=exclude_doc_id,
        vector_sim_floor=vector_sim_floor,
        exclude_confidential=exclude_confidential,
    )
    with scoped_settings(conn, _SCAN_SETTINGS):
        rows = conn.execute(sql, params).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2] or "")) for r in rows]
