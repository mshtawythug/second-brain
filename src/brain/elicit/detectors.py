"""Pluggable tacit-knowledge gap detectors (Open/Closed)."""
from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

import psycopg

from .schema import Gap


@runtime_checkable
class GapDetector(Protocol):
    signal_kind: str

    def detect(
        self, conn: psycopg.Connection[Any], *, tenant_id: str, limit: int
    ) -> list[Gap]: ...


class DeltaDetector:
    """Entities referenced only in ingested docs, never in an authored vault note."""

    signal_kind = "delta"

    def detect(
        self, conn: psycopg.Connection[Any], *, tenant_id: str, limit: int
    ) -> list[Gap]:
        rows = conn.execute(
            """
            SELECT e.id::text, e.entity_type, e.name,
                   array_agg(DISTINCT m.document_id::text) AS evidence_ids,
                   count(DISTINCT m.document_id) AS n
            FROM graph_entities e
            JOIN graph_entity_mentions m
              ON m.tenant_id = e.tenant_id AND m.entity_id = e.id
            JOIN documents d ON d.id = m.document_id
            WHERE e.tenant_id = %s
            GROUP BY e.id, e.entity_type, e.name
            HAVING bool_and(d.kind = 'ingested')
            ORDER BY n DESC
            LIMIT %s
            """,
            (tenant_id, limit),
        ).fetchall()
        return [
            Gap(
                gap_id=str(uuid.uuid4()),
                signal_kind="delta",
                target_type=etype,
                target_id=eid,
                score=float(n),
                evidence_ids=list(ev),
                rationale=(
                    f"{name} is referenced in {n} ingested doc(s) but never authored in a note."
                ),
            )
            for (eid, etype, name, ev, n) in rows
        ]


class OrphanEntityDetector:
    """High-mention entities with no description — the graph knows who, not why."""

    signal_kind = "orphan"

    def detect(
        self, conn: psycopg.Connection[Any], *, tenant_id: str, limit: int
    ) -> list[Gap]:
        rows = conn.execute(
            """
            SELECT e.id::text, e.entity_type, e.name, e.doc_count,
                   array_agg(DISTINCT m.document_id::text) AS evidence_ids
            FROM graph_entities e
            JOIN graph_entity_mentions m
              ON m.tenant_id = e.tenant_id AND m.entity_id = e.id
            WHERE e.tenant_id = %s
              AND (e.description IS NULL OR length(trim(e.description)) = 0)
            GROUP BY e.id, e.entity_type, e.name, e.doc_count
            ORDER BY e.doc_count DESC
            LIMIT %s
            """,
            (tenant_id, limit),
        ).fetchall()
        return [
            Gap(
                gap_id=str(uuid.uuid4()),
                signal_kind="orphan",
                target_type=etype,
                target_id=eid,
                score=float(dc),
                evidence_ids=list(ev),
                rationale=f"{name} appears in {dc} doc(s) but has no written description.",
            )
            for (eid, etype, name, dc, ev) in rows
        ]


class ContradictionDetector:
    """Stub until Wave 4 — returns [] unless ELICIT_CONTRADICTION_ENABLED."""

    signal_kind = "contradiction"

    def __init__(self, *, enabled: bool = False, enricher: Any | None = None) -> None:
        self._enabled = enabled
        self._enricher = enricher

    def detect(
        self, conn: psycopg.Connection[Any], *, tenant_id: str, limit: int
    ) -> list[Gap]:
        return []  # Wave 4 implements the enabled path


class UserFlaggedDetector:
    """Wrap a user-supplied target into a single Gap (resolve to a graph entity if possible)."""

    signal_kind = "user_flagged"

    def __init__(self, *, target: str) -> None:
        self._target = target

    def detect(
        self, conn: psycopg.Connection[Any], *, tenant_id: str, limit: int
    ) -> list[Gap]:
        row = conn.execute(
            """
            SELECT e.id::text, e.entity_type, e.name,
                   coalesce(array_agg(DISTINCT m.document_id::text)
                            FILTER (WHERE m.document_id IS NOT NULL), '{}') AS ev
            FROM graph_entities e
            LEFT JOIN graph_entity_mentions m
              ON m.tenant_id = e.tenant_id AND m.entity_id = e.id
            WHERE e.tenant_id = %s AND lower(e.name) = lower(%s)
            GROUP BY e.id, e.entity_type, e.name
            LIMIT 1
            """,
            (tenant_id, self._target),
        ).fetchone()
        if row is not None:
            eid, etype, name, ev = row
            return [
                Gap(
                    gap_id=str(uuid.uuid4()),
                    signal_kind="user_flagged",
                    target_type=etype,
                    target_id=eid,
                    score=1.0,
                    evidence_ids=list(ev),
                    rationale=f"User asked to elicit knowledge about {name}.",
                )
            ]
        return [
            Gap(
                gap_id=str(uuid.uuid4()),
                signal_kind="user_flagged",
                target_type="topic",
                target_id=self._target,
                score=1.0,
                evidence_ids=[],
                rationale=f"User asked to elicit knowledge about '{self._target}'.",
            )
        ]


DETECTOR_REGISTRY: dict[str, type] = {
    "delta": DeltaDetector,
    "orphan": OrphanEntityDetector,
    "contradiction": ContradictionDetector,
    "user_flagged": UserFlaggedDetector,
}
