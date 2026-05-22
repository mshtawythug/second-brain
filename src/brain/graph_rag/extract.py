"""Gated, validated, versioned concept entity extractor (wave G2-b, GraphRAG).

The concept aspect of the entity graph (spec §3 D3, §5, §8). People entities are
derived *for free* from the participants pipeline (see
:mod:`brain.graph_rag.reconcile`); **concepts** — topics, projects,
organizations, tools — are extracted from raw document text by a small local
Ollama model, then validated/canonicalized/positioned/capped here.

**Single responsibility split (SOLID).** The Ollama *transport* (httpx, JSON
mode, retry, timeout, error mapping) lives in :class:`brain.enrichment.
OllamaEnricher` — the same place ``summarize`` / ``propose_tags`` live, so this
module never duplicates a chat client (spec §8: "extract.py — OllamaExtractor
(validated/versioned/chunked) + canonicalization"). This module owns the
extraction *logic*:

* **Chunked** — long documents are split with the existing paragraph-aware
  :func:`brain.ingest.chunker.chunk_text` (token-budgeted via the enricher's
  ``count_tokens``) and the model is called once per chunk, so a document larger
  than the model context is still fully covered.
* **Validated** — every model entry is checked deterministically: it must be a
  ``{"name": str, "type": str}`` object whose ``type`` is one of the four
  concept types (:data:`CONCEPT_ENTITY_TYPES`). People and unknown types are
  dropped (spec §17b decision 2: "people excluded"). Malformed entries are
  skipped, never fatal.
* **Canonicalized** — ``canonical_key`` is ``name`` lower-cased with collapsed
  internal whitespace, matching the people aspect's "normalized lowercase"
  identity (:mod:`brain.graph_rag.reconcile`). Entities dedup on
  ``(entity_type, canonical_key)`` — the same key the eval gate scores
  (spec §17b decision 2) and the catalog uniqueness key
  ``UNIQUE(tenant_id, entity_type, canonical_key)`` (spec §5).
* **Positioned** — each entity's raw-text *word-index* positions are located in
  the full document (chunker-independent, per spec §4 D4: ``pos`` is "a
  token/word index for raw-text concepts"). These feed
  :func:`brain.graph_rag.cooccur.cooccurrence_counts` in G2-c.
* **Capped** — the per-document distinct-entity count is bounded by
  ``BRAIN_GRAPH_MAX_ENTITIES_PER_DOC`` (spec §10), kept by mention frequency
  (ties broken by ``canonical_key``), mirroring
  :func:`brain.graph_rag.cooccur._apply_entity_cap`.
* **Never-raise** — Ollama down / timeout / invalid JSON yields an **empty
  list + a WARN**, never an exception (spec §7 / §17b decision 7 discipline), so
  the gated G2-c ingest hook can never be broken by a flaky extractor.

**Versioning.** :data:`EXTRACTOR_VERSION` is the extraction-algorithm version.
:attr:`OllamaExtractor.version` folds it together with the model fingerprint
(``"<model>@<ver>"``), so a model swap *or* an algorithm bump flips the
``graph_index_state.extractor_ver`` watermark and forces G2-c to re-extract
(spec §7 step 1). The ``graph_entity_mentions.source`` provenance string
(spec §5a) is ``"extractor:<model>@<ver>"`` — i.e. ``f"extractor:{version}"`` —
constructed by the G2-c reconcile from this same property.

**Gating.** Concepts are default-OFF (``BRAIN_GRAPH_CONCEPTS=false``); this
module is dormant until G2-c wires it behind that flag. G2-b builds and
unit-tests the extractor in isolation only.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..config import Config
from ..enrichment import OllamaEnricher
from ..errors import EnrichmentError, OllamaUnavailable
from .cooccur import DEFAULT_MAX_ENTITIES_PER_DOC

_logger = logging.getLogger(__name__)

__all__ = [
    "CONCEPT_ENTITY_TYPES",
    "EXTRACTOR_VERSION",
    "EntityExtractor",
    "ExtractedEntity",
    "OllamaExtractor",
    "make_extractor",
]

# Extraction-algorithm version. Folded into :attr:`OllamaExtractor.version`,
# which feeds ``graph_index_state.extractor_ver`` (spec §7). Bump when the
# prompt / validation / canonicalization semantics change so reconcile
# re-extracts affected documents.
EXTRACTOR_VERSION = "concepts-v1"

# The concept entity types the extractor emits (spec §5 ``entity_type CHECK``
# minus ``person``). People are derived from the participants pipeline and are
# explicitly NOT extracted here (spec §17b decision 2: "people excluded"); any
# model entry typed ``person`` (or anything off this list) is dropped.
CONCEPT_ENTITY_TYPES = frozenset({"topic", "project", "org", "tool"})

# Per-chunk token budget for the LLM extraction calls. A document longer than
# this is split into multiple chunks (one model call each). Independent of the
# enrich-summary budget (no enrich<->graph config coupling); not env-tunable in
# G2 (YAGNI — promote to a knob only if the eval gate needs it).
_DEFAULT_CHUNK_TARGET_TOKENS = 1500

# Overlap between consecutive extraction chunks. A small overlap reduces the
# chance an entity straddling a chunk boundary is missed; cross-chunk repeats
# are harmless (dedup collapses them, positions are computed over the full doc).
_DEFAULT_CHUNK_OVERLAP_TOKENS = 100

# Raw-text word tokenizer for positions + canonicalization. ``\w+`` over the
# lower-cased text yields chunker-independent word ordinals (spec §4 D4).
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class ExtractedEntity:
    """One validated, canonicalized concept entity extracted from a document.

    The pre-upsert input to the G2-c concept reconcile (no ``tenant_id`` /
    ``id`` — those are assigned when the catalog row is upserted, exactly as a
    :class:`brain.graph_rag.reconcile.ResolvedPerson` carries no id).

    ``canonical_key`` is the dedup identity (``name`` lower-cased,
    whitespace-collapsed) — unique per ``(entity_type, canonical_key)`` and the
    key the eval gate scores. ``display_name`` is the surface form for the
    catalog ``name`` / AGE vertex label. ``positions`` are the entity's
    raw-text word-index occurrences over the *whole* document (empty when the
    model named a concept that does not appear literally in the text); they feed
    :func:`brain.graph_rag.cooccur.cooccurrence_counts` in G2-c.
    ``mention_count`` (``>= 1``) backs the ``graph_entity_mentions`` row.
    """

    entity_type: str
    canonical_key: str
    display_name: str
    positions: tuple[int, ...] = ()
    mention_count: int = 1


@runtime_checkable
class EntityExtractor(Protocol):
    """Resolve one document's concept entities (dependency-inversion seam).

    Narrow Protocol mirroring the embedder/enricher pattern: a ``version``
    property (threaded into the ``graph_index_state.extractor_ver`` watermark)
    and an ``extract`` method. The default implementation
    (:class:`OllamaExtractor`) wraps a local-Ollama model; G2-c depends only on
    this Protocol and tests inject fakes.
    """

    @property
    def version(self) -> str:
        """Watermark version string for ``graph_index_state.extractor_ver``."""
        ...

    def extract(self, text: str) -> list[ExtractedEntity]:
        """Return the document's deduped, capped concept entities (never raises)."""
        ...


class OllamaExtractor:
    """Local-Ollama concept extractor — validated, versioned, chunked.

    Composes a :class:`brain.enrichment.OllamaEnricher` for the chat transport
    (``extract_entities`` round-trip + ``count_tokens`` for chunking + the model
    fingerprint) and adds the extraction logic on top. Construct via
    :func:`make_extractor`; tests inject an enricher backed by an
    ``httpx.MockTransport`` (no monkey-patching).
    """

    def __init__(
        self,
        *,
        enricher: OllamaEnricher,
        max_entities: int | None = DEFAULT_MAX_ENTITIES_PER_DOC,
        chunk_target_tokens: int = _DEFAULT_CHUNK_TARGET_TOKENS,
        chunk_overlap_tokens: int = _DEFAULT_CHUNK_OVERLAP_TOKENS,
    ) -> None:
        if max_entities is not None and max_entities < 1:
            raise ValueError(
                f"max_entities must be a positive integer or None (got {max_entities})"
            )
        if chunk_target_tokens < 1:
            raise ValueError(
                f"chunk_target_tokens must be a positive integer (got {chunk_target_tokens})"
            )
        if chunk_overlap_tokens < 0:
            raise ValueError(
                "chunk_overlap_tokens must be a non-negative integer "
                f"(got {chunk_overlap_tokens})"
            )
        self._enricher = enricher
        self._max_entities = max_entities
        self._chunk_target_tokens = chunk_target_tokens
        self._chunk_overlap_tokens = chunk_overlap_tokens

    @property
    def version(self) -> str:
        """``"<model>@<ver>"`` — the watermark version (spec §7).

        Folds the model fingerprint together with :data:`EXTRACTOR_VERSION` so a
        model swap *or* an algorithm bump re-extracts. G2-c uses this both as the
        ``graph_index_state.extractor_ver`` value and (as ``f"extractor:{version}"``)
        the ``graph_entity_mentions.source`` provenance (spec §5a).
        """
        return f"{self._enricher.model}@{EXTRACTOR_VERSION}"

    def extract(self, text: str) -> list[ExtractedEntity]:
        """Extract one document's concept entities. Never raises.

        Chunks ``text``, calls the model once per chunk, validates + collects
        every well-formed candidate, then dedups on ``(entity_type,
        canonical_key)``, locates raw-text positions over the whole document,
        and applies the per-doc cap. On Ollama unavailability the whole
        extraction returns ``[]`` (+ WARN); a single chunk that returns
        malformed JSON is skipped (+ WARN) and the remaining chunks still
        contribute.
        """
        text = text.strip()
        if not text:
            return []

        # Late import keeps :mod:`brain.graph_rag` import-cheap and avoids pulling
        # the heavy :mod:`brain.ingest` package in at module load (mirrors
        # ``reconcile.py``'s late import of ``brain.wiki.build_people``).
        from ..ingest.chunker import chunk_text

        chunks = chunk_text(
            text,
            target_tokens=self._chunk_target_tokens,
            overlap_tokens=self._chunk_overlap_tokens,
            count_tokens=self._enricher.count_tokens,
        )
        candidates: list[tuple[str, str]] = []
        for chunk in chunks:
            try:
                raw_entities = self._enricher.extract_entities(chunk.content)
            except OllamaUnavailable as exc:
                # Server down — no chunk can succeed; abort the whole extraction.
                _logger.warning(
                    "concept extraction aborted: Ollama unavailable (%s); "
                    "returning no entities",
                    exc,
                )
                return []
            except EnrichmentError as exc:
                # Malformed JSON after retry on THIS chunk; skip it, keep going.
                _logger.warning(
                    "concept extraction: chunk %d failed (%s); skipping chunk",
                    chunk.index,
                    exc,
                )
                continue
            for entry in raw_entities:
                validated = _validate_entry(entry)
                if validated is not None:
                    candidates.append(validated)

        return self._finalize(candidates, text)

    def _finalize(
        self, candidates: list[tuple[str, str]], text: str
    ) -> list[ExtractedEntity]:
        """Dedup, position, cap, and order the validated candidates."""
        if not candidates:
            return []
        doc_words = _tokenize_words(text)
        # Dedup on (entity_type, canonical_key); keep the first-seen surface form
        # as the display name. Iteration order over the candidate list is
        # deterministic (chunk order, then in-chunk order), so the kept surface
        # form is stable.
        seen: dict[tuple[str, str], str] = {}
        for entity_type, display_name in candidates:
            canonical_key = _canonical_key(display_name)
            if not canonical_key:
                continue
            seen.setdefault((entity_type, canonical_key), display_name)

        entities: list[ExtractedEntity] = []
        for (entity_type, canonical_key), display_name in seen.items():
            positions = _locate_positions(display_name, doc_words)
            entities.append(
                ExtractedEntity(
                    entity_type=entity_type,
                    canonical_key=canonical_key,
                    display_name=display_name,
                    positions=positions,
                    mention_count=max(1, len(positions)),
                )
            )
        entities = self._apply_cap(entities)
        entities.sort(key=lambda entity: (entity.entity_type, entity.canonical_key))
        return entities

    def _apply_cap(self, entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
        """Bound the per-document distinct-entity count (spec §10).

        Keeps the most-mentioned entities (ties broken by ``canonical_key``
        ascending for determinism), mirroring
        :func:`brain.graph_rag.cooccur._apply_entity_cap`. ``None`` disables.
        """
        if self._max_entities is None or len(entities) <= self._max_entities:
            return entities
        ranked = sorted(
            entities,
            key=lambda entity: (
                -entity.mention_count,
                entity.entity_type,
                entity.canonical_key,
            ),
        )
        return ranked[: self._max_entities]


def make_extractor(cfg: Config) -> OllamaExtractor:
    """Factory — same shape as :func:`brain.enrichment.make_enricher`.

    Builds the :class:`OllamaExtractor` from ``cfg.graph_extract_model`` /
    ``cfg.ollama_host`` and the per-doc cap ``cfg.graph_max_entities``
    (``BRAIN_GRAPH_MAX_ENTITIES_PER_DOC``). The model is the dedicated
    ``BRAIN_GRAPH_EXTRACT_MODEL`` so the concept extractor and the summary
    enricher stay independently overridable (no enrich<->graph coupling).
    """
    enricher = OllamaEnricher(host=cfg.ollama_host, model=cfg.graph_extract_model)
    return OllamaExtractor(enricher=enricher, max_entities=cfg.graph_max_entities)


# --------------------------------------------------------------------------- #
# Pure helpers (validation / canonicalization / positions)
# --------------------------------------------------------------------------- #
def _validate_entry(entry: Any) -> tuple[str, str] | None:
    """Validate one raw model entry → ``(entity_type, display_name)`` or ``None``.

    Deterministic, defensive validation (spec §16 "strict validation"): an entry
    is accepted only when it is a ``{"name": str, "type": str}`` object with a
    non-empty ``name`` and a ``type`` (case-insensitively) in
    :data:`CONCEPT_ENTITY_TYPES`. Everything else — non-dicts, missing/extra
    keys, wrong value types, empty names, people, unknown types — returns
    ``None`` and is skipped by the caller.
    """
    if not isinstance(entry, dict):
        return None
    raw_name = entry.get("name")
    raw_type = entry.get("type")
    if not isinstance(raw_name, str) or not isinstance(raw_type, str):
        return None
    display_name = raw_name.strip()
    entity_type = raw_type.strip().lower()
    if not display_name:
        return None
    if entity_type not in CONCEPT_ENTITY_TYPES:
        return None
    return (entity_type, display_name)


def _canonical_key(display_name: str) -> str:
    """Canonical dedup key: lower-cased, internal whitespace collapsed.

    Matches the people aspect's "normalized lowercase" identity and the
    catalog's ``UNIQUE(tenant_id, entity_type, canonical_key)`` (spec §5). Returns
    an empty string only for a name with no word content (caller skips it).
    """
    return " ".join(display_name.lower().split())


def _tokenize_words(text: str) -> list[str]:
    r"""Lower-cased ``\w+`` word tokens — the raw-text position unit (spec §4 D4)."""
    return _WORD_RE.findall(text.lower())


def _locate_positions(name: str, doc_words: list[str]) -> tuple[int, ...]:
    """Word-index start positions of ``name`` in ``doc_words`` (case-insensitive).

    Finds every occurrence of the (possibly multi-word) entity name as a
    contiguous sub-sequence of the document's word stream and returns each
    match's starting word index. Empty when the name's words never appear
    contiguously (e.g. the model paraphrased a concept not present verbatim).
    """
    name_words = _tokenize_words(name)
    if not name_words:
        return ()
    span = len(name_words)
    last_start = len(doc_words) - span
    return tuple(
        i for i in range(last_start + 1) if doc_words[i : i + span] == name_words
    )
