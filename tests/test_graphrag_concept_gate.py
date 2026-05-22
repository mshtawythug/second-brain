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
from brain.graph_rag.extract import make_extractor

_EXTRACT_LOGGER = "brain.graph_rag.extract"


@pytest.mark.eval
def test_concept_extractor_gate(caplog: pytest.LogCaptureFixture) -> None:
    """Run the live extractor over the labeled fixture and assert the Q2 gate."""
    cfg = Config.load()

    # Reachability probe — skip (don't fail) when Ollama / the model is down.
    probe = OllamaEnricher(host=cfg.ollama_host, model=cfg.graph_extract_model)
    try:
        probe.extract_entities("Stripe billing and pricing for Project Aurora.")
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
