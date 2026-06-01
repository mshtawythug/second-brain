"""Build the ranked, de-duplicated elicitation gap queue."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import psycopg

from ..config import Config
from ..rank_fusion import rrf_contribution
from .detectors import GapDetector
from .schema import Gap


def _normalize(gaps: list[Gap]) -> list[Gap]:
    """Rescale gap scores so the maximum is 1.0; identity on empty list."""
    if not gaps:
        return []
    top = max(g.score for g in gaps) or 1.0
    return [replace(g, score=g.score / top) for g in gaps]


def _rank_gaps(by_signal: dict[str, list[Gap]]) -> list[Gap]:
    """Merge per-signal gap lists into one ranked list using Reciprocal Rank Fusion.

    Each signal list is independently score-normalised; then RRF contributions
    are accumulated per ``target_id`` across all signals. Targets that appear in
    multiple signal lists receive additive boosts. The returned list is ordered
    descending by the accumulated RRF score (also normalised to [0, 1]).
    """
    contrib: dict[str, float] = {}
    chosen: dict[str, Gap] = {}
    for gaps in by_signal.values():
        for rank, g in enumerate(
            sorted(_normalize(gaps), key=lambda x: x.score, reverse=True)
        ):
            contrib[g.target_id] = contrib.get(g.target_id, 0.0) + rrf_contribution(rank)
            if g.target_id not in chosen or len(g.evidence_ids) > len(
                chosen[g.target_id].evidence_ids
            ):
                chosen[g.target_id] = g
    ranked = [replace(chosen[t], score=c) for t, c in contrib.items()]
    top = max((g.score for g in ranked), default=1.0) or 1.0
    ranked = [replace(g, score=g.score / top) for g in ranked]
    return sorted(ranked, key=lambda g: g.score, reverse=True)


def build_queue(
    conn: psycopg.Connection[Any],
    *,
    cfg: Config,
    tenant_id: str,
    detectors: list[GapDetector],
    limit: int,
    include_low_confidence: bool = False,
) -> list[Gap]:
    """Run ``detectors``, upsert results, then return the filtered open queue.

    Detection results are RRF-merged across signal kinds, upserted into
    ``elicitation_gaps`` (with the partial-index conflict clause so resolved gaps
    are not overwritten), and then read back in score order with evidence and
    confidence filters applied.
    """
    by_signal: dict[str, list[Gap]] = {}
    for det in detectors:
        by_signal[det.signal_kind] = det.detect(
            conn, tenant_id=tenant_id, limit=cfg.elicit_queue_limit
        )
    ranked = _rank_gaps(by_signal)
    # Evidence guard: skip gaps with too few supporting docs (except user_flagged).
    ranked = [
        g
        for g in ranked
        if len(g.evidence_ids) >= cfg.elicit_min_evidence_docs
        or g.signal_kind == "user_flagged"
    ]
    # Upsert passing gaps; the partial index prevents overwriting resolved rows.
    for g in ranked:
        conn.execute(
            """
            INSERT INTO elicitation_gaps
                (tenant_id, signal_kind, target_type, target_id,
                 score, evidence_ids, rationale)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, signal_kind, target_id) WHERE status <> 'resolved'
            DO UPDATE SET
                score        = EXCLUDED.score,
                evidence_ids = EXCLUDED.evidence_ids,
                rationale    = EXCLUDED.rationale,
                updated_at   = now()
            """,
            (
                tenant_id,
                g.signal_kind,
                g.target_type,
                g.target_id,
                g.score,
                g.evidence_ids,
                g.rationale,
            ),
        )
    # Read back the open queue with score and confidence floors applied.
    floor = 0.0 if include_low_confidence else cfg.elicit_min_gap_score
    rows = conn.execute(
        """
        SELECT id::text, signal_kind, target_type, target_id,
               score, evidence_ids, rationale
        FROM elicitation_gaps
        WHERE tenant_id = %s
          AND status IN ('surfaced', 'snoozed')
          AND (snoozed_until IS NULL OR snoozed_until < now())
          AND score >= %s
        ORDER BY score DESC
        LIMIT %s
        """,
        (tenant_id, floor, limit),
    ).fetchall()
    return [
        Gap(
            gap_id=r[0],
            signal_kind=r[1],
            target_type=r[2],
            target_id=r[3],
            score=float(r[4]),
            evidence_ids=list(r[5]),
            evidence_texts=[],
            rationale=r[6],
        )
        for r in rows
    ]
