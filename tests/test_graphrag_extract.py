"""Tests for the concept entity extractor (wave G2-b, GraphRAG).

DI throughout: the :class:`brain.graph_rag.extract.OllamaExtractor` composes a
real :class:`brain.enrichment.OllamaEnricher` backed by an
``httpx.MockTransport`` returning canned JSON — no monkey-patching of production
modules (CLAUDE.md rule 13). All entity names are synthetic (no PII).
"""
from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable

import httpx
import pytest

from brain.config import Config
from brain.enrichment import OllamaEnricher
from brain.errors import EnrichmentError
from brain.graph_rag import (
    EntityExtractor,
    ExtractedEntity,
    OllamaExtractor,
    make_extractor,
)
from brain.graph_rag.extract import (
    CONCEPT_ENTITY_TYPES,
    EXTRACTOR_VERSION,
)

_Handler = Callable[[httpx.Request], httpx.Response]


# --------------------------------------------------------------------------- #
# Fakes / helpers (DI, not patching)
# --------------------------------------------------------------------------- #


def _enricher(handler: _Handler, *, model: str = "llama3.1:8b") -> OllamaEnricher:
    return OllamaEnricher(
        host="http://x",
        model=model,
        client=httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler)),
    )


def _ok_entities(entities: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "message": {
                "role": "assistant",
                "content": json.dumps({"entities": entities}),
            },
            "done": True,
        },
    )


def _ok_summary(summary: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": json.dumps({"summary": summary})},
            "done": True,
        },
    )


def _const_handler(response: httpx.Response) -> _Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    return handler


def _extractor(handler: _Handler, **kwargs: object) -> OllamaExtractor:
    return OllamaExtractor(enricher=_enricher(handler), **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Happy path: validation + canonicalization + positions
# --------------------------------------------------------------------------- #


def test_extract_returns_canonicalized_entities_with_positions() -> None:
    text = "We migrated Stripe billing for Project Phoenix. Stripe was central."
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "Stripe", "type": "org"},
                {"name": "Project Phoenix", "type": "project"},
            ]
        )
    )
    out = _extractor(handler).extract(text)

    assert [(e.entity_type, e.canonical_key) for e in out] == [
        ("org", "stripe"),
        ("project", "project phoenix"),
    ]
    stripe = next(e for e in out if e.canonical_key == "stripe")
    phoenix = next(e for e in out if e.canonical_key == "project phoenix")
    # doc words: we migrated stripe billing for project phoenix stripe was central
    #            0  1        2      3       4   5       6       7      8   9
    assert stripe.positions == (2, 7)
    assert stripe.mention_count == 2
    assert stripe.display_name == "Stripe"  # surface form preserved
    assert phoenix.positions == (5,)
    assert phoenix.mention_count == 1


def test_extract_canonical_key_lowercases_and_collapses_whitespace() -> None:
    handler = _const_handler(_ok_entities([{"name": "  Data   Lake  ", "type": "topic"}]))
    out = _extractor(handler).extract("the Data Lake project")
    assert len(out) == 1
    assert out[0].canonical_key == "data lake"
    assert out[0].display_name == "Data   Lake"


def test_extract_drops_name_absent_from_text() -> None:
    """v3 presence validation: a concept the model named but that does NOT appear
    in the source text at all is dropped (kills few-shot prompt-example leakage /
    paraphrased hallucinations)."""
    handler = _const_handler(_ok_entities([{"name": "Kubernetes", "type": "tool"}]))
    out = _extractor(handler).extract("We discussed container orchestration broadly.")
    assert out == []


def test_extract_keeps_substring_present_name_with_empty_positions() -> None:
    """v3 presence validation is a separator-normalized SUBSTRING match (lenient,
    recall-safe): a name that appears only inside a larger word still surfaces,
    even though contiguous-token positions are empty (mention_count floored)."""
    handler = _const_handler(_ok_entities([{"name": "observ", "type": "topic"}]))
    out = _extractor(handler).extract("Observability dashboards were rolled out.")
    assert len(out) == 1
    assert out[0].canonical_key == "observ"
    assert out[0].positions == ()  # 'observ' is not a standalone word token
    assert out[0].mention_count == 1


# --------------------------------------------------------------------------- #
# Dedup + type-awareness
# --------------------------------------------------------------------------- #


def test_extract_dedups_by_entity_type_and_canonical_key() -> None:
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "Stripe", "type": "org"},
                {"name": "stripe", "type": "org"},
                {"name": "STRIPE", "type": "org"},
            ]
        )
    )
    out = _extractor(handler).extract("Stripe Stripe Stripe")
    assert len(out) == 1
    assert out[0].canonical_key == "stripe"
    assert out[0].display_name == "Stripe"  # first-seen surface form


def test_extract_collapses_same_key_across_types_to_highest_precedence() -> None:
    """v3 cross-type dedup: the same canonical name emitted under multiple types
    collapses to ONE node, keeping the highest-precedence type (org > project >
    tool > topic). Prevents graph/community fragmentation (audit B.3/B.4)."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "Acme", "type": "topic"},
                {"name": "Acme", "type": "project"},
                {"name": "Acme", "type": "org"},
            ]
        )
    )
    out = _extractor(handler).extract("Acme Acme")
    assert [(e.entity_type, e.canonical_key) for e in out] == [("org", "acme")]


def test_extract_excludes_people_and_unknown_types() -> None:
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "Person X", "type": "person"},  # excluded — people pipeline
                {"name": "2026-05-21", "type": "date"},  # unknown type
                {"name": "Datadog", "type": "tool"},  # kept
            ]
        )
    )
    out = _extractor(handler).extract("Person X used Datadog on 2026-05-21")
    assert [(e.entity_type, e.canonical_key) for e in out] == [("tool", "datadog")]


# --------------------------------------------------------------------------- #
# Per-doc cap
# --------------------------------------------------------------------------- #


def test_extract_enforces_per_doc_cap_by_mention_frequency() -> None:
    # alpha x3, beta x2, gamma x1, delta absent (mention_count floored to 1).
    text = "alpha alpha alpha beta beta gamma"
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "alpha", "type": "topic"},
                {"name": "beta", "type": "topic"},
                {"name": "gamma", "type": "topic"},
                {"name": "delta", "type": "topic"},
            ]
        )
    )
    out = _extractor(handler, max_entities=2).extract(text)
    assert [e.canonical_key for e in out] == ["alpha", "beta"]


def test_extract_cap_none_keeps_all() -> None:
    handler = _const_handler(
        _ok_entities([{"name": f"topic{i}", "type": "topic"} for i in range(5)])
    )
    # All five names appear in the text so presence validation keeps them.
    out = _extractor(handler, max_entities=None).extract(
        "topic0 topic1 topic2 topic3 topic4"
    )
    assert len(out) == 5


# --------------------------------------------------------------------------- #
# Chunking of long input
# --------------------------------------------------------------------------- #


def test_extract_chunks_long_input_and_merges_results() -> None:
    """A document larger than the chunk budget triggers one model call per chunk;
    results merge + dedup across chunks."""
    from brain.ingest.chunker import chunk_text

    text = " ".join(["stripe billing notes"] * 80)
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _ok_entities([{"name": "Stripe", "type": "org"}])

    enricher = _enricher(handler)
    extractor = OllamaExtractor(
        enricher=enricher, chunk_target_tokens=10, chunk_overlap_tokens=0
    )
    expected_chunks = len(
        chunk_text(
            text,
            target_tokens=10,
            overlap_tokens=0,
            count_tokens=enricher.count_tokens,
        )
    )
    out = extractor.extract(text)
    assert expected_chunks > 1  # the input really was split
    assert call_count == expected_chunks  # one model call per chunk
    assert len(out) == 1  # deduped across chunks
    assert out[0].canonical_key == "stripe"


def test_extract_empty_text_makes_no_model_call() -> None:
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return _ok_entities([])

    out = _extractor(handler).extract("   \n  ")
    assert out == []
    assert call_count == 0


# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #


def test_extractor_version_constant_present_and_folds_model() -> None:
    assert EXTRACTOR_VERSION == "concepts-v3"
    extractor = _extractor(_const_handler(_ok_entities([])))
    assert extractor.version == "llama3.1:8b@concepts-v3"


def test_extractor_version_changes_with_model() -> None:
    extractor = OllamaExtractor(
        enricher=_enricher(_const_handler(_ok_entities([])), model="custom:7b")
    )
    assert extractor.version == "custom:7b@concepts-v3"


def test_concept_entity_types_excludes_person() -> None:
    assert frozenset({"topic", "project", "org", "tool"}) == CONCEPT_ENTITY_TYPES
    assert "person" not in CONCEPT_ENTITY_TYPES


# --------------------------------------------------------------------------- #
# Malformed model output — REGRESSION (never raises, skips bad entries)
# --------------------------------------------------------------------------- #


def test_extract_skips_malformed_entries_keeps_valid() -> None:
    handler = _const_handler(
        _ok_entities(
            [
                "not a dict",
                {"name": "NoType"},  # missing type
                {"type": "topic"},  # missing name
                {"name": 123, "type": "topic"},  # name not str
                {"name": "", "type": "topic"},  # empty name
                {"name": "   ", "type": "topic"},  # whitespace-only name
                {"name": "Bad", "type": "date"},  # invalid type
                {"name": "GoodTopic", "type": "topic", "extra": "junk"},  # valid + junk
            ]
        )
    )
    out = _extractor(handler).extract("a GoodTopic appears here")
    assert [(e.entity_type, e.canonical_key) for e in out] == [("topic", "goodtopic")]


def test_extract_all_malformed_returns_empty() -> None:
    handler = _const_handler(_ok_entities(["junk", {"nope": 1}, 42]))
    out = _extractor(handler).extract("some text")
    assert out == []


def test_extract_entities_not_a_list_skips_chunk(caplog: pytest.LogCaptureFixture) -> None:
    """Model returns ``{"entities": "oops"}`` (valid JSON, wrong shape) →
    extract_entities raises EnrichmentError → the chunk is skipped, never raises."""
    handler = _const_handler(
        httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"entities": "oops not a list"}),
                },
                "done": True,
            },
        )
    )
    with caplog.at_level(logging.WARNING, logger="brain.graph_rag.extract"):
        out = _extractor(handler).extract("some text")
    assert out == []
    assert "skipping chunk" in caplog.text


def test_extract_invalid_json_twice_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two consecutive invalid-JSON responses → EnrichmentError per chunk →
    skipped → empty list, never raises."""
    handler = _const_handler(
        httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "not json"}, "done": True},
        )
    )
    with caplog.at_level(logging.WARNING, logger="brain.graph_rag.extract"):
        out = _extractor(handler).extract("some text")
    assert out == []
    assert "skipping chunk" in caplog.text


# --------------------------------------------------------------------------- #
# Never-raise on Ollama unavailable
# --------------------------------------------------------------------------- #


def test_extract_returns_empty_and_warns_on_connect_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with caplog.at_level(logging.WARNING, logger="brain.graph_rag.extract"):
        out = _extractor(handler).extract("some text")
    assert out == []
    assert "Ollama unavailable" in caplog.text


def test_extract_returns_empty_and_warns_on_5xx(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = _const_handler(httpx.Response(503, text="model still loading"))
    with caplog.at_level(logging.WARNING, logger="brain.graph_rag.extract"):
        out = _extractor(handler).extract("some text")
    assert out == []
    assert "Ollama unavailable" in caplog.text


# --------------------------------------------------------------------------- #
# enricher.extract_entities — request shape + raw passthrough
# --------------------------------------------------------------------------- #


def test_enricher_extract_entities_passes_raw_list_and_sets_num_predict() -> None:
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_entities([{"name": "Stripe", "type": "org"}])

    enricher = _enricher(handler)
    raw = enricher.extract_entities("some text")
    assert raw == [{"name": "Stripe", "type": "org"}]
    body = json.loads(captured[0])
    assert body["format"] == "json"
    assert body["options"]["num_predict"] == 1024  # extraction budget, not 256


def test_enricher_extract_entities_non_list_raises_enrichment_error() -> None:
    handler = _const_handler(_ok_entities("not a list"))
    enricher = _enricher(handler)
    with pytest.raises(EnrichmentError, match="non-list entities"):
        enricher.extract_entities("some text")


# --------------------------------------------------------------------------- #
# summarize_group — best-effort, never-raise (G2-f stub)
# --------------------------------------------------------------------------- #


def test_summarize_group_returns_summary() -> None:
    handler = _const_handler(_ok_summary("Recurring billing-platform work."))
    enricher = _enricher(handler)
    out = enricher.summarize_group(
        person="Pat Synth",
        entity_names=["stripe", "billing"],
        doc_titles=["Q2 billing review"],
    )
    assert out == "Recurring billing-platform work."


def test_summarize_group_returns_none_and_warns_on_connect_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    enricher = _enricher(handler)
    with caplog.at_level(logging.WARNING, logger="brain.enrichment"):
        out = enricher.summarize_group(
            person=None, entity_names=["stripe"], doc_titles=[]
        )
    assert out is None
    assert "summary=None" in caplog.text


def test_summarize_group_returns_none_on_invalid_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = _const_handler(
        httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "not json"}, "done": True},
        )
    )
    enricher = _enricher(handler)
    with caplog.at_level(logging.WARNING, logger="brain.enrichment"):
        out = enricher.summarize_group(
            person="Pat Synth", entity_names=["x"], doc_titles=["t"]
        )
    assert out is None
    assert "summary=None" in caplog.text


def test_summarize_group_returns_none_on_empty_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = _const_handler(_ok_summary("   "))
    enricher = _enricher(handler)
    with caplog.at_level(logging.WARNING, logger="brain.enrichment"):
        out = enricher.summarize_group(
            person="Pat Synth", entity_names=["x"], doc_titles=["t"]
        )
    assert out is None
    assert "summary=None" in caplog.text


# --------------------------------------------------------------------------- #
# Factory + Protocol + dataclass invariants
# --------------------------------------------------------------------------- #


def test_make_extractor_uses_graph_extract_model() -> None:
    cfg = Config(database_url="postgresql://x/y", graph_extract_model="custom:7b")
    extractor = make_extractor(cfg)
    assert isinstance(extractor, OllamaExtractor)
    assert extractor.version == "custom:7b@concepts-v3"


def test_make_extractor_threads_max_entities() -> None:
    cfg = Config(database_url="postgresql://x/y", graph_max_entities=2)
    extractor = make_extractor(cfg)
    assert extractor._max_entities == 2


def test_ollama_extractor_satisfies_entity_extractor_protocol() -> None:
    extractor = _extractor(_const_handler(_ok_entities([])))
    assert isinstance(extractor, EntityExtractor)


def test_extracted_entity_is_frozen() -> None:
    entity = ExtractedEntity(entity_type="topic", canonical_key="x", display_name="X")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entity.entity_type = "org"  # type: ignore[misc]


def test_invalid_max_entities_raises_value_error() -> None:
    with pytest.raises(ValueError, match="max_entities"):
        OllamaExtractor(
            enricher=_enricher(_const_handler(_ok_entities([]))), max_entities=0
        )


# --------------------------------------------------------------------------- #
# G2 quality fixes (task #53): type-label normalization, noise filter, project
# prefix repair, conservative project substring dedup, timeout threading.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw_type", "expected_type"),
    [
        ("organization", "org"),
        ("Organisation", "org"),
        ("company", "org"),
        ("vendor", "org"),
        ("software", "tool"),
        ("framework", "tool"),
        ("library", "tool"),
        ("application", "tool"),
        ("initiative", "project"),
        ("effort", "project"),
        ("theme", "topic"),
        ("subject", "topic"),
    ],
)
def test_extract_normalizes_synonym_type_labels(
    raw_type: str, expected_type: str
) -> None:
    """Near-synonym type labels map to the canonical type (recall: not dropped)."""
    handler = _const_handler(_ok_entities([{"name": "Glasswing", "type": raw_type}]))
    out = _extractor(handler).extract("We adopted Glasswing this quarter.")
    assert [(e.entity_type, e.canonical_key) for e in out] == [
        (expected_type, "glasswing")
    ]


def test_extract_unknown_type_still_dropped_after_normalization() -> None:
    """A label that is neither canonical nor a known synonym is still dropped."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "2026-05-22", "type": "date"},  # unknown -> dropped
                {"name": "Glasswing", "type": "vendor"},  # synonym -> org
            ]
        )
    )
    out = _extractor(handler).extract("Glasswing on 2026-05-22")
    assert [(e.entity_type, e.canonical_key) for e in out] == [("org", "glasswing")]


def test_extract_drops_single_char_key_keeps_two_char_acronym() -> None:
    """Min-key-len floor is 2: 1-char noise dropped, 2-char acronym kept."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "x", "type": "topic"},  # 1 char -> dropped
                {"name": "ML", "type": "topic"},  # 2 char -> kept
            ]
        )
    )
    out = _extractor(handler).extract("x and ML were discussed")
    assert [(e.entity_type, e.canonical_key) for e in out] == [("topic", "ml")]


def test_extract_drops_generic_stop_words() -> None:
    """Exact-match generic stop words are filtered; real concepts survive."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "platform", "type": "topic"},  # generic -> dropped
                {"name": "Dashboard", "type": "topic"},  # generic -> dropped
                {"name": "billing", "type": "topic"},  # real -> kept
            ]
        )
    )
    out = _extractor(handler).extract("the billing platform dashboard")
    assert [(e.entity_type, e.canonical_key) for e in out] == [("topic", "billing")]


def test_extract_repairs_stripped_project_prefix() -> None:
    """A bare project name is restored to 'Project X' when the doc names it so."""
    handler = _const_handler(_ok_entities([{"name": "Helios", "type": "project"}]))
    out = _extractor(handler).extract("Project Helios shipped the migration.")
    assert len(out) == 1
    assert out[0].entity_type == "project"
    assert out[0].canonical_key == "project helios"
    assert out[0].display_name == "Project Helios"


def test_extract_project_prefix_repair_only_when_present_in_text() -> None:
    """No spurious 'Project ' prefix when the doc does not use that form."""
    handler = _const_handler(_ok_entities([{"name": "Helios", "type": "project"}]))
    out = _extractor(handler).extract("Helios shipped the migration.")
    assert out[0].canonical_key == "helios"


def test_extract_dedupes_project_substring_keeping_longer() -> None:
    """A project subsumed by a longer 'Project X …' sibling is dropped — both
    forms appear in the text, so presence validation keeps them and the
    conservative project substring dedup collapses to the longer named form."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "Project Helios", "type": "project"},
                {"name": "Project Helios Phase Two", "type": "project"},
            ]
        )
    )
    out = _extractor(handler).extract(
        "Project Helios and Project Helios Phase Two both shipped."
    )
    keys = sorted(e.canonical_key for e in out if e.entity_type == "project")
    assert keys == ["project helios phase two"]


def test_extract_presence_drops_absent_longer_project_form() -> None:
    """v3 presence validation: when the text says only 'Helios' (never 'Project
    Helios'), the model's invented 'Project Helios' form is dropped as not
    present, leaving the bare name that actually appears."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "Helios", "type": "project"},
                {"name": "Project Helios", "type": "project"},
            ]
        )
    )
    out = _extractor(handler).extract("We discussed Helios at length.")
    keys = sorted(e.canonical_key for e in out if e.entity_type == "project")
    assert keys == ["helios"]


def test_extract_substring_dedup_not_applied_to_topics() -> None:
    """Topics are NOT substring-deduped — a shorter topic is often the right one."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "billing", "type": "topic"},
                {"name": "billing platform", "type": "topic"},
            ]
        )
    )
    out = _extractor(handler).extract("billing and billing platform discussed")
    keys = sorted(e.canonical_key for e in out if e.entity_type == "topic")
    assert keys == ["billing", "billing platform"]


def test_make_extractor_threads_enrich_timeout() -> None:
    """make_extractor threads cfg.enrich_timeout_seconds (not the 60s default)."""
    cfg = Config(database_url="postgresql://x/y", enrich_timeout_seconds=137.0)
    extractor = make_extractor(cfg)
    assert extractor._enricher._client.timeout.read == 137.0


def test_extract_project_prefix_repair_rejects_substring_false_positive() -> None:
    """A bare project is NOT promoted on a substring-only match: 'Project
    Helioscope' in the text must not turn 'Helios' into 'Project Helios'."""
    handler = _const_handler(_ok_entities([{"name": "Helios", "type": "project"}]))
    out = _extractor(handler).extract("Project Helioscope shipped the rollout.")
    assert len(out) == 1
    assert out[0].entity_type == "project"
    assert out[0].canonical_key == "helios"  # NOT 'project helios'


@pytest.mark.parametrize(
    "ambiguous_type",
    ["platform", "technology", "domain", "area", "app", "language", "infrastructure"],
)
def test_extract_drops_ambiguous_non_synonym_type_labels(ambiguous_type: str) -> None:
    """Ambiguous words excluded from the synonym map are NOT normalized — the
    entity is dropped rather than mis-typed (Codex review: narrow synonym set)."""
    handler = _const_handler(
        _ok_entities([{"name": "Glasswing", "type": ambiguous_type}])
    )
    out = _extractor(handler).extract("We use Glasswing daily.")
    assert out == []


# --------------------------------------------------------------------------- #
# v3 data-quality fixes (Phase 2): few-shot leakage kill (presence validation),
# reasoning-text rejection, cross-type dedup, structural-junk filtering.
# All entity names below are SYNTHETIC (no PII).
# --------------------------------------------------------------------------- #


# Entity-sparse prose: generic chatter with NO org/project/tool/topic entities —
# the exact gap the v2 concept gate (entity-RICH only) missed.
_SPARSE_TEXTS = [
    "Thanks for the note. Let's sync tomorrow and figure out next steps.",
    "Sounds good to me. I'll circle back after lunch with a few thoughts.",
    "Appreciate the quick turnaround. Talk soon and have a great weekend.",
]

# Synthetic example-style names a buggy small model copies from its prompt /
# prior context on a sparse document (the leakage failure mode). NONE of these
# appear in the _SPARSE_TEXTS above, so presence validation must drop them all.
_LEAKED_NAMES = [
    {"name": "Glasswing", "type": "org"},
    {"name": "Quillbase", "type": "tool"},
    {"name": "Project Marlin", "type": "project"},
    {"name": "Helmwright", "type": "tool"},
    {"name": "onboarding", "type": "topic"},
]


@pytest.mark.parametrize("sparse_text", _SPARSE_TEXTS)
def test_extract_leakage_gate_drops_all_absent_names(sparse_text: str) -> None:
    """LEAKAGE GATE (deterministic): on entity-sparse prose, a model that emits
    invented/example names not present in the text yields ZERO entities. This is
    the v3 presence-validation kill for few-shot prompt-example leakage."""
    handler = _const_handler(_ok_entities(_LEAKED_NAMES))
    out = _extractor(handler).extract(sparse_text)
    assert out == []


def test_extract_presence_keeps_present_drops_absent() -> None:
    """Mixed batch: only names that literally appear in the text survive."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "Acme Metrics", "type": "tool"},  # present -> kept
                {"name": "Glasswing", "type": "org"},  # absent -> dropped
                {"name": "observability", "type": "topic"},  # present -> kept
                {"name": "Quillbase", "type": "tool"},  # absent -> dropped
            ]
        )
    )
    out = _extractor(handler).extract(
        "We rolled out Acme Metrics dashboards to improve observability."
    )
    assert [(e.entity_type, e.canonical_key) for e in out] == [
        ("tool", "acme metrics"),
        ("topic", "observability"),
    ]


def test_extract_presence_match_is_separator_normalized() -> None:
    """A hyphenated concept present in the text matches its whitespace-collapsed
    canonical key (separator-normalized substring), not dropped spuriously."""
    handler = _const_handler(_ok_entities([{"name": "back-pressure", "type": "topic"}]))
    out = _extractor(handler).extract("Throughput and back-pressure were discussed.")
    assert [(e.entity_type, e.canonical_key) for e in out] == [("topic", "back-pressure")]


def test_extract_rejects_reasoning_text_as_entity() -> None:
    """A model that emits its own meta-commentary as an 'entity' has it dropped
    (sentence length + reasoning-phrase patterns), keeping the real concept."""
    handler = _const_handler(
        _ok_entities(
            [
                {
                    "name": "Glasswing is not present in the text. However, the "
                    "document discusses billing.",
                    "type": "topic",
                },
                {"name": "billing", "type": "topic"},  # the real concept
            ]
        )
    )
    out = _extractor(handler).extract("The billing migration finished this sprint.")
    assert [(e.entity_type, e.canonical_key) for e in out] == [("topic", "billing")]


@pytest.mark.parametrize(
    "reasoning_name",
    [
        "There are no concept entities in this text",
        "The document does not appear to mention any tools",
        "N/A",
        "None",
        "I cannot identify any organizations here",
    ],
)
def test_extract_drops_reasoning_phrases(reasoning_name: str) -> None:
    """Generic reasoning / meta-commentary phrases never become entities."""
    handler = _const_handler(_ok_entities([{"name": reasoning_name, "type": "topic"}]))
    # Echo the reasoning text into the doc so this isolates the reasoning filter
    # (not presence validation): even when 'present', reasoning text is dropped.
    out = _extractor(handler).extract(f"Context: {reasoning_name} -- end.")
    assert out == []


@pytest.mark.parametrize(
    ("types", "winner"),
    [
        (["topic", "org"], "org"),
        (["topic", "project"], "project"),
        (["tool", "project"], "project"),
        (["topic", "tool"], "tool"),
        (["topic", "project", "org", "tool"], "org"),
    ],
)
def test_extract_cross_type_precedence(types: list[str], winner: str) -> None:
    """The same canonical name across types collapses to the single
    highest-precedence type (org > project > tool > topic)."""
    handler = _const_handler(
        _ok_entities([{"name": "Helix", "type": t} for t in types])
    )
    out = _extractor(handler).extract("Helix Helix Helix")
    assert [(e.entity_type, e.canonical_key) for e in out] == [(winner, "helix")]


def test_extract_cross_type_dedup_preserves_distinct_names() -> None:
    """Cross-type dedup only collapses SAME-named entities — distinct concepts
    under different types are untouched."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "Acme", "type": "org"},
                {"name": "Acme", "type": "topic"},  # collapses into the org
                {"name": "billing", "type": "topic"},  # distinct -> kept
            ]
        )
    )
    out = _extractor(handler).extract("Acme rebuilt its billing system.")
    assert sorted((e.entity_type, e.canonical_key) for e in out) == [
        ("org", "acme"),
        ("topic", "billing"),
    ]


@pytest.mark.parametrize(
    "junk_name",
    ["PDF", "pdf", "DOCX", "Chapter 24", "Section 3", "Page 12", "Figure 2",
     "Appendix A", "Standard 72", "standard", "standards", "Table 5"],
)
def test_extract_drops_structural_junk_entities(junk_name: str) -> None:
    """Document-structure / file-format noise is never a concept (audit B.5)."""
    handler = _const_handler(_ok_entities([{"name": junk_name, "type": "topic"}]))
    # Put the junk in the text so presence passes — this isolates the structural
    # filter (the junk is 'present' yet still dropped as structural noise).
    out = _extractor(handler).extract(f"See {junk_name} for the relevant details.")
    assert out == []


def test_extract_structural_filter_keeps_real_concepts() -> None:
    """The structural filter is generic — it drops noise but keeps real concepts
    that happen to sit beside structural words."""
    handler = _const_handler(
        _ok_entities(
            [
                {"name": "PDF", "type": "topic"},  # structural -> dropped
                {"name": "Chapter 24", "type": "topic"},  # structural -> dropped
                {"name": "compliance", "type": "topic"},  # real -> kept
            ]
        )
    )
    out = _extractor(handler).extract(
        "Chapter 24 of the PDF covers our compliance program."
    )
    assert [(e.entity_type, e.canonical_key) for e in out] == [("topic", "compliance")]
