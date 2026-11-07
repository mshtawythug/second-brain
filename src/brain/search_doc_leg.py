"""The document-leg stage: re-score a search's rows by a whole-document vector (RM-20).

:class:`DocLegStage` is a :class:`brain.search_pipeline.SearchStage`. For the
first :data:`DOC_LEG_DEPTH` rows it reads each document's vector from
``document_embeddings`` (written by ``brain docembed backfill``, see
:mod:`brain.doc_embeddings`), ranks those rows by cosine to the query vector,
and fuses that rank with the pipeline's own by reciprocal rank
(:func:`doc_leg_order`). It ships **off** (``BRAIN_DOC_LEG=false``) and is wired
once, in :func:`brain.search_pipeline.build_stages`.

Four rulings shape it (plan ``docs/plans/2026-11-07-rm-20-document-embeddings.md``):

- **It re-scores; it adds no candidates** (spec RM-20.D1). It only reorders rows
  the search already returned, so every filter and the confidential exclusion
  hold by construction.
- **A row without a vector keeps its exact index** (D6): one not yet backfilled,
  one written by another embedder (D11), or a confidential one under a hosted
  embedder. It is neither promoted nor demoted.
- **The query vector is the one the search computed**: a hit on
  :func:`brain.search.cached_query_embedding`, so the stage costs no second
  embed. It declares ``needs_query_vector`` so the pipeline skips it after a
  text-only search instead of letting it embed (D5).
- **``SearchResult.score`` is not overwritten** (D7, RM-14a.D1's precedent).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg

from .ingest import Embedder
from .rank_fusion import rrf_contribution
from .search import (
    CANDIDATE_LIMIT,
    RRF_K,
    SearchResult,
    cached_query_embedding,
    embedder_identity,
)
from .search_pipeline import StageOutcome, StageReport

#: The stage's name in ``StageReport``s, the ``stages`` key and timings.
DOC_LEG_STAGE_NAME = "doc_leg"

#: How deep the stage looks: every row a ranking leg can contribute (D8). To lift
#: a document from rank 30 the stage has to see rank 30.
DOC_LEG_DEPTH = CANDIDATE_LIMIT

#: The ``skipped_reason`` when no candidate has a vector (D12): an un-backfilled
#: corpus must read as "skipped", never as a measurement of the leg.
NO_DOCUMENT_VECTORS = "no_document_vectors"

# No sensitivity predicate, deliberately: it reads vectors only for ids the
# (gated) search already returned, returns no content, and the cosine is
# computed in Postgres. A row written by another embedder is not comparable,
# so it is filtered out as if absent (D11).
_COSINES_SQL = """
    SELECT document_id::text, 1 - (embedding <=> %s::vector)
    FROM document_embeddings
    WHERE document_id = ANY(%s::uuid[]) AND embedder = %s AND embedding IS NOT NULL
"""


def doc_leg_order(n: int, cosines: Mapping[int, float], *, weight: float) -> list[int]:
    """The new order of ``n`` rows, as pipeline indices.

    ``cosines`` maps a row's pipeline index to its document's cosine; a row
    absent from it keeps its index. The others are ranked by cosine (descending,
    ties in pipeline order) and re-sorted, within their own slots, by
    ``rrf(index) + weight * rrf(cosine rank)``. Both sorts are stable, so equal
    scores keep the pipeline order.
    """
    slots = sorted(i for i in cosines if 0 <= i < n)
    by_cosine = sorted(slots, key=lambda i: -cosines[i])
    doc_rank = {index: rank for rank, index in enumerate(by_cosine)}
    fused = {
        i: rrf_contribution(i, k=RRF_K) + weight * rrf_contribution(doc_rank[i], k=RRF_K)
        for i in slots
    }
    ranked = sorted(slots, key=lambda i: -fused[i])
    order = list(range(n))
    for slot, source in zip(slots, ranked, strict=True):
        order[slot] = source
    return order


def _cosines(
    conn: psycopg.Connection[Any],
    query_vector: list[float],
    document_ids: list[str],
    identity: str,
) -> dict[str, float]:
    """Each document's cosine to ``query_vector``: one statement, by primary key."""
    rows = conn.execute(_COSINES_SQL, (query_vector, document_ids, identity)).fetchall()
    return {str(row[0]): float(row[1]) for row in rows}


@dataclass(frozen=True)
class DocLegStage:
    """Re-scores the first :data:`DOC_LEG_DEPTH` rows by their document vectors."""

    embedder: Embedder
    weight: float
    name: str = DOC_LEG_STAGE_NAME
    #: Read by :func:`brain.search_pipeline.stage_needs_query_vector` (D5).
    needs_query_vector: bool = True

    def candidate_limit(self, limit: int) -> int:
        return max(limit, DOC_LEG_DEPTH)

    def signature(self) -> dict[str, Any]:
        return {"doc_leg_weight": self.weight}

    def apply(
        self,
        conn: psycopg.Connection[Any],
        *,
        query: str,
        session_id: str | None,
        results: list[SearchResult],
    ) -> StageOutcome:
        del session_id  # the leg scores the query, not the session
        head, tail = results[:DOC_LEG_DEPTH], results[DOC_LEG_DEPTH:]
        if not head:
            return self._outcome(results, applied=True)
        cosines = _cosines(
            conn,
            cached_query_embedding(self.embedder, query),
            [r.document_id for r in head],
            embedder_identity(self.embedder),
        )
        by_index = {
            i: cosines[r.document_id] for i, r in enumerate(head) if r.document_id in cosines
        }
        if not by_index:
            return self._outcome(results, applied=False, skipped_reason=NO_DOCUMENT_VECTORS)
        order = doc_leg_order(len(head), by_index, weight=self.weight)
        return self._outcome([head[i] for i in order] + tail, applied=True)

    def _outcome(
        self, results: list[SearchResult], *, applied: bool, skipped_reason: str | None = None
    ) -> StageOutcome:
        # ``ms`` is measured by ``apply_stages``, which times every stage alike.
        report = StageReport(
            name=self.name, applied=applied, skipped_reason=skipped_reason, ms=0.0
        )
        return StageOutcome(results=results, report=report)
