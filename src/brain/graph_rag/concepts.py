"""Concept-aspect relational helpers for graph reconcile (wave G2-c, GraphRAG).

The concept counterpart of the person-aspect helpers in
:mod:`brain.graph_rag.reconcile`. People are derived *for free* from the
participants pipeline; **concepts** — topics, projects, organizations, tools —
are extracted from raw document text by the gated
:class:`brain.graph_rag.extract.EntityExtractor` (default
:class:`~brain.graph_rag.extract.OllamaExtractor`), then upserted +
positioned + co-occurrence-windowed here.

This module owns ONLY the concept-aspect's relational + derive-time pieces,
keeping :mod:`brain.graph_rag.reconcile` the lean orchestrator (mirroring the
G1 boundary that moved the shared aggregate recompute into
:mod:`brain.graph_rag.aggregates`):

* :data:`CONCEPTS_ASPECT` / :data:`CONCEPT_ENTITY_TYPES` — the
  ``graph_index_state.aspect`` value (migration 012 ``CHECK IN
  ('people','concepts')``) and the four concept ``entity_type``s the extractor
  emits (people are a separate aspect — never double-counted).
* :func:`concept_mention_source` — the ``graph_entity_mentions.source``
  provenance string ``"extractor:<model>@<ver>"`` (spec §5a) from the
  extractor's ``version``.
* :func:`concept_inputs_hash` — the per-aspect watermark ``inputs_hash``. Unlike
  the person aspect (which folds the *resolved persons* into ``inputs_hash`` so a
  metadata-only edit that does not change ``content_hash`` still re-indexes),
  concept extraction depends ONLY on the document text + the model/algorithm, so
  ``content_hash`` + ``extractor_ver`` already capture the extraction inputs and
  ``inputs_hash`` carries only the co-occurrence config (window + per-doc cap).
  This is what lets the G2-c concept skip-check run **before** the LLM call —
  an unchanged watermark short-circuits with no extraction (spec §7 step 1).
* :func:`upsert_concept_entities` — upsert the extracted entities into
  ``graph_entities`` (keyed ``(tenant_id, entity_type, canonical_key)``),
  returning :class:`~brain.graph_rag.schema.GraphEntity` rows with their ids.
* :func:`build_concept_rows` — turn the extracted entities + their upserted ids
  into the doc's concept ``graph_entity_mentions`` + ``graph_edge_contributions``
  rows. Concepts use **real raw-text word positions** (spec §4 D4) so the
  co-occurrence window pairs only entities that occur within ``window`` words of
  each other — distinct from the person aspect's doc-level co-presence (every
  participant at notional position 0, so any ``window >= 1`` yields the complete
  graph over the doc's persons).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg

from .cooccur import EntityOccurrence, cooccurrence_counts, to_contributions
from .extract import CONCEPT_ENTITY_TYPES as _CONCEPT_ENTITY_TYPES
from .extract import ExtractedEntity
from .schema import EdgeContribution, EntityMention, GraphEntity

__all__ = [
    "CONCEPTS_ASPECT",
    "CONCEPT_ENTITY_TYPES",
    "build_concept_rows",
    "concept_inputs_hash",
    "concept_mention_source",
    "upsert_concept_entities",
]

# The migration-012 ``graph_index_state.aspect`` value this aspect owns. People
# and concepts re-index independently under their own watermark rows (spec §7).
CONCEPTS_ASPECT = "concepts"

# The four concept ``entity_type``s (migration 012 ``CHECK`` minus ``person``),
# as a sorted tuple for deterministic ``entity_type = ANY(%s)`` scoping in the
# aspect-scoped relational rewrite. People are derived from the participants
# pipeline and handled by the person aspect — never extracted here (spec §17b
# decision 2: "people excluded"), so concept reconcile never touches a person
# row and the two aspects never double-count an entity.
CONCEPT_ENTITY_TYPES: tuple[str, ...] = tuple(sorted(_CONCEPT_ENTITY_TYPES))


def concept_mention_source(extractor_version: str) -> str:
    """``graph_entity_mentions.source`` provenance for concepts (spec §5a).

    ``"extractor:<model>@<ver>"`` — i.e. ``f"extractor:{extractor.version}"``,
    where ``extractor.version`` is the ``"<model>@concepts-v3"`` fingerprint
    (:attr:`brain.graph_rag.extract.OllamaExtractor.version`). Distinguishes
    concept mentions from the person pipeline's ``"people"`` source.
    """
    return f"extractor:{extractor_version}"


def concept_inputs_hash(window: int, max_entities: int | None) -> str:
    """Stable fingerprint of the concept-aspect's config inputs (watermark).

    Captures only the co-occurrence config (window + per-doc cap). The document
    content is tracked by ``graph_index_state.content_hash`` and the
    model/algorithm by ``extractor_ver``, so this deliberately excludes the
    extracted entities — letting the G2-c concept skip-check run before any LLM
    call (an unchanged watermark short-circuits extraction; spec §7 step 1).
    """
    payload = {
        "aspect": CONCEPTS_ASPECT,
        "window": window,
        "max_entities": max_entities,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def upsert_concept_entities(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    entities: list[ExtractedEntity],
) -> list[GraphEntity]:
    """Upsert extracted concept ``graph_entities`` rows, returning them with ids.

    Keyed on ``(tenant_id, entity_type, canonical_key)`` (migration 012) so
    re-running reuses the existing row (refreshing its surface ``name``). Order
    is preserved 1:1 with ``entities`` so the caller can zip each returned
    :class:`GraphEntity` back to its source :class:`ExtractedEntity` (positions /
    mention_count). Mirrors
    :func:`brain.graph_rag.reconcile._upsert_person_entities` but carries each
    entity's own concept ``entity_type`` rather than the hardcoded ``person``.
    """
    result: list[GraphEntity] = []
    for entity in entities:
        row = conn.execute(
            """
            INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, entity_type, canonical_key) DO UPDATE SET
                name = EXCLUDED.name,
                updated_at = NOW()
            RETURNING id::text
            """,
            (tenant_id, entity.entity_type, entity.display_name, entity.canonical_key),
        ).fetchone()
        # RETURNING on an INSERT ... ON CONFLICT DO UPDATE always yields one row.
        assert row is not None
        result.append(
            GraphEntity(
                id=str(row[0]),
                entity_type=entity.entity_type,
                name=entity.display_name,
                canonical_key=entity.canonical_key,
                tenant_id=tenant_id,
            )
        )
    return result


def build_concept_rows(
    extracted: list[ExtractedEntity],
    concept_entities: list[GraphEntity],
    *,
    document_id: str,
    tenant_id: str,
    window: int,
    source: str,
) -> tuple[list[EntityMention], list[EdgeContribution]]:
    """Build a doc's concept mentions + co-occurrence contributions.

    ``extracted`` and ``concept_entities`` are positional twins (the latter is
    :func:`upsert_concept_entities`'s output). Each extracted entity becomes one
    ``graph_entity_mentions`` row (provenance ``source``); its real raw-text word
    positions (spec §4 D4) become :class:`~brain.graph_rag.cooccur.EntityOccurrence`
    inputs to the windowed co-occurrence — so two concepts co-occur iff their
    word positions differ by at most ``window`` (genuine text proximity, distinct
    from the person aspect's doc-level co-presence). An extracted entity the
    model named but that never appears verbatim has empty positions: it is a
    mention (it is a concept of the doc) but pairs with nothing. The per-doc
    distinct-entity cap was already applied by the extractor, so the
    co-occurrence pass disables its own cap (``max_entities=None``), mirroring the
    person aspect.
    """
    mentions = [
        EntityMention(
            entity_id=entity.id,
            document_id=document_id,
            source=source,
            tenant_id=tenant_id,
            mention_count=ext.mention_count,
        )
        for ext, entity in zip(extracted, concept_entities, strict=True)
    ]
    occurrences = [
        EntityOccurrence(entity_id=entity.id, position=position)
        for ext, entity in zip(extracted, concept_entities, strict=True)
        for position in ext.positions
    ]
    counts = cooccurrence_counts(occurrences, window=window, max_entities=None)
    contributions = to_contributions(
        counts, document_id=document_id, tenant_id=tenant_id
    )
    return mentions, contributions
