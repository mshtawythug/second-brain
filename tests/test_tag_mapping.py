"""Pure tests for :mod:`brain.tag_mapping`: mappings and merge suggestions (RM-21).

No database. The per-document loop and the saved-search rewrite are exercised through the CLI in
``tests/test_cli_tags.py`` and ``tests/test_normalize_tags_golden.py``. All tags are synthetic.
"""

from __future__ import annotations

import itertools
import random
from pathlib import Path

import pytest

from brain.set_similarity import jaccard
from brain.tag_mapping import (
    JACCARD_THRESHOLD,
    MIN_SHARED_DOCS,
    SIMILARITY_THRESHOLD,
    MergeSuggestion,
    TagMappingError,
    apply_tag_mapping,
    build_merge_mapping,
    build_rename_mapping,
    load_tag_mapping,
    parse_tag_mapping,
    suggest_merges,
)

# --- mappings -----------------------------------------------------------------------------------


def test_parse_normalizes_keys_and_values_and_drops_empty_entries() -> None:
    raw = '{"Big Widgets": "Big_Widget", "---": "x", "y": "  "}'

    assert parse_tag_mapping(raw) == {"big-widgets": "big-widget"}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{nope", "mapping file is not valid JSON"),
        ('["a"]', "mapping file must be a JSON object of {from: to} strings"),
        ('{"a": ["b"]}', "mapping entries must be strings; got str → list"),
    ],
)
def test_parse_refuses_a_bad_payload(raw: str, message: str) -> None:
    with pytest.raises(TagMappingError) as excinfo:
        parse_tag_mapping(raw)

    assert str(excinfo.value).startswith(message)


def test_load_refuses_an_unreadable_path(tmp_path: Path) -> None:
    with pytest.raises(TagMappingError) as excinfo:
        load_tag_mapping(tmp_path / "absent.json")

    assert str(excinfo.value).startswith("could not read mapping file: ")


def test_apply_maps_case_insensitively_and_canonicalizes_unmapped_tags() -> None:
    assert apply_tag_mapping(["Widgets", "Other Tag"], {"widgets": "widget"}) == [
        "widget",
        "other-tag",
    ]


def test_rename_mapping_is_normalized_and_refuses_same_or_empty() -> None:
    assert build_rename_mapping("Old Tag", "New_Tag") == {"old-tag": "new-tag"}
    for old, new in (("a", "A"), ("__", "b"), ("a", " ")):
        with pytest.raises(TagMappingError):
            build_rename_mapping(old, new)


def test_merge_mapping_drops_the_target_and_repeats_and_refuses_nothing_to_do() -> None:
    assert build_merge_mapping(["A", "b", "a", "C"], into="c") == {"a": "c", "b": "c"}
    with pytest.raises(TagMappingError):
        build_merge_mapping(["c", "C"], into="c")
    with pytest.raises(TagMappingError):
        build_merge_mapping(["a", "__"], into="c")


# --- suggestions --------------------------------------------------------------------------------


def _pairs(suggestions: list[MergeSuggestion]) -> dict[tuple[str, str], MergeSuggestion]:
    return {(s.keep, s.merge): s for s in suggestions}


@pytest.mark.parametrize(
    ("singular", "plural"),
    [
        ("widget", "widgets"),
        ("box", "boxes"),
        ("policy", "policies"),
        ("data-model", "data-models"),
    ],
)
def test_singular_plural_pairs(singular: str, plural: str) -> None:
    got = _pairs(suggest_merges({"d1": [singular], "d2": [singular], "d3": [plural]}))

    assert "singular-plural" in got[(singular, plural)].reasons


def test_es_is_stripped_only_after_a_sibilant_and_ss_is_not_a_plural() -> None:
    # "notes" → "not" is not a plural rule; "boss" is not the plural of "bos".
    got = _pairs(suggest_merges({"d1": ["not", "boss"], "d2": ["notes", "bos"]}))

    assert all("singular-plural" not in s.reasons for s in got.values())


def test_similar_spelling_uses_the_threshold() -> None:
    # "deployment"/"deploymnet": ratio 0.9; "planning"/"plotting": ratio 0.625.
    got = _pairs(
        suggest_merges({"d1": ["deployment", "planning"], "d2": ["deploymnet", "plotting"]})
    )

    pair = got[("deployment", "deploymnet")]
    assert pair.reasons == ("similar-spelling",)
    assert pair.ratio >= SIMILARITY_THRESHOLD
    assert ("planning", "plotting") not in got and ("plotting", "planning") not in got


def test_co_occurrence_needs_the_overlap_and_the_shared_documents() -> None:
    doc_tags = {
        "d1": ["roadmap", "planning", "solo-a", "solo-b"],
        "d2": ["roadmap", "planning"],
        "d3": ["roadmap", "planning"],
        "d4": ["roadmap", "planning"],
        "d5": ["roadmap"],
    }

    got = _pairs(suggest_merges(doc_tags))

    # roadmap {d1..d5} vs planning {d1..d4}: Jaccard 0.8, 4 shared → suggested.
    co = got[("roadmap", "planning")]
    assert co.reasons == ("co-occurrence",)
    assert (co.overlap, co.shared) == (JACCARD_THRESHOLD, 4)
    assert (co.keep_count, co.merge_count) == (5, 4)
    # solo-a / solo-b overlap at 1.0 on ONE document: below MIN_SHARED_DOCS, not suggested.
    assert MIN_SHARED_DOCS == 2
    assert ("solo-a", "solo-b") not in got


def test_co_occurrence_below_the_jaccard_threshold_is_not_suggested() -> None:
    # 3 shared of 4 in the union: Jaccard 0.75.
    doc_tags = {"d1": ["x", "yy"], "d2": ["x", "yy"], "d3": ["x", "yy"], "d4": ["x"]}

    assert suggest_merges(doc_tags) == []


def test_keep_is_the_more_used_tag_then_the_shorter() -> None:
    by_count = _pairs(suggest_merges({"d1": ["widgets"], "d2": ["widgets"], "d3": ["widget"]}))
    tied = _pairs(suggest_merges({"d1": ["widgets"], "d2": ["widget"]}))

    assert ("widgets", "widget") in by_count
    assert ("widget", "widgets") in tied


def test_ranked_by_number_of_reasons_then_strength_and_limited() -> None:
    doc_tags = {
        # widget/widgets: plural + spelling + co-occurrence (3 reasons).
        "d1": ["widget", "widgets", "deployment"],
        "d2": ["widget", "widgets"],
        # deployment/deploymnet: spelling only (1 reason).
        "d3": ["deploymnet"],
    }

    ranked = suggest_merges(doc_tags)
    first = suggest_merges(doc_tags, limit=1)

    assert [(s.keep, s.merge) for s in ranked] == [
        ("widget", "widgets"),
        ("deployment", "deploymnet"),
    ]
    assert ranked[0].reasons == ("similar-spelling", "singular-plural", "co-occurrence")
    assert first == ranked[:1]


def test_to_json_carries_the_evidence() -> None:
    (only,) = suggest_merges({"d1": ["widget"], "d2": ["widget"], "d3": ["widgets"]})

    assert only.to_json() == {
        "keep": "widget",
        "merge": "widgets",
        "keep_count": 2,
        "merge_count": 1,
        "reasons": ["similar-spelling", "singular-plural"],
        "ratio": round(only.ratio, 3),
        "overlap": 0.0,
        "shared": 0,
    }


def test_no_tags_no_suggestions() -> None:
    assert suggest_merges({}) == []


def _brute_force_co_occurrence(
    doc_tags: dict[str, list[str]],
) -> dict[tuple[str, str], tuple[float, int]]:
    """Reference: every pair of tags, document sets intersected directly (the pre-round-1 rule)."""
    docs_by_tag: dict[str, set[str]] = {}
    for doc_id, tags in doc_tags.items():
        for tag in tags:
            docs_by_tag.setdefault(tag, set()).add(doc_id)
    out: dict[tuple[str, str], tuple[float, int]] = {}
    for a, b in itertools.combinations(sorted(docs_by_tag), 2):
        shared = len(docs_by_tag[a] & docs_by_tag[b])
        overlap = jaccard(docs_by_tag[a], docs_by_tag[b])
        if shared >= MIN_SHARED_DOCS and overlap >= JACCARD_THRESHOLD:
            out[(a, b)] = (overlap, shared)
    return out


@pytest.mark.parametrize("seed", [7, 19, 2026])
def test_co_occurrence_equals_the_brute_force_rule_on_a_random_corpus(seed: int) -> None:
    rng = random.Random(seed)
    noise = [f"n{i:02d}" for i in range(20)]
    # Partner tags that mostly appear beside their lead, at strengths either side of the 0.8
    # cut, so the rule fires for some pairs and not for others; plus independent noise tags.
    strengths = {f"p{i}": rng.choice([0.6, 0.75, 0.85, 0.95, 1.0]) for i in range(8)}
    doc_tags: dict[str, list[str]] = {}
    for d in range(400):
        tags = set(rng.sample(noise, rng.randint(0, 3)))
        for lead, strength in strengths.items():
            if rng.random() < 0.15:
                tags.add(f"{lead}-lead")
                if rng.random() < strength:
                    tags.add(f"{lead}-partner")
            elif rng.random() < 0.02:
                # A partner without its lead puts a document in P (partner) that is not in L
                # (lead), so P is not a subset of L. A strength below 1.0 leaves lead-only
                # documents, so L is not a subset of P. With both, neither set contains the
                # other, the union is larger than either, and |A| + |B| - shared is tested.
                tags.add(f"{lead}-partner")
        doc_tags[f"d{d:03d}"] = sorted(tags)

    expected = _brute_force_co_occurrence(doc_tags)
    got = {
        tuple(sorted((s.keep, s.merge))): (s.overlap, s.shared)
        for s in suggest_merges(doc_tags)
        if "co-occurrence" in s.reasons
    }

    assert expected  # the corpus is built so the rule fires
    docs_by_tag: dict[str, set[str]] = {}
    for doc_id, tags in doc_tags.items():
        for tag in tags:
            docs_by_tag.setdefault(tag, set()).add(doc_id)
    # ... and so at least one partner/lead pair is non-nested: NEITHER set is a subset of the
    # other, which is exactly when |A| + |B| - shared differs from max(|A|, |B|).
    assert any(
        not (partner <= lead_docs or lead_docs <= partner)
        for partner, lead_docs in (
            (docs_by_tag.get(f"{lead}-partner", set()), docs_by_tag.get(f"{lead}-lead", set()))
            for lead in strengths
        )
    )
    assert got.keys() == expected.keys()
    for pair, (overlap, shared) in expected.items():
        assert got[pair][1] == shared
        assert got[pair][0] == pytest.approx(overlap)


@pytest.mark.parametrize(
    ("b_docs", "suggested", "overlap"),
    [
        # A on d1..d5, B on d2..d6: shared 4, union 6, Jaccard 0.667 -> not suggested. (A union
        # taken as max(|A|, |B|) = 5 would give 0.8 and wrongly suggest it.)
        (range(2, 7), False, 4 / 6),
        # A on d1..d9, B on d2..d10: shared 8, union 10, Jaccard 0.8 -> suggested, at the cut.
        (range(2, 11), True, 0.8),
    ],
)
def test_co_occurrence_on_non_nested_document_sets(
    b_docs: range, suggested: bool, overlap: float
) -> None:
    a_docs = range(1, len(b_docs) + 1)
    doc_tags: dict[str, list[str]] = {}
    for i in set(a_docs) | set(b_docs):
        doc_tags[f"d{i:02d}"] = [
            t for t, docs in (("harbor", a_docs), ("lantern", b_docs)) if i in docs
        ]

    got = _pairs(suggest_merges(doc_tags))

    pair = got.get(("harbor", "lantern")) or got.get(("lantern", "harbor"))
    if not suggested:
        assert pair is None
        return
    assert pair is not None
    assert pair.reasons == ("co-occurrence",)
    assert pair.overlap == pytest.approx(overlap)
    assert pair.shared == 8
