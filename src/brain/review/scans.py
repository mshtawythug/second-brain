"""Contradiction + staleness scan orchestrators (Plan 03 — ``brain review scan``).

One reason to change: the scan algorithm. The SQL lives in
:mod:`brain.review.queries`; this module owns the pure-Python pipeline —
graph prefilter -> embedding prefilter -> LLM adjudication (conflicts only) ->
upsert. Logs entity names and doc ids at INFO level only; never summaries or
document bodies (privacy, CLAUDE.md security standards).
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import psycopg

from ..config import Config
from ..enrichment import ContradictionVerdict
from ..errors import OllamaUnavailable, ReviewError
from ..ingest import Embedder
from . import queries

_logger = logging.getLogger(__name__)


@runtime_checkable
class ContradictionAssessor(Protocol):
    """The single enricher method the conflict scan depends on (DIP).

    :class:`brain.enrichment.OllamaEnricher` satisfies this; the MCP layer wraps
    it with a call-counter, and tests pass a fake — all without importing the
    concrete enricher here.
    """

    def assess_contradiction(
        self, *, subject: str, summaries: list[str]
    ) -> ContradictionVerdict: ...

# Embedding prefilter cap: at most this many of an entity's documents are
# pairwise-compared (Step 3). Bounds the pair count to C(10, 2) = 45 before the
# per-entity ``pairs_per_entity`` cap trims it further.
_MAX_DOCS_PER_ENTITY = 10


@dataclass(frozen=True)
class ReviewFinding:
    """One contradiction or staleness finding produced by a scan.

    ``kind`` is the ``elicitation_gaps.signal_kind`` value
    (``'contradiction'`` | ``'stale'``). ``target_type`` is the entity type for
    conflicts and ``'doc'`` for staleness. ``target_id`` is the entity
    ``canonical_key`` (conflicts) or the stale document id (staleness).
    ``score`` is ``1.0`` for a confirmed conflict and the cosine similarity for
    a stale finding. ``evidence_ids`` are the conflicting / superseded document
    ids. Frozen — a finding is an immutable value object.
    """

    kind: str
    target_type: str
    target_id: str
    score: float
    rationale: str
    evidence_ids: list[str]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors in pure Python (no numpy).

    Returns ``0.0`` when either vector is all-zero (degenerate, no direction) so
    the caller never divides by zero. Identical vectors -> ``1.0``; orthogonal
    -> ``0.0``.
    """
    # strict=False: vectors are same-dim in practice; tolerate drift over the
    # overlap rather than raising mid-scan.
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _top_pairs(
    doc_ids: Sequence[str],
    embeddings: Mapping[str, Sequence[float]],
    *,
    sim_floor: float,
    max_pairs: int,
) -> list[tuple[str, str, float]]:
    """Top ``max_pairs`` document pairs by cosine, keeping only pairs >= floor.

    Only documents present in ``embeddings`` are paired (a missing embedding
    can't be compared). Pairs below ``sim_floor`` are discarded — too topically
    distant to possibly contradict. Returns ``(doc_a, doc_b, cosine)`` sorted by
    cosine DESC. Pure logic; unit-tested without a DB.
    """
    usable = [d for d in doc_ids if d in embeddings]
    pairs: list[tuple[str, str, float]] = []
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            doc_a, doc_b = usable[i], usable[j]
            sim = _cosine(embeddings[doc_a], embeddings[doc_b])
            if sim >= sim_floor:
                pairs.append((doc_a, doc_b, sim))
    pairs.sort(key=lambda p: p[2], reverse=True)
    return pairs[:max_pairs]


def run_conflict_scan(
    conn: psycopg.Connection[Any],
    enricher: ContradictionAssessor,
    embedder: Embedder,
    cfg: Config,
    *,
    tenant_id: str,
    dry_run: bool = False,
) -> list[ReviewFinding]:
    """Detect entities whose document summaries express contradictory positions.

    Pipeline (spec §3): graph prefilter (entities with >= min_docs summarized
    docs) -> idempotency skip (never re-adjudicate or overwrite an already
    surfaced / snoozed / dismissed finding) -> embedding prefilter (top pairs
    above the cosine floor) -> LLM adjudication via
    :meth:`OllamaEnricher.assess_contradiction` -> upsert. At most one finding
    per entity (the ``canonical_key`` is the unique target). When ``dry_run`` is
    true the findings are computed and returned but never written.

    ``embedder`` is part of the documented scan signature for symmetry with the
    ingest / search layer; the scan compares pre-stored lead-chunk embeddings,
    so it never calls the embedder to vectorize new text.

    Raises :class:`ReviewError` (carrying the findings written so far and the
    processed / total counts) when Ollama becomes unreachable mid-scan — earlier
    findings are committed before the error propagates.
    """
    del embedder  # documented-but-unused; see docstring.
    candidates = queries.iter_entities_for_conflict_scan(
        conn,
        tenant_id=tenant_id,
        min_docs=cfg.elicit_contradiction_min_docs,
        limit=cfg.review_conflict_limit,
    )
    existing = queries.existing_finding_statuses(
        conn, tenant_id=tenant_id, signal_kind="contradiction"
    )
    findings: list[ReviewFinding] = []
    total = len(candidates)
    processed = 0
    for cand in candidates:
        # Idempotency: any non-resolved row (surfaced / snoozed / dismissed)
        # means we never re-adjudicate or overwrite. Only resolved / absent
        # targets are rescanned.
        if cand.canonical_key in existing:
            continue
        docs = cand.doc_ids[:_MAX_DOCS_PER_ENTITY]
        embeddings = queries.fetch_best_chunk_embeddings(conn, document_ids=docs)
        pairs = _top_pairs(
            docs,
            embeddings,
            sim_floor=cfg.review_embed_sim_floor,
            max_pairs=cfg.review_conflict_pairs_per_entity,
        )
        if pairs:
            summaries = queries.fetch_doc_summaries(conn, document_ids=docs)
            _logger.info(
                "conflict scan: adjudicating entity %r (%d candidate pair(s))",
                cand.name,
                len(pairs),
            )
            finding = _adjudicate_entity(
                conn,
                enricher,
                cand,
                pairs,
                summaries,
                tenant_id=tenant_id,
                dry_run=dry_run,
                findings=findings,
                processed=processed,
                total=total,
            )
            if finding is not None:
                findings.append(finding)
        processed += 1
    return findings


def _adjudicate_entity(
    conn: psycopg.Connection[Any],
    enricher: ContradictionAssessor,
    cand: queries.EntityCandidate,
    pairs: Sequence[tuple[str, str, float]],
    summaries: Mapping[str, str],
    *,
    tenant_id: str,
    dry_run: bool,
    findings: list[ReviewFinding],
    processed: int,
    total: int,
) -> ReviewFinding | None:
    """Run the LLM on each surviving pair; return the first confirmed conflict.

    Returns ``None`` when no pair is judged contradictory. On
    :class:`OllamaUnavailable` commits the findings written so far (unless
    ``dry_run``) and raises :class:`ReviewError` with the partial result.
    """
    for doc_a, doc_b, _sim in pairs:
        summary_a = summaries.get(doc_a)
        summary_b = summaries.get(doc_b)
        if not summary_a or not summary_b:
            continue
        try:
            verdict = enricher.assess_contradiction(
                subject=cand.name, summaries=[summary_a, summary_b]
            )
        except OllamaUnavailable as exc:
            if not dry_run:
                conn.commit()
            raise ReviewError(
                f"Ollama unavailable mid-scan: {exc}",
                findings=findings,
                processed=processed,
                total=total,
            ) from exc
        if verdict.contradicts:
            if not dry_run:
                queries.upsert_review_finding(
                    conn,
                    tenant_id=tenant_id,
                    signal_kind="contradiction",
                    target_type=cand.entity_type,
                    target_id=cand.canonical_key,
                    score=1.0,
                    evidence_ids=[doc_a, doc_b],
                    rationale=verdict.rationale,
                )
            return ReviewFinding(
                kind="contradiction",
                target_type=cand.entity_type,
                target_id=cand.canonical_key,
                score=1.0,
                rationale=verdict.rationale,
                evidence_ids=[doc_a, doc_b],
            )
    return None


def run_staleness_scan(
    conn: psycopg.Connection[Any],
    embedder: Embedder,
    cfg: Config,
    *,
    tenant_id: str,
    dry_run: bool = False,
) -> list[ReviewFinding]:
    """Flag aged docs superseded by a newer doc sharing an entity (no LLM calls).

    Pipeline (spec §3): age candidates (older than ``stale_age_days``,
    summarized, non-transcript, non-draft) -> newer docs sharing an entity
    within ``stale_supersede_window_days`` -> embedding similarity filter (keep
    the best superseding doc with cosine >= ``stale_sim_floor``) -> idempotency
    skip -> upsert. The score is the cosine similarity. When ``dry_run`` is true
    the findings are computed and returned but never written.

    ``embedder`` is part of the documented scan signature for symmetry; the scan
    compares pre-stored lead-chunk embeddings, so it never vectorizes new text.
    """
    del embedder  # documented-but-unused; see docstring.
    candidates = queries.iter_docs_for_staleness_scan(
        conn,
        stale_age_days=cfg.review_stale_age_days,
        limit=cfg.review_stale_limit,
    )
    existing = queries.existing_finding_statuses(
        conn, tenant_id=tenant_id, signal_kind="stale"
    )
    findings: list[ReviewFinding] = []
    for cand in candidates:
        if cand.doc_id in existing:
            continue
        newer = queries.fetch_superseding_docs(
            conn,
            tenant_id=tenant_id,
            doc_id=cand.doc_id,
            window_days=cfg.review_stale_supersede_window_days,
        )
        if not newer:
            continue
        best = _best_superseding(conn, cand, newer, sim_floor=cfg.review_stale_sim_floor)
        if best is None:
            continue
        superseding, sim = best
        rationale = (
            f"Age: {cand.age_days} days. "
            f"Superseded by: '{superseding.title}' (similarity {sim:.2f})"
        )
        _logger.info(
            "stale scan: doc %s superseded by %s (cosine %.2f)",
            cand.doc_id,
            superseding.doc_id,
            sim,
        )
        if not dry_run:
            queries.upsert_review_finding(
                conn,
                tenant_id=tenant_id,
                signal_kind="stale",
                target_type="doc",
                target_id=cand.doc_id,
                score=sim,
                evidence_ids=[cand.doc_id, superseding.doc_id],
                rationale=rationale,
            )
        findings.append(
            ReviewFinding(
                kind="stale",
                target_type="doc",
                target_id=cand.doc_id,
                score=sim,
                rationale=rationale,
                evidence_ids=[cand.doc_id, superseding.doc_id],
            )
        )
    return findings


def _best_superseding(
    conn: psycopg.Connection[Any],
    cand: queries.StaleCandidate,
    newer: Sequence[queries.SupersedingDoc],
    *,
    sim_floor: float,
) -> tuple[queries.SupersedingDoc, float] | None:
    """Most-similar superseding doc with cosine >= ``sim_floor`` (or ``None``).

    Fetches the stale doc's lead-chunk embedding alongside every newer doc's in
    one batched query, then ranks by cosine.
    """
    ids = [cand.doc_id, *(n.doc_id for n in newer)]
    embeddings = queries.fetch_best_chunk_embeddings(conn, document_ids=ids)
    old_emb = embeddings.get(cand.doc_id)
    if old_emb is None:
        return None
    best: tuple[queries.SupersedingDoc, float] | None = None
    for superseding in newer:
        new_emb = embeddings.get(superseding.doc_id)
        if new_emb is None:
            continue
        sim = _cosine(old_emb, new_emb)
        if sim >= sim_floor and (best is None or sim > best[1]):
            best = (superseding, sim)
    return best
