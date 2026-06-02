"""Concept-extractor eval gate metric (wave G2-j, GraphRAG; spec §17b decision 2).

The gate that informs the **later** decision to flip ``BRAIN_GRAPH_CONCEPTS``
default-ON. **G2 ships concepts default-OFF regardless of this gate** — this
module is a *measurement / decision* tool, not a G2 blocker (spec §17b
decision 2: "Passing this gate is necessary but not sufficient to flip the
default; ``BRAIN_GRAPH_CONCEPTS`` stays default-OFF for the G2 ship").

**Metric (normative, spec §17b decision 2).** Document-level, **type-aware**
concept-set **micro-F1** over unique ``(entity_type, canonical_key)`` pairs after
canonicalization, **people excluded**. Aggregation is *micro* — true/false
positives and false negatives are summed across every document, then a single
precision/recall/F1 is computed from those totals (so a document with many
concepts weighs more than one with few, which is the right unit for an
extraction gate). Type-awareness means ``("org", "acme")`` and
``("project", "acme")`` are **distinct** pairs. People are excluded because they
are derived for free from the participants pipeline, not the LLM extractor.

**Pass threshold (normative).** ``micro_f1 >= 0.80`` AND ``precision >= 0.85``
AND ``recall >= 0.70`` AND ``invalid_json_or_schema_rate == 0`` — see
:attr:`ConceptF1Report.passes`.

``invalid_json_or_schema_rate`` is the fraction of documents whose extraction
produced malformed / schema-invalid model output (the live gate detects this via
the :class:`~brain.graph_rag.extract.OllamaExtractor`'s documented
"skipping chunk" WARN; an invalid document contributes **no** predicted pairs,
so all its gold pairs become false negatives).

This module is **pure** — no DB, no Ollama, no I/O on the metric path — and is
fully unit-testable with hand-constructed predicted-vs-gold pair sets. Only the
in-repo synthetic-fixture loader (:func:`load_concept_fixture`) touches the
filesystem. Mirrors :mod:`brain.eval.metrics` (the ranking metrics) in style.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import EvalCorpusError, EvalMetricError

__all__ = [
    "ConceptF1Report",
    "ConceptFixtureDoc",
    "ConceptPair",
    "PASS_INVALID_RATE",
    "PASS_MICRO_F1",
    "PASS_PRECISION",
    "PASS_RECALL",
    "PERSON_ENTITY_TYPE",
    "concept_set_micro_f1",
    "load_concept_fixture",
    "normalize_concept_pairs",
]

# One concept identity: ``(entity_type, canonical_key)`` — the gate's scoring
# unit and the catalog's ``UNIQUE(tenant_id, entity_type, canonical_key)`` key.
ConceptPair = tuple[str, str]

# People are excluded from the concept gate (spec §17b decision 2): they come
# from the participants pipeline for free, not the LLM extractor.
PERSON_ENTITY_TYPE = "person"

# Pass thresholds (spec §17b decision 2). Stored as module constants so the gate
# test and any future surfacing share one source of truth.
PASS_MICRO_F1 = 0.80
PASS_PRECISION = 0.85
PASS_RECALL = 0.70
PASS_INVALID_RATE = 0.0

# Fixture schema version — bump when the YAML shape changes (mirrors
# :data:`brain.eval.corpus._CORPUS_VERSION`).
_FIXTURE_VERSION = 1

# Default path to the in-repo synthetic labeled fixture. Resolved relative to the
# package install root exactly like :data:`brain.eval.corpus._DEFAULT_CORPUS_PATH`
# (eval/ → brain/ → src/ → repo root, then tests/eval/). Unlike the gitignored
# golden corpus, THIS fixture is synthetic (no PII) and shipped in-repo.
_DEFAULT_FIXTURE_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "eval"
    / "concept_extraction_fixture.yaml"
)


@dataclass(frozen=True)
class ConceptF1Report:
    """Type-aware concept-set micro-F1 result + the four gate numbers.

    ``precision`` / ``recall`` / ``micro_f1`` are computed *micro* from the
    summed-across-documents ``true_positives`` / ``false_positives`` /
    ``false_negatives``. ``invalid_json_or_schema_rate`` =
    ``n_invalid_docs / n_docs``. :attr:`passes` applies the spec §17b decision 2
    thresholds.
    """

    n_docs: int
    n_invalid_docs: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    micro_f1: float
    invalid_json_or_schema_rate: float

    @property
    def passes(self) -> bool:
        """True iff all four gate conditions hold (spec §17b decision 2)."""
        return (
            self.micro_f1 >= PASS_MICRO_F1
            and self.precision >= PASS_PRECISION
            and self.recall >= PASS_RECALL
            and self.invalid_json_or_schema_rate <= PASS_INVALID_RATE
        )

    def failing_conditions(self) -> list[str]:
        """Human-readable list of the gate conditions this report fails.

        Empty when :attr:`passes` is True. Used by the gate test to build an
        actionable assertion message.
        """
        failures: list[str] = []
        if self.micro_f1 < PASS_MICRO_F1:
            failures.append(f"micro_f1={self.micro_f1:.4f} < {PASS_MICRO_F1}")
        if self.precision < PASS_PRECISION:
            failures.append(f"precision={self.precision:.4f} < {PASS_PRECISION}")
        if self.recall < PASS_RECALL:
            failures.append(f"recall={self.recall:.4f} < {PASS_RECALL}")
        if self.invalid_json_or_schema_rate > PASS_INVALID_RATE:
            failures.append(
                f"invalid_json_or_schema_rate="
                f"{self.invalid_json_or_schema_rate:.4f} > {PASS_INVALID_RATE}"
            )
        return failures


@dataclass(frozen=True)
class ConceptFixtureDoc:
    """One synthetic labeled document from the concept-gate fixture.

    ``text`` is the synthetic body fed to the extractor; ``gold_concepts`` is the
    hand-labeled set of unique ``(entity_type, canonical_key)`` pairs the
    extractor *should* find (people excluded by construction). ``doc_id`` is a
    synthetic identifier for diagnostics only.
    """

    doc_id: str
    text: str
    gold_concepts: tuple[ConceptPair, ...]


def normalize_concept_pairs(pairs: Iterable[ConceptPair]) -> set[ConceptPair]:
    """Canonicalize a pair iterable into a deduped, people-excluded pair set.

    The ``canonical_key`` is lower-cased with ALL separators stripped — the
    strip-all rule that mirrors :func:`brain.graph_rag.extract._canonical_key`
    (Bug B) and the catalog identity, so gold/predicted pairs score on the same
    key shape (a human-readable spaced gold key like ``project aurora`` and the
    extractor's strip-all ``projectaurora`` collapse to one identity). The
    ``entity_type`` is lower-cased + whitespace-collapsed (types are single
    words). ``person``-typed pairs and pairs with an empty type or key are dropped
    — so the metric is type-aware *and* people-excluded regardless of what the
    caller passes (spec §17b decision 2).
    """
    out: set[ConceptPair] = set()
    for entity_type, canonical_key in pairs:
        norm_type = " ".join(str(entity_type).lower().split())
        norm_key = re.sub(r"[\W_]+", "", str(canonical_key).lower())
        if not norm_type or not norm_key:
            continue
        if norm_type == PERSON_ENTITY_TYPE:
            continue
        out.add((norm_type, norm_key))
    return out


def concept_set_micro_f1(
    *,
    predicted_per_doc: Sequence[Iterable[ConceptPair]],
    gold_per_doc: Sequence[Iterable[ConceptPair]],
    invalid_doc_flags: Sequence[bool] | None = None,
) -> ConceptF1Report:
    """Document-level, type-aware concept-set **micro-F1** (spec §17b decision 2).

    For each document, the predicted and gold pair iterables are canonicalized +
    people-excluded via :func:`normalize_concept_pairs`. A document flagged
    invalid (malformed model output) contributes **no** predicted pairs — all its
    gold pairs become false negatives — and increments the invalid count. True
    positives / false positives / false negatives are summed *across* documents
    (micro), then precision / recall / F1 are computed from those totals.

    Args:
        predicted_per_doc: Per-document predicted concept pairs (extractor output).
        gold_per_doc: Per-document hand-labeled gold concept pairs. Must be the
            same length as ``predicted_per_doc``.
        invalid_doc_flags: Optional per-document "extraction was malformed" flags
            (same length). When omitted, no document is treated as invalid.

    Returns:
        A frozen :class:`ConceptF1Report`.

    Raises:
        EvalMetricError: When the input sequences are empty or their lengths
            disagree (a corpus-construction bug).
    """
    n_docs = len(gold_per_doc)
    if n_docs == 0:
        raise EvalMetricError("concept_set_micro_f1 requires at least one document")
    if len(predicted_per_doc) != n_docs:
        raise EvalMetricError(
            f"predicted_per_doc / gold_per_doc length mismatch: "
            f"{len(predicted_per_doc)} != {n_docs}"
        )
    if invalid_doc_flags is None:
        flags: Sequence[bool] = [False] * n_docs
    else:
        if len(invalid_doc_flags) != n_docs:
            raise EvalMetricError(
                f"invalid_doc_flags length mismatch: {len(invalid_doc_flags)} != {n_docs}"
            )
        flags = invalid_doc_flags

    true_positives = 0
    false_positives = 0
    false_negatives = 0
    n_invalid_docs = 0

    for i in range(n_docs):
        gold = normalize_concept_pairs(gold_per_doc[i])
        if flags[i]:
            n_invalid_docs += 1
            predicted: set[ConceptPair] = set()  # malformed output → no usable pairs
        else:
            predicted = normalize_concept_pairs(predicted_per_doc[i])
        true_positives += len(predicted & gold)
        false_positives += len(predicted - gold)
        false_negatives += len(gold - predicted)

    precision = (
        true_positives / (true_positives + false_positives)
        if (true_positives + false_positives) > 0
        else 0.0
    )
    recall = (
        true_positives / (true_positives + false_negatives)
        if (true_positives + false_negatives) > 0
        else 0.0
    )
    # micro-F1 = 2·TP / (2·TP + FP + FN) — algebraically identical to the
    # harmonic mean of the micro precision/recall, computed directly to avoid
    # double float rounding.
    f1_denominator = 2 * true_positives + false_positives + false_negatives
    micro_f1 = (2 * true_positives / f1_denominator) if f1_denominator > 0 else 0.0
    invalid_rate = n_invalid_docs / n_docs

    return ConceptF1Report(
        n_docs=n_docs,
        n_invalid_docs=n_invalid_docs,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        precision=precision,
        recall=recall,
        micro_f1=micro_f1,
        invalid_json_or_schema_rate=invalid_rate,
    )


def load_concept_fixture(path: Path | None = None) -> list[ConceptFixtureDoc]:
    """Load + validate the synthetic concept-gate fixture YAML.

    Schema::

        version: 1
        docs:
          - id: synth-billing-001
            text: "..."
            gold:
              - {type: org, key: acmepay}
              - {type: topic, key: billing}

    Every ``gold.type`` must be one of the four concept types
    (:data:`brain.graph_rag.extract.CONCEPT_ENTITY_TYPES`) — ``person`` is a
    fixture-authoring error (people are excluded by design), as is any unknown
    type. Gold pairs are returned canonicalized + deduped per document.

    Args:
        path: Path to the fixture YAML. Defaults to the in-repo synthetic file.

    Returns:
        Parsed list of :class:`ConceptFixtureDoc`.

    Raises:
        EvalCorpusError: On a missing / malformed file, version mismatch, missing
            required fields, an empty gold set, or an invalid ``gold.type``.
    """
    # Lazy import keeps the pure metric path (``from brain.eval import
    # concept_set_micro_f1``) free of any brain.graph_rag dependency; only the
    # fixture loader needs the concept-type allowlist.
    from ..graph_rag.extract import CONCEPT_ENTITY_TYPES

    if path is None:
        path = _DEFAULT_FIXTURE_PATH
    if not path.exists():
        raise EvalCorpusError(f"concept fixture not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EvalCorpusError(f"concept fixture YAML parse error in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise EvalCorpusError(
            f"concept fixture must be a YAML mapping, got {type(raw).__name__}"
        )
    if raw.get("version") != _FIXTURE_VERSION:
        raise EvalCorpusError(
            f"concept fixture version mismatch: expected {_FIXTURE_VERSION}, "
            f"got {raw.get('version')!r}"
        )
    raw_docs = raw.get("docs")
    if not isinstance(raw_docs, list) or not raw_docs:
        raise EvalCorpusError("concept fixture must have a non-empty 'docs' list")

    docs: list[ConceptFixtureDoc] = []
    for i, entry in enumerate(raw_docs):
        if not isinstance(entry, dict):
            raise EvalCorpusError(
                f"concept fixture doc #{i + 1} must be a mapping, "
                f"got {type(entry).__name__}"
            )
        doc_id = entry.get("id")
        text = entry.get("text")
        gold_raw = entry.get("gold")
        if not isinstance(doc_id, str) or not doc_id.strip():
            raise EvalCorpusError(f"concept fixture doc #{i + 1} missing a string 'id'")
        if not isinstance(text, str) or not text.strip():
            raise EvalCorpusError(
                f"concept fixture doc {doc_id!r} missing a non-empty 'text'"
            )
        if not isinstance(gold_raw, list) or not gold_raw:
            raise EvalCorpusError(
                f"concept fixture doc {doc_id!r} must have a non-empty 'gold' list"
            )
        gold_pairs: list[ConceptPair] = []
        for j, pair in enumerate(gold_raw):
            if not isinstance(pair, dict):
                raise EvalCorpusError(
                    f"concept fixture doc {doc_id!r} gold #{j + 1} must be a mapping"
                )
            etype = pair.get("type")
            key = pair.get("key")
            if not isinstance(etype, str) or not isinstance(key, str):
                raise EvalCorpusError(
                    f"concept fixture doc {doc_id!r} gold #{j + 1} needs string "
                    f"'type' and 'key'"
                )
            norm_type = etype.strip().lower()
            if norm_type not in CONCEPT_ENTITY_TYPES:
                raise EvalCorpusError(
                    f"concept fixture doc {doc_id!r} gold #{j + 1} has invalid type "
                    f"{etype!r}; allowed: {', '.join(sorted(CONCEPT_ENTITY_TYPES))} "
                    f"(people are excluded by design)"
                )
            gold_pairs.append((norm_type, key))
        docs.append(
            ConceptFixtureDoc(
                doc_id=doc_id,
                text=text,
                gold_concepts=tuple(sorted(normalize_concept_pairs(gold_pairs))),
            )
        )
    return docs
