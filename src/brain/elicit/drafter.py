"""Turn a tacit-knowledge Gap into a confident draft rule for the user to correct."""
from __future__ import annotations

from typing import Any, Protocol

import psycopg

from ..enrichment import OllamaEnricher
from .schema import ElicitDraft, Gap

_EVIDENCE_FALLBACK_CHARS = 500


class Drafter(Protocol):
    """Anything that can turn a Gap into an ElicitDraft."""

    def draft(
        self, conn: psycopg.Connection[Any], gap: Gap, *, tenant_id: str
    ) -> ElicitDraft: ...


class GapDrafter:
    """Default drafter: fetch evidence summaries, ask the enricher to articulate the rule."""

    def __init__(self, enricher: OllamaEnricher) -> None:
        self._enricher = enricher

    def draft(
        self, conn: psycopg.Connection[Any], gap: Gap, *, tenant_id: str
    ) -> ElicitDraft:
        """Build an :class:`ElicitDraft` from a :class:`Gap`.

        Fetches the best available text for each evidence document (``summary``
        when present, otherwise the first :data:`_EVIDENCE_FALLBACK_CHARS`
        characters of ``content``), then asks :attr:`_enricher` to articulate
        the underlying tacit rule.

        ``tenant_id`` is accepted for API symmetry with the :class:`Drafter`
        protocol but is not yet used — all evidence is fetched from the shared
        ``documents`` table.
        """
        evidence_texts = self._fetch_evidence_texts(conn, gap.evidence_ids)
        subject = gap.rationale or gap.target_id
        rule = self._enricher.draft_rule(subject=subject, evidence_texts=evidence_texts)
        return ElicitDraft(
            gap_id=gap.gap_id,
            title=rule.title,
            draft_text=rule.rule_text,
            evidence_ids=gap.evidence_ids,
            evidence_texts=evidence_texts,
        )

    def _fetch_evidence_texts(
        self, conn: psycopg.Connection[Any], evidence_ids: list[str]
    ) -> list[str]:
        """Return one text snippet per evidence document.

        Prefers ``documents.summary``; falls back to the first
        :data:`_EVIDENCE_FALLBACK_CHARS` characters of ``content`` when
        ``summary`` is NULL.  Documents that yield an empty/NULL result after
        the coalesce are silently omitted.
        """
        if not evidence_ids:
            return []
        rows = conn.execute(
            "SELECT coalesce(summary, left(content, %s)) "
            "FROM documents WHERE id = ANY(%s::uuid[])",
            (_EVIDENCE_FALLBACK_CHARS, evidence_ids),
        ).fetchall()
        return [r[0] for r in rows if r[0]]
