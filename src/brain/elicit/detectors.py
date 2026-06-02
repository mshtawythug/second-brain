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
    """Detect entities whose document summaries express contradictory positions.

    One LLM call per qualifying entity (entities with ``doc_count >= min_docs``
    that have at least two non-null document summaries).  Returns an empty list
    when ``enabled=False`` or ``enricher`` is None — the latter case means the
    caller chose not to supply an LLM backend (e.g. ``brain elicit list`` when
    contradiction detection is off).
    """

    signal_kind = "contradiction"

    def __init__(
        self,
        *,
        enabled: bool = False,
        enricher: Any | None = None,
        min_docs: int = 5,
    ) -> None:
        self._enabled = enabled
        self._enricher = enricher
        self._min_docs = min_docs

    def detect(
        self, conn: psycopg.Connection[Any], *, tenant_id: str, limit: int
    ) -> list[Gap]:
        if not self._enabled or self._enricher is None:
            return []

        # Fetch entities that have enough docs AND at least 2 non-null summaries
        # among their mentioned documents.  We pass doc_count as an upper-bound
        # pre-filter; the HAVING clause enforces the exact non-null summary count.
        rows = conn.execute(
            """
            SELECT e.id::text, e.entity_type, e.name,
                   array_agg(d.id::text) AS doc_ids,
                   array_agg(d.summary)  AS summaries
            FROM graph_entities e
            JOIN graph_entity_mentions m
              ON m.tenant_id = e.tenant_id AND m.entity_id = e.id
            JOIN documents d ON d.id = m.document_id
            WHERE e.tenant_id = %s AND e.doc_count >= %s AND d.summary IS NOT NULL
            GROUP BY e.id, e.entity_type, e.name, e.doc_count
            HAVING count(d.summary) >= 2
            ORDER BY e.doc_count DESC
            LIMIT %s
            """,
            (tenant_id, self._min_docs, limit),
        ).fetchall()

        gaps: list[Gap] = []
        for eid, etype, name, doc_ids, summaries in rows:
            verdict = self._enricher.assess_contradiction(
                subject=name, summaries=list(summaries)
            )
            if verdict.contradicts:
                gaps.append(
                    Gap(
                        gap_id=str(uuid.uuid4()),
                        signal_kind="contradiction",
                        target_type=etype,
                        target_id=eid,
                        score=float(len(doc_ids)),
                        evidence_ids=list(doc_ids),
                        rationale=(
                            f"Conflicting positions about {name}: {verdict.rationale}"
                        ),
                    )
                )
        return gaps


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


DETECTOR_REGISTRY: dict[str, type[GapDetector]] = {
    "delta": DeltaDetector,
    "orphan": OrphanEntityDetector,
    "contradiction": ContradictionDetector,
    "user_flagged": UserFlaggedDetector,
}
