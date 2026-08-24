"""``brain.format_search.search_results_brief_json`` — the brief projection.

Pure logic: no DB, no embedder, no CLI. Every test builds :class:`SearchResult`
values directly and injects its own ``cost`` callable, which is exactly why the
function takes one.

Three properties are load-bearing and each has its own test:

- **The choice is per-result, never global.** A summary that is cheaper for one
  hit can be more expensive for the next (on the live corpus one stitched
  snippet measured 899 chars against a 375-char summary; another ran the other
  way), so a single global decision would inflate half the payload.
- **A tie keeps the chunk snippet** — strict ``<``. The query-conditioned
  artifact wins when both cost the same.
- **A missing summary always falls back** — ``None``, empty, or whitespace-only
  — so brief mode can never return less than the default projection.

All fixture data is synthetic.
"""
from __future__ import annotations

from unittest import mock

from brain.format_search import search_results_brief_json, search_results_json
from brain.search import SearchResult

#: Deliberately different lengths so ``len``-based cost comparisons are obvious.
LONG_SNIPPET = "x" * 900
SHORT_SUMMARY = "s" * 375


def _result(
    doc_id: str = "doc-1",
    *,
    snippet: str = "the matching passage",
    summary: str | None = None,
) -> SearchResult:
    """A minimal hit carrying just the two artifacts under test."""
    return SearchResult(
        document_id=doc_id,
        title=f"title-{doc_id}",
        source_kind="manual",
        snippet=snippet,
        score=1.0,
        content_type="note",
        tags=["planning"],
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_brief_substitutes_summary_when_smaller() -> None:
    """The whole point: a cheaper summary replaces the snippet."""
    # Arrange
    results = [_result(snippet=LONG_SNIPPET, summary=SHORT_SUMMARY)]

    # Act
    brief = search_results_brief_json(results)

    # Assert
    assert brief[0]["snippet"] == SHORT_SUMMARY
    assert brief[0]["snippet_source"] == "summary"


def test_brief_keeps_snippet_when_summary_is_larger() -> None:
    """The inflation guard, positive case — substituting would COST tokens.

    Measured per result: this is the direction that makes a global
    summary-always rule wrong.
    """
    # Arrange
    results = [_result(snippet="short passage", summary="v" * 2000)]

    # Act
    brief = search_results_brief_json(results)

    # Assert
    assert brief[0]["snippet"] == "short passage"
    assert brief[0]["snippet_source"] == "chunk"


def test_brief_chooses_per_result_not_globally() -> None:
    """Two hits in one call resolve independently, in opposite directions."""
    # Arrange
    results = [
        _result("doc-cheap-summary", snippet=LONG_SNIPPET, summary=SHORT_SUMMARY),
        _result("doc-cheap-snippet", snippet="tiny", summary="w" * 500),
    ]

    # Act
    brief = search_results_brief_json(results)

    # Assert
    assert [e["snippet_source"] for e in brief] == ["summary", "chunk"]


def test_brief_keeps_snippet_on_exact_tie() -> None:
    """Pins the strict ``<``: equal cost keeps the query-conditioned artifact."""
    # Arrange — same length, so ``len`` reports an exact tie.
    results = [_result(snippet="a" * 100, summary="b" * 100)]

    # Act
    brief = search_results_brief_json(results)

    # Assert
    assert brief[0]["snippet"] == "a" * 100
    assert brief[0]["snippet_source"] == "chunk"


def test_brief_falls_back_to_snippet_when_summary_is_null() -> None:
    """The 7.4% tail with no ingest-time summary still gets a snippet."""
    # Arrange
    results = [_result(snippet=LONG_SNIPPET, summary=None)]

    # Act
    brief = search_results_brief_json(results)

    # Assert
    assert brief[0]["snippet"] == LONG_SNIPPET
    assert brief[0]["snippet_source"] == "chunk"


def test_brief_falls_back_to_snippet_when_summary_is_empty_string() -> None:
    """A blank summary must NOT win on cost and blank the snippet.

    An ``is not None`` guard would let ``cost("") == 0`` beat any snippet,
    returning ``snippet == ""`` labelled ``"summary"`` — strictly LESS than the
    default projection, which the docstring promises can never happen.
    ``documents.summary`` has no CHECK constraint (migration 011), so only the
    enricher currently keeps blanks out of the column.
    """
    # Arrange
    results = [_result(snippet=LONG_SNIPPET, summary="")]

    # Act
    brief = search_results_brief_json(results)

    # Assert
    assert brief[0]["snippet"] == LONG_SNIPPET
    assert brief[0]["snippet_source"] == "chunk"


def test_brief_falls_back_to_snippet_when_summary_is_whitespace_only() -> None:
    """Whitespace-only is blank too — and it is CHEAPER than a real snippet."""
    # Arrange — 3 chars of whitespace beats a 900-char snippet on ``len``.
    results = [_result(snippet=LONG_SNIPPET, summary="  \n\t  ")]

    # Act
    brief = search_results_brief_json(results)

    # Assert
    assert brief[0]["snippet"] == LONG_SNIPPET
    assert brief[0]["snippet_source"] == "chunk"


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_brief_shape_is_seven_keys_plus_snippet_source() -> None:
    """Eight keys total, with ``snippet_source`` last and the seven in order."""
    # Arrange
    results = [_result(snippet=LONG_SNIPPET, summary=SHORT_SUMMARY)]

    # Act
    brief = search_results_brief_json(results)

    # Assert
    assert list(brief[0]) == [
        "id",
        "title",
        "source_kind",
        "snippet",
        "score",
        "content_type",
        "tags",
        "snippet_source",
    ]


def test_brief_reuses_search_results_json_for_the_frozen_keys() -> None:
    """ANTI-DRIFT: brief must *call* :func:`search_results_json`, not re-derive it.

    Two complementary assertions, because either one alone is weak:

    - **The call.** Comparing outputs cannot distinguish "delegates" from
      "inlines a faithful copy of the seven-key literal" — the copy produces
      byte-identical values and would sail through a values-only test, which
      is exactly the second construction site the design forbids. The spy is
      the only assertion that can see that difference, so it is made
      explicitly.
    - **The values.** Delegating and then overwriting a frozen key would
      satisfy the spy while still drifting, so the untouched keys are still
      compared field by field against the default projection.

    ``mock.patch`` is a test double with automatic cleanup, not monkey-patching
    (CLAUDE.md rule 13); ``wraps=`` keeps the real function running so the
    value assertions below exercise production behaviour, not a stub's.
    """
    # Arrange
    results = [
        _result("doc-a", snippet=LONG_SNIPPET, summary=SHORT_SUMMARY),
        _result("doc-b", snippet="tiny", summary=None),
    ]

    # Act
    default = search_results_json(results)
    with mock.patch(
        "brain.format_search.search_results_json", wraps=search_results_json
    ) as spy:
        brief = search_results_brief_json(results)

    # Assert — the call: exactly one delegation to the single construction site.
    spy.assert_called_once_with(results)

    # Assert — the values.
    assert len(brief) == len(default)
    for brief_entry, default_entry in zip(brief, default, strict=True):
        if brief_entry["snippet_source"] == "chunk":
            # ``doc-b`` has no summary, so brief's snippet MUST be the default
            # projection's own value — asserted against that projection, never
            # against a string literal, which is what makes this a drift guard.
            assert brief_entry["snippet"] == default_entry["snippet"]
            stripped = {k: v for k, v in brief_entry.items() if k != "snippet_source"}
            expected = dict(default_entry)
        else:
            stripped = {
                k: v
                for k, v in brief_entry.items()
                if k not in {"snippet", "snippet_source"}
            }
            expected = {k: v for k, v in default_entry.items() if k != "snippet"}
        assert stripped == expected


def test_brief_returns_empty_list_for_no_results() -> None:
    """Zero hits stay zero hits: the empty list survives the projection.

    Guards the degenerate input, not the ``strict=True`` zip — zipping two
    empty sequences cannot raise. What this pins is that the function returns
    ``[]`` rather than ``None`` or a one-entry artifact of the loop.
    """
    assert search_results_brief_json([]) == []


# ---------------------------------------------------------------------------
# The injected cost callable
# ---------------------------------------------------------------------------


def test_brief_cost_callable_is_honoured() -> None:
    """MUTATION TEST: invert the cost ordering and the selection must flip.

    With ``len`` the short summary wins. With a cost that ranks longer strings
    as cheaper, the long snippet must win instead — proving the comparison
    actually consults the injected callable rather than hardcoding ``len``.
    """
    # Arrange
    results = [_result(snippet=LONG_SNIPPET, summary=SHORT_SUMMARY)]

    # Act
    with_len = search_results_brief_json(results)
    inverted = search_results_brief_json(results, cost=lambda s: -len(s))

    # Assert
    assert with_len[0]["snippet_source"] == "summary"
    assert inverted[0]["snippet_source"] == "chunk"
    assert inverted[0]["snippet"] == LONG_SNIPPET


def test_brief_cost_callable_sees_both_artifacts() -> None:
    """The callable is applied to the summary AND the snippet, not just one."""
    # Arrange
    seen: list[str] = []
    results = [_result(snippet="abc", summary="de")]

    def recording_cost(text: str) -> int:
        seen.append(text)
        return len(text)

    # Act
    search_results_brief_json(results, cost=recording_cost)

    # Assert
    assert sorted(seen) == ["abc", "de"]
