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
* **Presence-validated (v3)** — a small model on an entity-*sparse* document
  tends to copy the prompt's illustrative names or emit its own reasoning text
  as "entities". So every candidate whose name does not actually appear in the
  source document (separator-normalized substring match) is dropped, regardless
  of prompt. This is the robust kill for few-shot prompt-example leakage; a
  reasoning-text reject (sentence-length / meta-commentary patterns) and a
  generic structural-noise filter (``PDF`` / ``Chapter 24`` / ``Section 3`` /
  page + format words) back it up.
* **One type per name (v3)** — when the model emits the *same* canonical name
  under multiple types (a standards body as both ``org`` and ``topic``; a
  numbered standard duplicated across ``topic`` / ``org`` / ``tool``), the
  duplicates are collapsed to a single node keeping the highest-precedence type
  (:data:`_TYPE_PRECEDENCE`: person > org > project > tool > topic), so the
  graph (and the communities derived from it) is not fragmented.
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
#
# concepts-v4 (2026-05-24, perf Fix C): the extractor now applies a configurable
# input head cap (``BRAIN_GRAPH_EXTRACT_MAX_INPUT_TOKENS``, default 8000) BEFORE
# chunking, so a long document is no longer extracted in full. This changes the
# extraction output for docs past the cap, so the watermark MUST change to force a
# ``--backfill`` re-extraction.
EXTRACTOR_VERSION = "concepts-v4"

# The concept entity types the extractor emits (spec §5 ``entity_type CHECK``
# minus ``person``). People are derived from the participants pipeline and are
# explicitly NOT extracted here (spec §17b decision 2: "people excluded"); any
# model entry typed ``person`` (or anything off this list) is dropped.
CONCEPT_ENTITY_TYPES = frozenset({"topic", "project", "org", "tool"})

# Near-synonym TYPE labels a small model commonly emits for the four canonical
# concept types. Normalized to the canonical type BEFORE the allowlist check in
# :func:`_validate_entry`, so a valid extraction tagged ``"organization"`` /
# ``"software"`` / ``"initiative"`` / ``"theme"`` is recovered rather than
# silently dropped (a real recall loss the gate exposed). This is keyed strictly
# on the TYPE label — never on the entity NAME (no vendor/name allowlist, which
# would overfit the synthetic gate fixture).
#
# Deliberately conservative: ONLY unambiguous type-label synonyms are mapped.
# Ambiguous words (``platform`` / ``technology`` / ``domain`` / ``area`` /
# ``language`` / ``infrastructure`` / ``app``) are intentionally EXCLUDED — a
# real-corpus entity mislabeled with one of those is better dropped than
# silently mis-typed (Codex review).
_TYPE_LABEL_SYNONYMS: dict[str, str] = {
    "organization": "org",
    "organisation": "org",
    "company": "org",
    "companies": "org",
    "vendor": "org",
    "institution": "org",
    "corporation": "org",
    "software": "tool",
    "framework": "tool",
    "library": "tool",
    "application": "tool",
    "initiative": "project",
    "effort": "project",
    "program": "project",
    "programme": "project",
    "projects": "project",
    "theme": "topic",
    "subject": "topic",
    "topics": "topic",
}

# Minimum canonical-key length (characters). A floor of 2 drops single-character
# noise while KEEPING short acronyms / tool names (a 2-char language, ``ml``,
# ``ci``…). Deliberately not 3 — a 3-char floor silently kills valid 2-char
# acronyms in the real corpus (Codex review, recall-protective).
_MIN_CANONICAL_KEY_LEN = 2

# Generic structural / meeting nouns that are never a useful standalone concept.
# EXACT canonical-key match only, and intentionally GENERIC (NOT this fixture's
# specific distractor terms) so it never blocks a legitimate domain concept in
# the real 1195-doc corpus (Codex review: a fixture-derived blocklist overfits
# the precision side). Keep this list small and obviously-generic.
_GENERIC_STOP_KEYS: frozenset[str] = frozenset({
    "platform",
    "service",
    "services",
    "system",
    "systems",
    "stack",
    "team",
    "teams",
    "group",
    "groups",
    "meeting",
    "meetings",
    "call",
    "calls",
    "sync",
    "standup",
    "review",
    "reviews",
    "update",
    "updates",
    "discussion",
    "workshop",
    "sprint",
    "document",
    "note",
    "notes",
    "report",
    "dashboard",
    "dashboards",
})

# Document-structure / file-format noise that is never a standalone concept
# (audit B.5: ``PDF`` / ``Chapter 24`` / ``<Body> Standard`` extracted as
# topics). EXACT canonical-key match, extends :data:`_GENERIC_STOP_KEYS`, and
# kept deliberately GENERIC — only format + document-structure words, never a
# real product / standards-body / domain name (a corpus-derived blocklist would
# overfit precision and risk dropping legitimate concepts). "standard" /
# "standards" sit here because the audit observed a numbered-standards family
# fragmenting the graph; a real domain concept is virtually never the bare word.
_STRUCTURAL_STOP_KEYS: frozenset[str] = frozenset({
    "pdf",
    "docx",
    "doc",
    "html",
    "csv",
    "json",
    "xml",
    "page",
    "pages",
    "chapter",
    "chapters",
    "section",
    "sections",
    "appendix",
    "appendices",
    "figure",
    "figures",
    "table",
    "tables",
    "exhibit",
    "exhibits",
    "paragraph",
    "paragraphs",
    "footnote",
    "footnotes",
    "heading",
    "headings",
    "introduction",
    "conclusion",
    "abstract",
    "preface",
    "glossary",
    "index",
    "contents",
    "attachment",
    "attachments",
    "screenshot",
    "screenshots",
    "standard",
    "standards",
})

# A structural reference of the form "<structural noun> <enumerator>" — e.g.
# "chapter 24", "section 3", "page 12", "figure 2", "appendix a", "standard 72".
# Matches a document-structure noun followed by a number / roman numeral / single
# letter; generic by construction, never a real entity name. Applied to the
# already-lower-cased canonical key.
_STRUCTURAL_ENUM_RE = re.compile(
    r"^(?:chapter|section|page|figure|fig|table|appendix|exhibit|paragraph|"
    r"para|footnote|clause|article|item|part|volume|vol|step|standard|"
    r"version|ver|revision|rev)\s+(?:\d+|[ivxlcdm]+|[a-z])$"
)

# Maximum word count for an entity NAME. Concept names are short (the gate's
# longest gold entity is two words); a longer "name" is almost always the
# model's reasoning text / a sentence rather than an entity (audit B.2). A
# generous cap of 6 protects recall on legitimate multi-word concepts.
_MAX_ENTITY_WORDS = 6

# Meta-commentary / reasoning-text markers a small model emits AS an entity name
# on entity-sparse documents (audit B.2: e.g. "<X> is not present in the text.
# However, …"). Matched against the candidate name; a hit means it is the
# model's prose, not a concept, so it is dropped. Generic phrasing only.
_REASONING_PATTERNS = re.compile(
    r"(?i)("
    r"\bis not present\b|\bare not present\b|\bnot present in\b|"
    r"\bdoes not appear\b|\bdo not appear\b|\bnot mentioned\b|"
    r"\bno (?:clear |concept )*entit(?:y|ies)\b|"
    r"\bthere (?:is|are) no\b|"
    r"\bcannot (?:find|identify|extract)\b|\bunable to\b|"
    r"\bthe (?:text|document|passage) (?:does|contains|has|mentions|states|"
    r"describes|discusses)\b|"
    r"\bn/a\b|\bnone\b|\bhowever\b"
    r")"
)

# Type precedence for cross-type collapse (audit B.3/B.4): when the SAME
# canonical name is emitted under multiple types, keep ONE node with the
# highest-precedence type and drop the rest. ``person`` ranks first for
# completeness (a name confusable with a person should win person), though
# people never reach :func:`_finalize` — they are dropped in
# :func:`_validate_entry`. Among concept types: a hosted provider is an ``org``
# before a ``topic``; a named effort is a ``project`` before a ``tool``.
_TYPE_PRECEDENCE: tuple[str, ...] = ("person", "org", "project", "tool", "topic")
_TYPE_RANK: dict[str, int] = {t: rank for rank, t in enumerate(_TYPE_PRECEDENCE)}

# Separator-normalization for the presence check: lower-case and collapse every
# run of non-alphanumeric characters (whitespace, hyphens, dots, underscores) to
# a single space. So "back-pressure" / "back_pressure" / "Back Pressure" all
# normalize to "back pressure" and a hyphenated concept present in the text is
# not spuriously dropped.
_SEPARATOR_RE = re.compile(r"[\W_]+", re.UNICODE)

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
        max_input_tokens: int | None = None,
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
        if max_input_tokens is not None and max_input_tokens < 1:
            raise ValueError(
                "max_input_tokens must be a positive integer or None "
                f"(got {max_input_tokens})"
            )
        self._enricher = enricher
        self._max_entities = max_entities
        self._chunk_target_tokens = chunk_target_tokens
        self._chunk_overlap_tokens = chunk_overlap_tokens
        # Perf Fix C: head cap (in cl100k_base tokens) applied to the document
        # body before chunking. ``None`` == no cap (the whole document is
        # extracted — the historical behavior and the test default). Production
        # threads ``BRAIN_GRAPH_EXTRACT_MAX_INPUT_TOKENS`` via ``make_extractor``.
        self._max_input_tokens = max_input_tokens

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

        Optionally head-caps ``text`` to the first ``max_input_tokens`` tokens
        (perf Fix C; ``None`` = no cap), then chunks it, calls the model once per
        chunk, validates + collects every well-formed candidate, then dedups on
        ``(entity_type, canonical_key)``, locates raw-text positions over the
        (capped) document, and applies the per-doc cap. On Ollama unavailability
        the whole extraction returns ``[]`` (+ WARN); a single chunk that returns
        malformed JSON is skipped (+ WARN) and the remaining chunks still
        contribute.
        """
        text = text.strip()
        if not text:
            return []

        # Perf Fix C: head-cap the input BEFORE chunking. The model is called once
        # per chunk, so a long document drives a heavy tail of LLM calls; capping
        # to the first ``self._max_input_tokens`` tokens bounds that tail. The cap
        # uses the enricher's tokenizer (the same ``count_tokens`` path the chunker
        # below budgets with), so the truncation boundary and the chunk sizing
        # agree. ``None`` disables the cap (whole document extracted). KISS: a
        # plain HEAD cap — head+tail sampling (to also catch concepts that appear
        # only deep in a long transcript) is a deliberate FUTURE tuning option, not
        # built here.
        if self._max_input_tokens is not None:
            text = self._enricher.truncate_to_tokens(text, self._max_input_tokens)

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
        """Repair, filter, dedup, position, cap, and order the candidates."""
        if not candidates:
            return []
        doc_words = _tokenize_words(text)
        normalized_text = _normalize_for_presence(text)
        # Dedup on (entity_type, canonical_key); keep the first-seen surface form
        # as the display name. Iteration order over the candidate list is
        # deterministic (chunk order, then in-chunk order), so the kept surface
        # form is stable. Per candidate, in order: restore a stripped "Project"
        # prefix when the document names the project that way (fixes an exact-key
        # miss); drop the model's reasoning text / meta-commentary (B.2); drop
        # too-short, generic, or structural-noise keys (B.5 precision); and drop
        # any name that does not actually appear in the source text (B.1 presence
        # validation — the robust kill for few-shot prompt-example leakage).
        seen: dict[tuple[str, str], str] = {}
        for entity_type, raw_display_name in candidates:
            display_name = _repair_project_prefix(entity_type, raw_display_name, text)
            if _is_reasoning_text(display_name):
                continue
            canonical_key = _canonical_key(display_name)
            if _is_noise_key(canonical_key):
                continue
            if not _name_present_in_text(canonical_key, normalized_text):
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
        entities = _dedupe_project_substrings(entities)
        entities = _dedupe_cross_type(entities)
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

    The HTTP timeout is threaded from ``cfg.enrich_timeout_seconds``
    (``BRAIN_ENRICH_TIMEOUT_SECONDS``) — NOT the 60s ``OllamaEnricher`` default.
    A large document can take more than 60s to extract on a slow model; the old
    hardcoded default timed those out and silently returned zero entities.
    Operators raise ``BRAIN_ENRICH_TIMEOUT_SECONDS`` for slow models.

    The input head cap is threaded from ``cfg.graph_extract_max_input_tokens``
    (``BRAIN_GRAPH_EXTRACT_MAX_INPUT_TOKENS``, default 8000; ``None`` disables)
    so a long document's body is truncated to its first N tokens before chunking
    — bounding the per-document LLM call count (perf Fix C).
    """
    enricher = OllamaEnricher(
        host=cfg.ollama_host,
        model=cfg.graph_extract_model,
        timeout=cfg.enrich_timeout_seconds,
    )
    return OllamaExtractor(
        enricher=enricher,
        max_entities=cfg.graph_max_entities,
        max_input_tokens=cfg.graph_extract_max_input_tokens,
    )


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
    # Normalize near-synonym type labels (organization/software/initiative/theme/
    # …) to the canonical type BEFORE the allowlist check, so a valid extraction
    # is not dropped purely over label wording (a real recall loss). Keyed on the
    # TYPE label only — never on the entity NAME.
    entity_type = _TYPE_LABEL_SYNONYMS.get(entity_type, entity_type)
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


def _is_noise_key(canonical_key: str) -> bool:
    """True when a canonical key is too short, a generic stop word, or structural.

    Drops single-character noise (``len < _MIN_CANONICAL_KEY_LEN``), an exact
    match against the generic :data:`_GENERIC_STOP_KEYS` / file-format +
    document-structure :data:`_STRUCTURAL_STOP_KEYS` lists, and the
    "<structural noun> <enumerator>" pattern (``chapter 24``, ``section 3``,
    ``standard 72`` — :data:`_STRUCTURAL_ENUM_RE`). All checks are deliberately
    narrow + generic to protect recall — short acronyms (>= 2 chars) and every
    real domain concept pass through.
    """
    return (
        len(canonical_key) < _MIN_CANONICAL_KEY_LEN
        or canonical_key in _GENERIC_STOP_KEYS
        or canonical_key in _STRUCTURAL_STOP_KEYS
        or _STRUCTURAL_ENUM_RE.match(canonical_key) is not None
    )


def _normalize_for_presence(text: str) -> str:
    """Lower-case ``text`` and collapse every non-alphanumeric run to one space.

    The shared normalization for the presence check: separators (whitespace,
    hyphens, dots, underscores) all become a single space, so a hyphenated or
    handle-style concept that appears in the source is matched against its
    whitespace-collapsed canonical key (:func:`_canonical_key`).
    """
    return _SEPARATOR_RE.sub(" ", text.lower()).strip()


def _name_present_in_text(canonical_key: str, normalized_text: str) -> bool:
    """True when the entity name actually appears in the source text (B.1).

    Separator-normalized substring match: the canonical key is re-normalized the
    same way as ``normalized_text`` (:func:`_normalize_for_presence`) and checked
    for containment. Deliberately a substring (not a whole-word) match so a bare
    ``"helios"`` still counts as present when the document writes
    ``"Helioscope"`` (lenient = recall-safe); the goal is only to drop names that
    do **not** occur at all — hallucinated few-shot example names and paraphrases
    the model invented but never wrote.
    """
    needle = _normalize_for_presence(canonical_key)
    if not needle:
        return False
    return needle in normalized_text


def _is_reasoning_text(display_name: str) -> bool:
    """True when a candidate "name" is the model's reasoning text, not an entity.

    Drops sentence-length names (more than :data:`_MAX_ENTITY_WORDS` words) and
    names containing meta-commentary markers (:data:`_REASONING_PATTERNS`, e.g.
    "is not present", "however", "the document discusses") that a small model
    emits as an "entity" on entity-sparse documents (audit B.2). Generic phrasing
    only — never keyed on a specific entity name.
    """
    stripped = display_name.strip()
    if not stripped:
        return True
    if len(stripped.split()) > _MAX_ENTITY_WORDS:
        return True
    return _REASONING_PATTERNS.search(stripped) is not None


def _dedupe_cross_type(entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
    """Collapse one canonical name emitted under multiple types to a single node.

    Audit B.3/B.4: the model frequently emits the same real entity under more
    than one type (a standards body as both ``org`` and ``topic``; each numbered
    standard duplicated across ``topic`` / ``org`` / ``tool``), inflating and
    fragmenting the graph and the Louvain communities built from it. For each
    canonical key, keep the single highest-precedence type
    (:data:`_TYPE_PRECEDENCE`) and drop the lower-precedence duplicates. Order is
    preserved. Positions are a pure function of the name + document text, so the
    surviving node already carries the correct ``mention_count`` — no re-summing.
    """
    unknown_rank = len(_TYPE_PRECEDENCE)
    best_rank: dict[str, int] = {}
    for entity in entities:
        rank = _TYPE_RANK.get(entity.entity_type, unknown_rank)
        current = best_rank.get(entity.canonical_key)
        if current is None or rank < current:
            best_rank[entity.canonical_key] = rank
    return [
        entity
        for entity in entities
        if _TYPE_RANK.get(entity.entity_type, unknown_rank)
        == best_rank[entity.canonical_key]
    ]


def _repair_project_prefix(entity_type: str, display_name: str, text: str) -> str:
    """Restore a stripped ``Project`` prefix when the document names it that way.

    Small models often return the bare project name (``"Helios"``) for a concept
    the document writes as ``"Project Helios"``. When the entity is a ``project``
    and the ``"Project <name>"`` form appears verbatim in the text, prefer that
    fuller surface form so the canonical key matches how the project is actually
    named. Generic (any project name) — never keyed on a specific entity.

    The match is a contiguous WORD/token sequence, not a substring: a bare
    ``"Helios"`` is NOT promoted when the document only says ``"Project
    Helioscope"`` (which contains ``"project helios"`` as a substring).
    """
    if entity_type != "project":
        return display_name
    stripped = display_name.strip()
    if not stripped or stripped.lower().startswith("project "):
        return display_name
    name_words = _tokenize_words(stripped)
    if not name_words:
        return display_name
    target = ["project", *name_words]
    doc_words = _tokenize_words(text)
    span = len(target)
    if any(
        doc_words[i : i + span] == target
        for i in range(len(doc_words) - span + 1)
    ):
        return f"Project {stripped}"
    return display_name


def _is_word_subsequence(short_words: list[str], long_words: list[str]) -> bool:
    """True when ``short_words`` appears as a contiguous run inside ``long_words``."""
    span = len(short_words)
    if not span or span >= len(long_words):
        return False
    return any(
        long_words[i : i + span] == short_words
        for i in range(len(long_words) - span + 1)
    )


def _dedupe_project_substrings(
    entities: list[ExtractedEntity],
) -> list[ExtractedEntity]:
    """Drop a bare ``project`` entity subsumed by a fuller ``"Project X"`` sibling.

    Conservative and PROJECT-SCOPED: when two ``project`` entities exist and one
    canonical key is a contiguous word-subsequence of the other (e.g. ``helios``
    vs ``project helios``), keep only the longer named form. Deliberately NOT
    applied to org/tool/topic — for those a shorter key is often the correct
    concept (``billing`` vs ``billing platform``), so a blanket keep-longest
    would hurt recall.
    """
    projects = [e for e in entities if e.entity_type == "project"]
    if len(projects) < 2:
        return entities
    drop_keys: set[str] = set()
    for candidate in projects:
        candidate_words = candidate.canonical_key.split()
        for other in projects:
            if other.canonical_key == candidate.canonical_key:
                continue
            if _is_word_subsequence(candidate_words, other.canonical_key.split()):
                drop_keys.add(candidate.canonical_key)
                break
    if not drop_keys:
        return entities
    return [
        e
        for e in entities
        if not (e.entity_type == "project" and e.canonical_key in drop_keys)
    ]
