"""Pure tests for :mod:`brain.tag_mapping`: mappings and merge suggestions (RM-21).

No database. The per-document loop and the saved-search rewrite are exercised through the CLI in
``tests/test_cli_tags.py`` and ``tests/test_normalize_tags_golden.py``. All tags are synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
