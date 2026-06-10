"""Build the ranked, de-duplicated elicitation gap queue."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import psycopg

from ..config import Config
from ..rank_fusion import rrf_contribution
from .detectors import GapDetector
from .schema import ENTITY_TARGET_TYPES, Gap

# A UUID-shaped ``target_id`` (entity-typed gaps reference a ``graph_entities``
# row by id). Guards the F2 purge so a ``user_flagged`` gap carrying a raw-string
# target (not a UUID) is never treated as an orphaned entity reference.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


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


def _purge_orphaned_entity_gaps(
    conn: psycopg.Connection[Any], tenant_id: str
) -> int:
    """Delete open entity-typed gaps whose ``graph_entities`` target is gone (F2).

    The cross-type collapse (:func:`brain.graph_rag.cross_type.collapse_cross_type_concepts`)
    merges duplicate entities and GCs the losers, leaving ``elicitation_gaps`` rows
    whose UUID ``target_id`` no longer resolves. This drops those phantom gaps so a
    stale entry never lingers in the queue. Scoped to:

    * ``status IN ('surfaced', 'snoozed')`` — the two active states the queue
      read-back surfaces; never touches user-``resolved`` rows (a snoozed gap whose
      entity was merged away would otherwise resurface as a phantom on expiry);
    * entity target types only (:data:`ENTITY_TARGET_TYPES`);
    * a UUID-shaped ``target_id`` (:data:`_UUID_RE`) — a ``user_flagged`` gap with a
      raw-string target is never mistaken for an orphaned entity reference;
    * ``NOT EXISTS`` a matching tenant-scoped ``graph_entities`` row.

    Returns the number of rows purged. Parameterized SQL.
    """
    cur = conn.execute(
        """
        DELETE FROM elicitation_gaps eg
        WHERE eg.tenant_id = %s
          AND eg.status IN ('surfaced', 'snoozed')
          AND eg.target_type = ANY(%s)
          AND eg.target_id ~ %s
          AND NOT EXISTS (
              SELECT 1 FROM graph_entities ge
              WHERE ge.tenant_id = eg.tenant_id AND ge.id::text = eg.target_id
          )
        """,
        (tenant_id, list(ENTITY_TARGET_TYPES), _UUID_RE),
    )
    return cur.rowcount


def build_queue(
    conn: psycopg.Connection[Any],
    *,
    cfg: Config,
    tenant_id: str,
    detectors: list[GapDetector],
    limit: int,
    include_low_confidence: bool = False,
    signal_kinds: Sequence[str] | None = None,
    target_ids: Sequence[str] | None = None,
    target_types: Sequence[str] | None = None,
) -> list[Gap]:
    """Run ``detectors``, upsert results, then return the filtered open queue.

    Detection results are RRF-merged across signal kinds, upserted into
    ``elicitation_gaps`` (with the partial-index conflict clause so resolved gaps
    are not overwritten), and then read back in score order with evidence and
    confidence filters applied.

    ``signal_kinds`` / ``target_ids`` / ``target_types`` optionally scope the
    read-back so the caller sees only the gaps it asked about. They restrict the
    SELECT, not the detectors: ``brain elicit --signal delta`` runs only the
    delta detector AND
    filters the read-back to ``signal_kind = 'delta'`` so unrelated pre-existing
    open gaps from prior runs don't leak into the session. ``None`` (the
    default) adds no predicate — ``brain elicit list`` and the unfiltered
    ``brain elicit`` still see the whole open queue.
    """
    # Discovery cap: honour an explicit caller ``limit`` larger than the config
    # default so ``brain elicit list --limit 100`` can surface up to 100 gaps,
    # while the unfiltered default still discovers ``cfg.elicit_queue_limit``.
    # The read-back ``LIMIT`` below stays the caller's ``limit`` exactly.
    discovery_limit = max(limit, cfg.elicit_queue_limit)
    by_signal: dict[str, list[Gap]] = {}
    for det in detectors:
        by_signal[det.signal_kind] = det.detect(
            conn, tenant_id=tenant_id, limit=discovery_limit
        )
    ranked = _rank_gaps(by_signal)
    # Evidence guard: skip gaps with too few supporting docs. ``user_flagged``
    # (an explicit user request) and ``search_failure`` (a repeated failed
    # search — by definition NO doc answers it, so ``evidence_ids`` is always
    # empty) are both exempt; the signal itself is the evidence.
    ranked = [
        g
        for g in ranked
        if len(g.evidence_ids) >= cfg.elicit_min_evidence_docs
        or g.signal_kind in ("user_flagged", "search_failure")
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
    # F2 self-heal: drop open gaps whose entity target was merged/deleted (e.g. by
    # the cross-type collapse) so a phantom gap never lingers in the queue.
    _purge_orphaned_entity_gaps(conn, tenant_id)
    # Read back the open queue with score and confidence floors applied, plus
    # the optional signal/target scoping (parameterized; static clause strings
    # only — no user values are interpolated into the SQL).
    floor = 0.0 if include_low_confidence else cfg.elicit_min_gap_score
    clauses = [
        "eg.tenant_id = %s",
        "eg.status IN ('surfaced', 'snoozed')",
        "(eg.snoozed_until IS NULL OR eg.snoozed_until < now())",
        "eg.score >= %s",
        # Mirror the pre-upsert evidence guard on read-back so previously
        # persisted sparse gaps can't resurface (user_flagged and
        # search_failure are exempt, exactly as in the upsert filter above).
        "(cardinality(eg.evidence_ids) >= %s "
        "OR eg.signal_kind IN ('user_flagged', 'search_failure'))",
    ]
    params: list[Any] = [tenant_id, floor, cfg.elicit_min_evidence_docs]
    if signal_kinds is not None:
        clauses.append("eg.signal_kind = ANY(%s)")
        params.append(list(signal_kinds))
    if target_ids is not None:
        clauses.append("eg.target_id = ANY(%s)")
        params.append(list(target_ids))
    if target_types is not None:
        clauses.append("eg.target_type = ANY(%s)")
        params.append(list(target_types))
    params.append(limit)
    # LEFT JOIN graph_entities on TEXT equality so a raw-string target_id
    # (e.g. a user_flagged gap whose target is not a UUID) never triggers a
    # uuid-cast error — it simply finds no match and target_name stays "".
    sql = (
        "SELECT eg.id::text, eg.signal_kind, eg.target_type, eg.target_id, "
        "eg.score, eg.evidence_ids, eg.rationale, ge.name "
        "FROM elicitation_gaps eg "
        "LEFT JOIN graph_entities ge "
        "ON ge.tenant_id = eg.tenant_id AND ge.id::text = eg.target_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY eg.score DESC LIMIT %s"
    )
    rows = conn.execute(sql, tuple(params)).fetchall()
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
            target_name=r[7] or "",
        )
        for r in rows
    ]
