"""Pure unit tests for the concept-extractor eval gate metric (wave G2-j).

No Ollama, no DB — exact micro-F1 / precision / recall / invalid-rate numbers on
hand-constructed predicted-vs-gold pair sets, plus the in-repo synthetic-fixture
loader's happy path and every validation branch. All entity names are synthetic
(no PII). Mirrors ``tests/test_eval_metrics``-style pure metric testing.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from brain.eval.concept_extraction import (
    PASS_INVALID_RATE,
    PASS_MICRO_F1,
    PASS_PRECISION,
    PASS_RECALL,
    ConceptF1Report,
    ConceptFixtureDoc,
    concept_set_micro_f1,
    load_concept_fixture,
    normalize_concept_pairs,
)
from brain.eval.errors import EvalCorpusError, EvalMetricError


# --------------------------------------------------------------------------- #
# normalize_concept_pairs — canonicalization, type-awareness, people-exclusion
# --------------------------------------------------------------------------- #
def test_normalize_lowercases_and_collapses_whitespace() -> None:
    out = normalize_concept_pairs([("ORG", "  Acmepay   Inc "), ("Topic", "Billing")])
    assert out == {("org", "acmepay inc"), ("topic", "billing")}


def test_normalize_excludes_people() -> None:
    out = normalize_concept_pairs([("person", "alice"), ("topic", "billing")])
    assert out == {("topic", "billing")}


def test_normalize_same_key_different_type_are_distinct() -> None:
    out = normalize_concept_pairs([("org", "acme"), ("project", "acme")])
    assert out == {("org", "acme"), ("project", "acme")}


def test_normalize_drops_empty_type_or_key() -> None:
    out = normalize_concept_pairs([("", "billing"), ("topic", "   "), ("topic", "ok")])
    assert out == {("topic", "ok")}


def test_normalize_dedups() -> None:
    out = normalize_concept_pairs([("org", "Acmepay"), ("ORG", "acmepay")])
    assert out == {("org", "acmepay")}


# --------------------------------------------------------------------------- #
# concept_set_micro_f1 — exact numbers
# --------------------------------------------------------------------------- #
def test_perfect_prediction_scores_one() -> None:
    gold = [[("org", "acmepay"), ("topic", "billing")], [("topic", "roadmap")]]
    report = concept_set_micro_f1(predicted_per_doc=gold, gold_per_doc=gold)
    assert report.precision == 1.0
    assert report.recall == 1.0
    assert report.micro_f1 == 1.0
    assert report.true_positives == 3
    assert report.false_positives == 0
    assert report.false_negatives == 0
    assert report.invalid_json_or_schema_rate == 0.0
    assert report.passes is True


def test_mixed_prediction_exact_micro_numbers() -> None:
    # Doc1: TP=acmepay, FP=pricing, FN=billing.
    # Doc2: TP=roadmap, FP=analytics, FN=0.
    predicted = [
        [("org", "acmepay"), ("topic", "pricing")],
        [("topic", "roadmap"), ("topic", "analytics")],
    ]
    gold = [
        [("org", "acmepay"), ("topic", "billing")],
        [("topic", "roadmap")],
    ]
    report = concept_set_micro_f1(predicted_per_doc=predicted, gold_per_doc=gold)
    assert report.true_positives == 2
    assert report.false_positives == 2
    assert report.false_negatives == 1
    assert report.precision == 0.5
    assert math.isclose(report.recall, 2 / 3)
    assert math.isclose(report.micro_f1, 4 / 7)  # 2*TP / (2*TP + FP + FN)
    assert report.passes is False


def test_type_awareness_counts_pairs_separately() -> None:
    # Predict (project, acme) but gold is (org, acme): no overlap.
    report = concept_set_micro_f1(
        predicted_per_doc=[[("project", "acme")]],
        gold_per_doc=[[("org", "acme")]],
    )
    assert report.true_positives == 0
    assert report.false_positives == 1
    assert report.false_negatives == 1
    assert report.micro_f1 == 0.0


def test_people_excluded_from_scoring() -> None:
    # A predicted person pair is ignored entirely (not an FP).
    report = concept_set_micro_f1(
        predicted_per_doc=[[("person", "alice"), ("topic", "billing")]],
        gold_per_doc=[[("topic", "billing")]],
    )
    assert report.true_positives == 1
    assert report.false_positives == 0
    assert report.false_negatives == 0
    assert report.precision == 1.0


def test_invalid_doc_counts_toward_rate_and_ignores_predictions() -> None:
    # Doc2 is flagged invalid: even though its predicted pair MATCHES gold, it is
    # ignored (treated as empty) → that gold pair becomes a false negative.
    predicted = [[("org", "acmepay")], [("topic", "billing")]]
    gold = [[("org", "acmepay")], [("topic", "billing")]]
    report = concept_set_micro_f1(
        predicted_per_doc=predicted,
        gold_per_doc=gold,
        invalid_doc_flags=[False, True],
    )
    assert report.n_invalid_docs == 1
    assert report.invalid_json_or_schema_rate == 0.5
    assert report.true_positives == 1  # only doc1's acmepay
    assert report.false_negatives == 1  # doc2's billing
    assert report.false_positives == 0
    assert report.passes is False  # invalid_rate > 0 fails the gate


def test_empty_predictions_yield_zero_recall() -> None:
    report = concept_set_micro_f1(
        predicted_per_doc=[[]],
        gold_per_doc=[[("topic", "billing")]],
    )
    assert report.precision == 0.0  # no TP, no FP → defined as 0.0
    assert report.recall == 0.0
    assert report.micro_f1 == 0.0


def test_empty_corpus_raises() -> None:
    with pytest.raises(EvalMetricError, match="at least one document"):
        concept_set_micro_f1(predicted_per_doc=[], gold_per_doc=[])


def test_length_mismatch_raises() -> None:
    with pytest.raises(EvalMetricError, match="length mismatch"):
        concept_set_micro_f1(
            predicted_per_doc=[[("org", "acmepay")]],
            gold_per_doc=[[("org", "acmepay")], [("topic", "billing")]],
        )


def test_invalid_flags_length_mismatch_raises() -> None:
    with pytest.raises(EvalMetricError, match="invalid_doc_flags length mismatch"):
        concept_set_micro_f1(
            predicted_per_doc=[[("org", "acmepay")]],
            gold_per_doc=[[("org", "acmepay")]],
            invalid_doc_flags=[False, False],
        )


# --------------------------------------------------------------------------- #
# ConceptF1Report.passes — threshold boundaries
# --------------------------------------------------------------------------- #
def _report(
    *, micro_f1: float, precision: float, recall: float, invalid_rate: float
) -> ConceptF1Report:
    return ConceptF1Report(
        n_docs=1,
        n_invalid_docs=0,
        true_positives=0,
        false_positives=0,
        false_negatives=0,
        precision=precision,
        recall=recall,
        micro_f1=micro_f1,
        invalid_json_or_schema_rate=invalid_rate,
    )


def test_passes_exactly_at_thresholds() -> None:
    report = _report(
        micro_f1=PASS_MICRO_F1,
        precision=PASS_PRECISION,
        recall=PASS_RECALL,
        invalid_rate=PASS_INVALID_RATE,
    )
    assert report.passes is True
    assert report.failing_conditions() == []


def test_fails_below_each_threshold() -> None:
    assert not _report(
        micro_f1=0.799, precision=0.9, recall=0.9, invalid_rate=0.0
    ).passes
    assert not _report(
        micro_f1=0.9, precision=0.849, recall=0.9, invalid_rate=0.0
    ).passes
    assert not _report(
        micro_f1=0.9, precision=0.9, recall=0.699, invalid_rate=0.0
    ).passes
    assert not _report(
        micro_f1=0.9, precision=0.9, recall=0.9, invalid_rate=0.001
    ).passes


def test_failing_conditions_lists_all_failures() -> None:
    failures = _report(
        micro_f1=0.1, precision=0.1, recall=0.1, invalid_rate=0.5
    ).failing_conditions()
    assert len(failures) == 4
    joined = " ".join(failures)
    assert "micro_f1" in joined
    assert "precision" in joined
    assert "recall" in joined
    assert "invalid_json_or_schema_rate" in joined


# --------------------------------------------------------------------------- #
# load_concept_fixture — happy path (the shipped synthetic fixture)
# --------------------------------------------------------------------------- #
def test_load_shipped_fixture_parses_synthetic_docs() -> None:
    docs = load_concept_fixture()
    assert len(docs) >= 6  # the shipped synthetic set
    assert all(isinstance(d, ConceptFixtureDoc) for d in docs)
    first = next(d for d in docs if d.doc_id == "synth-billing-001")
    # Gold is canonicalized + sorted; all four concept types only (no person).
    assert ("org", "acmepay") in first.gold_concepts
    assert ("topic", "billing") in first.gold_concepts
    assert list(first.gold_concepts) == sorted(first.gold_concepts)
    for doc in docs:
        for etype, _key in doc.gold_concepts:
            assert etype in {"topic", "project", "org", "tool"}
            assert etype != "person"


# --------------------------------------------------------------------------- #
# load_concept_fixture — validation branches
# --------------------------------------------------------------------------- #
def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "fixture.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(EvalCorpusError, match="not found"):
        load_concept_fixture(tmp_path / "nope.yaml")


def test_load_non_mapping_root_raises(tmp_path: Path) -> None:
    with pytest.raises(EvalCorpusError, match="must be a YAML mapping"):
        load_concept_fixture(_write(tmp_path, "- just\n- a\n- list\n"))


def test_load_version_mismatch_raises(tmp_path: Path) -> None:
    with pytest.raises(EvalCorpusError, match="version mismatch"):
        load_concept_fixture(_write(tmp_path, "version: 99\ndocs: []\n"))


def test_load_docs_not_list_raises(tmp_path: Path) -> None:
    with pytest.raises(EvalCorpusError, match="non-empty 'docs' list"):
        load_concept_fixture(_write(tmp_path, "version: 1\ndocs: {}\n"))


def test_load_empty_docs_raises(tmp_path: Path) -> None:
    with pytest.raises(EvalCorpusError, match="non-empty 'docs' list"):
        load_concept_fixture(_write(tmp_path, "version: 1\ndocs: []\n"))


def test_load_doc_not_mapping_raises(tmp_path: Path) -> None:
    with pytest.raises(EvalCorpusError, match="must be a mapping"):
        load_concept_fixture(_write(tmp_path, "version: 1\ndocs:\n  - just a string\n"))


def test_load_missing_id_raises(tmp_path: Path) -> None:
    body = "version: 1\ndocs:\n  - text: hi\n    gold:\n      - {type: org, key: x}\n"
    with pytest.raises(EvalCorpusError, match="missing a string 'id'"):
        load_concept_fixture(_write(tmp_path, body))


def test_load_missing_text_raises(tmp_path: Path) -> None:
    body = "version: 1\ndocs:\n  - id: d1\n    gold:\n      - {type: org, key: x}\n"
    with pytest.raises(EvalCorpusError, match="non-empty 'text'"):
        load_concept_fixture(_write(tmp_path, body))


def test_load_missing_gold_raises(tmp_path: Path) -> None:
    body = "version: 1\ndocs:\n  - id: d1\n    text: hi\n    gold: []\n"
    with pytest.raises(EvalCorpusError, match="non-empty 'gold' list"):
        load_concept_fixture(_write(tmp_path, body))


def test_load_gold_entry_not_mapping_raises(tmp_path: Path) -> None:
    body = "version: 1\ndocs:\n  - id: d1\n    text: hi\n    gold:\n      - notamap\n"
    with pytest.raises(EvalCorpusError, match="must be a mapping"):
        load_concept_fixture(_write(tmp_path, body))


def test_load_gold_non_string_fields_raises(tmp_path: Path) -> None:
    body = (
        "version: 1\ndocs:\n  - id: d1\n    text: hi\n"
        "    gold:\n      - {type: 1, key: 2}\n"
    )
    with pytest.raises(EvalCorpusError, match="string"):
        load_concept_fixture(_write(tmp_path, body))


def test_load_gold_person_type_rejected(tmp_path: Path) -> None:
    body = (
        "version: 1\ndocs:\n  - id: d1\n    text: hi\n"
        "    gold:\n      - {type: person, key: alice}\n"
    )
    with pytest.raises(EvalCorpusError, match="invalid type"):
        load_concept_fixture(_write(tmp_path, body))


def test_load_gold_unknown_type_rejected(tmp_path: Path) -> None:
    # 'date' is not a concept type; key is a plain string so the loader reaches
    # the type-allowlist check (not the string-field check).
    body = (
        "version: 1\ndocs:\n  - id: d1\n    text: hi\n"
        "    gold:\n      - {type: date, key: release}\n"
    )
    with pytest.raises(EvalCorpusError, match="invalid type"):
        load_concept_fixture(_write(tmp_path, body))


def test_load_invalid_yaml_raises(tmp_path: Path) -> None:
    with pytest.raises(EvalCorpusError, match="parse error"):
        load_concept_fixture(_write(tmp_path, "version: 1\ndocs: [unclosed\n"))
