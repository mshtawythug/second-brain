"""Gap queue builder — run detectors, apply guards, upsert into elicitation_gaps."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from brain.config import Config
from brain.elicit.detectors import DETECTOR_REGISTRY, ContradictionDetector
from brain.elicit.schema import Gap
from brain.rank_fusion import rrf_contribution


@dataclass(frozen=True)
class BuildQueueResult:
    """Summary of a single build_queue run."""

    inserted: int
    updated: int
    skipped: int


def _passes_guards(gap: Gap, *, min_evidence_docs: int, min_gap_score: float) -> bool:
    """True when gap clears both the evidence-count and raw-score thresholds."""
    return len(gap.evidence_ids) >= min_evidence_docs and gap.score >= min_gap_score


def _upsert_gap(
    conn: psycopg.Connection[Any],
    gap: Gap,
    *,
    tenant_id: str,
    rrf_score: float,
) -> bool:
    """Upsert one gap into elicitation_gaps. Returns True when inserted, False when updated."""
    row = conn.execute(
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
        RETURNING (xmax = 0) AS was_inserted
        """,
        (
            tenant_id,
            gap.signal_kind,
            gap.target_type,
            gap.target_id,
            rrf_score,
            gap.evidence_ids,
            gap.rationale,
        ),
    ).fetchone()
    return bool(row and row[0])


def build_queue(
    conn: psycopg.Connection[Any],
    *,
    cfg: Config,
    tenant_id: str = "default",
    signal_kinds: list[str] | None = None,
) -> BuildQueueResult:
    """Run enabled detectors, apply guards, and upsert passing gaps into elicitation_gaps.

    For each detector, gaps are RRF-ranked by their position in the detector's
    ordered output list; the RRF score is stored in ``elicitation_gaps.score``
    so the ``brain elicit list`` queue is ordered by detection prominence.

    ``user_flagged`` is never batch-detected — it runs only on an explicit
    user-supplied target. ``contradiction`` is skipped when
    ``cfg.elicit_contradiction_enabled`` is False.
    """
    kinds: set[str] = set(signal_kinds) if signal_kinds is not None else set(DETECTOR_REGISTRY)
    kinds.discard("user_flagged")  # on-demand only, never batch

    inserted = updated = skipped = 0

    for kind in sorted(kinds):  # stable iteration order across runs
        if kind not in DETECTOR_REGISTRY:
            continue

        if kind == "contradiction":
            if not cfg.elicit_contradiction_enabled:
                continue
            detector: Any = ContradictionDetector(enabled=True)
        else:
            detector = DETECTOR_REGISTRY[kind]()

        # Overfetch so guards can filter without losing the top results.
        raw_gaps: list[Gap] = detector.detect(
            conn,
            tenant_id=tenant_id,
            limit=cfg.elicit_queue_limit * 4,
        )

        # Apply guards against the raw detector score.
        passing = [
            g
            for g in raw_gaps
            if _passes_guards(
                g,
                min_evidence_docs=cfg.elicit_min_evidence_docs,
                min_gap_score=cfg.elicit_min_gap_score,
            )
        ]
        skipped += len(raw_gaps) - len(passing)

        # Upsert up to elicit_queue_limit gaps, storing their RRF rank score.
        for rank, gap in enumerate(passing[: cfg.elicit_queue_limit]):
            was_inserted = _upsert_gap(
                conn,
                gap,
                tenant_id=tenant_id,
                rrf_score=rrf_contribution(rank),
            )
            if was_inserted:
                inserted += 1
            else:
                updated += 1

    return BuildQueueResult(inserted=inserted, updated=updated, skipped=skipped)
