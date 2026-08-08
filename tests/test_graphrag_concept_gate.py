"""Live concept-extractor eval GATE (wave G2-j; spec §17b decision 2).

Runs the real :class:`brain.graph_rag.extract.OllamaExtractor` over the synthetic
labeled fixture (``tests/eval/concept_extraction_fixture.yaml``) and asserts the
**type-aware concept-set micro-F1** gate:

    micro_f1 >= 0.80  AND  precision >= 0.85  AND  recall >= 0.70
    AND  invalid_json_or_schema_rate == 0

**This gate is a measurement / decision tool, NOT a G2 blocker.** Passing it is
*necessary but not sufficient* to flip ``BRAIN_GRAPH_CONCEPTS`` default-ON — that
is a separate, later, explicit decision. **G2 ships concepts default-OFF
regardless of this gate's outcome** (spec §17b decision 2); this test does not
touch the ``BRAIN_GRAPH_CONCEPTS`` default.

Marked ``@pytest.mark.eval`` so it is **excluded from the default test selection**
(``addopts = "-m 'not eval and not benchmark'"``) — it needs a live local Ollama
+ the ``BRAIN_GRAPH_EXTRACT_MODEL``. Run it explicitly::

    pytest -m eval tests/test_graphrag_concept_gate.py -v -s

It **skips cleanly** (never fails) when Ollama / the extract model is unreachable,
mirroring ``tests/test_eval_harness_live.py``'s embedder-unavailable skip.

Invalid-output detection: ``OllamaExtractor.extract`` is never-raise (it swallows
malformed-JSON chunks and logs a WARN). The gate captures that documented WARN
(``brain.graph_rag.extract`` logger) per document to derive
``invalid_json_or_schema_rate`` — a single extract() call per doc, reading the
same contract ``tests/test_graphrag_extract`` asserts. All fixture content is
synthetic (no PII).
"""
from __future__ import annotations

import logging

import pytest

from brain.config import Config
from brain.enrichment import OllamaEnricher
from brain.errors import EnrichmentError, OllamaUnavailable
from brain.eval.concept_extraction import concept_set_micro_f1, load_concept_fixture
from brain.graph_rag.extract import _normalize_for_presence, make_extractor

_EXTRACT_LOGGER = "brain.graph_rag.extract"

# Entity-SPARSE synthetic documents — generic prose / chatter with NO org,
# project, tool, or topic entities. This is the v2 concept gate's blind spot
# (its fixture was entity-RICH only): on sparse text an 8B model used to copy
# the prompt's few-shot example names. The v3 leakage gate asserts the live
# extractor returns no entity that is not literally present in the document.
# All text is synthetic (no PII).
_SPARSE_LEAKAGE_DOCS: tuple[str, ...] = (
    "Thanks so much for the quick reply. Let's catch up tomorrow afternoon and "
    "figure out the next steps together. Hope you have a relaxing evening.",
    "Sounds good to me. I'll circle back after lunch with a couple of thoughts. "
    "No rush at all on your end — whenever you get a chance is totally fine.",
    "Appreciate you taking the time today. It was great to reconnect after so "
    "long. Let me know if there is anything else you need from my side.",
    "Quick note to say the package arrived safely this morning. Everything "
    "looks great and exactly as expected. Thanks again for sorting it out.",
    "Happy Friday everyone. Reminder that the office will be a little quieter "
    "next week. Wishing you all a restful and enjoyable long weekend ahead.",
)


@pytest.mark.eval
@pytest.mark.live_ollama
def test_concept_extractor_gate(caplog: pytest.LogCaptureFixture) -> None:
    """Run the live extractor over the labeled fixture and assert the Q2 gate."""
    cfg = Config.load()

    # Reachability probe — skip (don't fail) when Ollama / the model is down.
    probe = OllamaEnricher(
        host=cfg.ollama_host,
        model=cfg.graph_extract_model,
        timeout=cfg.enrich_timeout_seconds,
    )
    try:
        probe.extract_entities("Acmepay billing and pricing for Project Aurora.")
    except OllamaUnavailable as exc:
        pytest.skip(f"Ollama / extract model {cfg.graph_extract_model!r} unreachable: {exc}")
    except EnrichmentError:
        # Server is up but disliked the probe JSON — that is not a skip reason;
        # the gate itself measures schema validity over the labeled set.
        pass

    extractor = make_extractor(cfg)
    docs = load_concept_fixture()
    assert docs, "concept fixture is empty"

    predicted_per_doc: list[list[tuple[str, str]]] = []
    gold_per_doc: list[list[tuple[str, str]]] = []
    invalid_flags: list[bool] = []

    for doc in docs:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_EXTRACT_LOGGER):
            entities = extractor.extract(doc.text)
        messages = [record.getMessage() for record in caplog.records]
        if any("Ollama unavailable" in message for message in messages):
            pytest.skip("Ollama became unavailable during the gate run")
        invalid = any("skipping chunk" in message for message in messages)
        predicted_per_doc.append([(e.entity_type, e.canonical_key) for e in entities])
        gold_per_doc.append(list(doc.gold_concepts))
        invalid_flags.append(invalid)

    report = concept_set_micro_f1(
        predicted_per_doc=predicted_per_doc,
        gold_per_doc=gold_per_doc,
        invalid_doc_flags=invalid_flags,
    )

    # Operator telemetry (visible under `pytest -s`); the numbers inform the
    # LATER BRAIN_GRAPH_CONCEPTS flip decision — concepts stay default-OFF for G2.
    print(
        f"\n[concept gate] model={cfg.graph_extract_model} docs={report.n_docs} "
        f"micro_f1={report.micro_f1:.4f} precision={report.precision:.4f} "
        f"recall={report.recall:.4f} "
        f"invalid_rate={report.invalid_json_or_schema_rate:.4f} "
        f"(TP={report.true_positives} FP={report.false_positives} "
        f"FN={report.false_negatives})"
    )

    assert report.passes, (
        "concept extractor eval gate FAILED (spec §17b decision 2): "
        f"{'; '.join(report.failing_conditions())}. "
        "Note: this gate is a DECISION tool — G2 ships BRAIN_GRAPH_CONCEPTS "
        "default-OFF regardless; a failing gate simply means concepts are not "
        "yet ready to be flipped default-ON."
    )


# Synthetic names that the v2 few-shot prompt baked in and that leaked as real
# entities (audit B.1). They appear in NONE of the sparse docs, so v3 must never
# emit them. Lower-cased for a case-insensitive canonical-key compare.
# Strip-all canonical keys (Bug B): these are compared against
# ``entity.canonical_key``, which now has all separators removed, so the
# multi-word leak names are stored glued (``projectmarlin`` / ``projectfalcon``).
_FORMER_LEAK_NAMES: frozenset[str] = frozenset({
    "glasswing",
    "helmwright",
    "quillbase",
    "tessa",
    "projectmarlin",
    "projectfalcon",
    "onboarding",
    "uptime",
})


@pytest.mark.eval
@pytest.mark.live_ollama
def test_concept_extractor_no_leakage_on_sparse_docs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LEAKAGE GATE (live): on entity-sparse prose the v3 extractor emits no
    entity that is not literally present in the document — and specifically none
    of the v2 prompt's example names. This closes the v2 gate's blind spot
    (entity-rich fixture only). Skips cleanly when Ollama is unreachable."""
    cfg = Config.load()

    probe = OllamaEnricher(
        host=cfg.ollama_host,
        model=cfg.graph_extract_model,
        timeout=cfg.enrich_timeout_seconds,
    )
    try:
        probe.extract_entities("Acmepay billing and pricing for Project Aurora.")
    except OllamaUnavailable as exc:
        pytest.skip(
            f"Ollama / extract model {cfg.graph_extract_model!r} unreachable: {exc}"
        )
    except EnrichmentError:
        pass

    extractor = make_extractor(cfg)
    leaked: list[tuple[str, str, str]] = []  # (doc_snippet, type, key)
    absent: list[tuple[str, str, str]] = []

    for text in _SPARSE_LEAKAGE_DOCS:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_EXTRACT_LOGGER):
            entities = extractor.extract(text)
        if any(
            "Ollama unavailable" in record.getMessage() for record in caplog.records
        ):
            pytest.skip("Ollama became unavailable during the leakage gate run")
        normalized_text = _normalize_for_presence(text)
        snippet = text[:40]
        for entity in entities:
            if entity.canonical_key in _FORMER_LEAK_NAMES:
                leaked.append((snippet, entity.entity_type, entity.canonical_key))
            needle = _normalize_for_presence(entity.canonical_key)
            if needle and needle not in normalized_text:
                absent.append((snippet, entity.entity_type, entity.canonical_key))

    print(
        f"\n[leakage gate] model={cfg.graph_extract_model} "
        f"docs={len(_SPARSE_LEAKAGE_DOCS)} leaked={len(leaked)} absent={len(absent)}"
    )
    assert not leaked, f"v2 prompt-example names leaked into v3 output: {leaked}"
    assert not absent, (
        f"v3 extractor emitted entities NOT present in the source text "
        f"(presence-validation regression): {absent}"
    )
