"""Opt-in query rewriting: fusion, paraphrase hygiene, the budget and the pipeline leg (RM-54).

Pure, plus :func:`~brain.search_pipeline.run_search` over a recording ``search_fn``. The real
Postgres surfaces are in ``tests/test_search_rewrite_surfaces.py``. All data is synthetic.
"""
from __future__ import annotations

import contextlib
import io
import logging
from typing import Any

import pytest
import tiktoken
from rich.console import Console

from brain.errors import OllamaUnavailable
from brain.format_search import found_by_table
from brain.ollama_budget import (
    CHAT_TEMPLATE_MARGIN_TOKENS,
    DEFAULT_OLLAMA_NUM_CTX,
    PROMPT_FRAME_RESERVE_TOKENS,
    REWRITE_MAX_INPUT_TOKENS,
    capped_call_budgets,
    num_ctx_violations,
)
from brain.query_rewrite import (
    MAX_PARAPHRASE_CHARS,
    MAX_PARAPHRASES,
    NO_USABLE_PARAPHRASES,
    FusedDoc,
    QueryHit,
    QueryRanking,
    build_prompt,
    clean_paraphrases,
    fuse_rankings,
    rewrite_notice,
)
from brain.search import SearchDiagnostics, SearchResult
from brain.search_pipeline import StageReport, run_search

pytestmark = pytest.mark.nodb

ENCODING = tiktoken.get_encoding("cl100k_base")


def _tokens(text: str) -> int:
    return len(ENCODING.encode(text, disallowed_special=()))


def _row(document_id: str) -> SearchResult:
    return SearchResult(
        document_id=document_id,
        title=document_id,
        source_kind="manual",
        snippet="",
        score=0.0,
        content_type="note",
        tags=[],
    )


class _Conn:
    """The one thing the leg asks of a connection when ``search_fn`` is a fake."""

    def transaction(self) -> contextlib.nullcontext[None]:
        return contextlib.nullcontext()


class _Search:
    """A ``search_fn`` returning fixed rankings per query, recording each call's kwargs."""

    def __init__(self, rankings: dict[str, list[str]], *, fail: tuple[str, ...] = ()) -> None:
        self.rankings = rankings
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def __call__(self, _conn: Any, **kwargs: Any) -> list[SearchResult]:
        self.calls.append(kwargs)
        if kwargs["query"] in self.fail:
            raise RuntimeError("synthetic failure with paraphrase text in it")
        return [_row(d) for d in self.rankings.get(kwargs["query"], [])[: kwargs["limit"]]]


def _run(search: _Search, rewriter: Any, *, limit: int = 5, **kwargs: Any) -> Any:
    return run_search(
        search,
        _Conn(),  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        query="original",
        limit=limit,
        stages=(),
        rewriter=rewriter,
        **kwargs,
    )


# --- fusion (AC2) ------------------------------------------------------------------------------


def test_fusion_scores_are_the_specified_weights_over_rrf() -> None:
    # Act
    fused = fuse_rankings(
        [
            QueryRanking("q", True, ("a", "b")),
            QueryRanking("p1", False, ("b", "c")),
            QueryRanking("p2", False, ("c",)),
        ]
    )

    # Assert: hand-computed, 1/(60 + rank + 1) with 0-indexed ranks.
    assert fused["a"].fused_score == pytest.approx(1.0 / 61)
    assert fused["b"].fused_score == pytest.approx(1.0 / 62 + 0.5 / 61)
    assert fused["c"].fused_score == pytest.approx(0.5 / 62 + 0.5 / 61)
    assert list(fused) == ["b", "a", "c"]


def test_the_originals_top_hit_outranks_a_paraphrases_next_hits() -> None:
    # Exact-name protection: rank 1 in both paraphrases does not beat the original's rank 0 alone.
    fused = fuse_rankings(
        [
            QueryRanking("q", True, ("exact",)),
            QueryRanking("p1", False, ("x", "other")),
            QueryRanking("p2", False, ("y", "other")),
        ]
    )

    assert list(fused)[0] == "exact"


def test_an_exact_tie_keeps_the_originals_order() -> None:
    # Two paraphrases agreeing on rank 0 equal the original's rank 0: 0.5/61 + 0.5/61 = 1/61.
    fused = fuse_rankings(
        [
            QueryRanking("q", True, ("first",)),
            QueryRanking("p1", False, ("second",)),
            QueryRanking("p2", False, ("second",)),
        ]
    )

    assert fused["first"].fused_score == fused["second"].fused_score
    assert list(fused) == ["first", "second"]


def test_a_paraphrase_only_document_enters_and_names_its_queries() -> None:
    fused = fuse_rankings(
        [QueryRanking("q", True, ("a",)), QueryRanking("p", False, ("a", "only-paraphrase"))]
    )

    hits = fused["only-paraphrase"].hits
    assert [(h.query, h.is_original, h.rank) for h in hits] == [("p", False, 2)]
    assert [(h.query, h.is_original, h.rank) for h in fused["a"].hits] == [
        ("q", True, 1),
        ("p", False, 1),
    ]


def test_fusing_nothing_is_empty() -> None:
    assert fuse_rankings([]) == {}
    assert fuse_rankings([QueryRanking("q", True, ())]) == {}


# --- paraphrase hygiene ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain \x1b[31mred\x1b[0m words", "plain red words"),
        ("osc \x1b]8;;http://bad.example\x07link\x1b]8;;\x07 text", "osc link text"),
        ("nul\x00byte", "nulbyte"),
        ("c1\x9b31m tail", "c131m tail"),
        ("bidi\u202eoverride", "bidioverride"),
        ("tag\U000e0041char", "tagchar"),
        ("lone\ud800surrogate", "lonesurrogate"),
        ("line\nbreak\u2028here\ttab", "line break here tab"),
    ],
)
def test_control_content_never_survives(raw: str, expected: str) -> None:
    assert clean_paraphrases([raw], original="q") == (expected,)


@pytest.mark.parametrize("ordinary", ["café résumé", "会议记录", "[draft] notes (v2)"])
def test_ordinary_text_passes_unchanged(ordinary: str) -> None:
    assert clean_paraphrases([ordinary], original="q") == (ordinary,)


@pytest.mark.parametrize(
    "raw", [None, "a string", {"paraphrases": ["x"]}, 7, True, [], [None, 1, ["nested"], {"a": 1}]]
)
def test_a_bad_shape_yields_nothing(raw: object) -> None:
    assert clean_paraphrases(raw, original="q") == ()


def test_only_the_cap_is_kept_and_order_holds() -> None:
    assert MAX_PARAPHRASES == 2
    assert clean_paraphrases(["a", "b", "c", "d"], original="q") == ("a", "b")


def test_an_over_long_item_is_dropped_not_cut() -> None:
    longest = "x" * MAX_PARAPHRASE_CHARS
    too_long = "y" * (MAX_PARAPHRASE_CHARS + 1)

    assert clean_paraphrases([too_long, longest], original="q") == (longest,)


def test_echoes_and_repeats_do_not_use_up_the_cap() -> None:
    # Arrange: an echo differing only by case and spacing, then a repeat of a real one.
    raw = ["Vague  QUERY", "real one", "REAL ONE", "real two"]

    # Act / Assert
    assert clean_paraphrases(raw, original="vague query") == ("real one", "real two")


def test_a_one_word_difference_is_a_real_paraphrase() -> None:
    assert clean_paraphrases(["vague queries"], original="vague query") == ("vague queries",)


def test_an_item_that_cleans_to_nothing_is_dropped() -> None:
    assert clean_paraphrases(["\x1b[0m", "   ", "\u202e"], original="q") == ()


# --- rendering ----------------------------------------------------------------------------------


def _rendered(paraphrase: str) -> str:
    hit = QueryHit(query=paraphrase, is_original=False, rank=3)
    table = found_by_table(
        [_row("doc-a")], {"doc-a": FusedDoc(fused_score=0.5, hits=(hit,))}
    )
    console = Console(record=True, width=200, file=io.StringIO())
    console.print(table)
    return console.export_text()


@pytest.mark.parametrize(
    "markup", ["[/]", "[red]tinted[/red]", "[link=http://bad.example]x", "[bold]"]
)
def test_rich_markup_in_a_paraphrase_prints_literally(markup: str) -> None:
    assert markup in _rendered(f"words {markup}")


def test_an_ordinary_paraphrase_renders_with_its_rank() -> None:
    assert '"plain words" #3' in _rendered("plain words")


def test_the_users_own_query_is_named_not_echoed() -> None:
    # Arrange
    hit = QueryHit(query="the users words", is_original=True, rank=1)
    table = found_by_table([_row("doc-a")], {"doc-a": FusedDoc(fused_score=0.5, hits=(hit,))})
    console = Console(record=True, width=200, file=io.StringIO())

    # Act
    console.print(table)

    # Assert
    text = console.export_text()
    assert "original #1" in text
    assert "the users words" not in text


# --- the budget (AC5) and the prompt -----------------------------------------------------------


def test_the_budget_is_listed_and_fits_the_default_num_ctx() -> None:
    budgets = capped_call_budgets(enrich_max_input_tokens=1, audio_max_input_tokens=1)
    rewrite = next(b for b in budgets if b.caller == "query rewrite")

    assert rewrite.input_tokens == REWRITE_MAX_INPUT_TOKENS == 256
    assert rewrite.required_num_ctx == 1792
    assert num_ctx_violations(DEFAULT_OLLAMA_NUM_CTX, [rewrite]) == []
    assert "query rewrite" in num_ctx_violations(1791, [rewrite])[0]


def test_the_prompts_frame_fits_the_reserve() -> None:
    frame = _tokens(build_prompt("", cost=_tokens))

    assert frame <= PROMPT_FRAME_RESERVE_TOKENS - CHAT_TEMPLATE_MARGIN_TOKENS


def test_a_query_over_the_cap_is_cut_in_the_prompt_only() -> None:
    # Arrange
    query = "word " * 2000

    # Act
    prompt = build_prompt(query, cost=_tokens)

    # Assert: the frame plus at most the capped query.
    frame = _tokens(build_prompt("", cost=_tokens))
    assert _tokens(prompt) <= frame + REWRITE_MAX_INPUT_TOKENS + 1
    assert query not in prompt


def test_a_short_query_is_sent_verbatim() -> None:
    assert "a short query" in build_prompt("a short query", cost=_tokens)


def test_a_query_with_braces_does_not_break_the_prompt() -> None:
    assert "{count} {0} {" in build_prompt("{count} {0} {", cost=_tokens)


# --- the pipeline leg --------------------------------------------------------------------------


def test_without_a_rewriter_only_the_original_runs_at_the_plain_limit() -> None:
    # Arrange
    search = _Search({"original": ["a", "b", "c"]})

    # Act
    outcome = _run(search, None, limit=2)

    # Assert
    assert [c["query"] for c in search.calls] == ["original"]
    assert search.calls[0]["limit"] == 2
    assert outcome.fused is None
    assert outcome.stages == ()


def test_the_leg_fuses_and_reports_itself_first() -> None:
    # Arrange
    search = _Search({"original": ["a"], "p1": ["b"], "p2": ["b"]})

    # Act
    outcome = _run(search, lambda _q: ["p1", "p2"])

    # Assert
    assert [c["query"] for c in search.calls] == ["original", "p1", "p2"]
    assert [r.document_id for r in outcome.results] == ["a", "b"]
    assert outcome.stages[0].name == "rewrite"
    assert outcome.stages[0].applied is True
    assert outcome.stages[0].skipped_reason is None
    assert set(outcome.fused) == {"a", "b"}


def test_each_query_fetches_twice_the_limit_and_the_output_is_cut_to_it() -> None:
    # Arrange: "deep" sits just past the limit in the original, first in both paraphrases.
    search = _Search({"original": ["a", "deep"], "p1": ["deep"], "p2": ["deep"]})

    # Act
    outcome = _run(search, lambda _q: ["p1", "p2"], limit=1)

    # Assert
    assert {c["limit"] for c in search.calls} == {2}
    assert [r.document_id for r in outcome.results] == ["deep"]


def test_only_the_original_gets_the_diagnostics_and_every_filter_reaches_all() -> None:
    # Arrange
    search = _Search({"original": ["a"], "p1": ["b"]})
    diagnostics = SearchDiagnostics()

    # Act
    _run(
        search,
        lambda _q: ["p1"],
        diagnostics=diagnostics,
        total_count=True,
        source_kind="manual",
        sensitivity="normal",
        without_tag="t",
    )

    # Assert
    original, paraphrase = search.calls
    assert original["diagnostics"] is diagnostics
    assert original["total_count"] is True
    assert "diagnostics" not in paraphrase
    assert "total_count" not in paraphrase
    for key, value in (("source_kind", "manual"), ("sensitivity", "normal"), ("without_tag", "t")):
        assert original[key] == value
        assert paraphrase[key] == value


def test_the_rewriter_output_is_cleaned_inside_the_leg() -> None:
    # Arrange: a rewriter handing back raw model output cannot skip the cleaning.
    search = _Search({"original": []})

    # Act
    outcome = _run(search, lambda _q: ["p\x1b[31m1\ud800", "p2", "p3", "p4"])

    # Assert
    assert [c["query"] for c in search.calls] == ["original", "p1", "p2"]
    assert outcome.stages[0].applied is True


@pytest.mark.parametrize("raw", [[], None, ["ORIGINAL"], [7]])
def test_nothing_usable_is_the_original_with_a_notice(raw: object) -> None:
    # Arrange
    search = _Search({"original": ["a", "b"]})

    # Act
    outcome = _run(search, lambda _q: raw)

    # Assert
    assert [c["query"] for c in search.calls] == ["original"]
    assert [r.document_id for r in outcome.results] == ["a", "b"]
    assert outcome.stages[0].applied is False
    assert outcome.stages[0].skipped_reason == NO_USABLE_PARAPHRASES
    assert rewrite_notice(outcome.stages) is not None


def test_a_rewriter_that_raises_costs_only_itself_and_stays_class_only() -> None:
    # Arrange
    def down(_query: str) -> object:
        raise OllamaUnavailable("reply text and the query original")

    search = _Search({"original": ["a"]})

    # Act
    outcome = _run(search, down)

    # Assert
    report = outcome.stages[0]
    assert [r.document_id for r in outcome.results] == ["a"]
    assert report.applied is False
    assert report.skipped_reason == "OllamaUnavailable"
    assert "reply text" not in rewrite_notice(outcome.stages)
    assert [(h.query, h.is_original, h.rank) for h in outcome.fused["a"].hits] == [
        ("original", True, 1)
    ]


def test_a_failed_paraphrase_drops_alone_and_says_so() -> None:
    # Arrange
    search = _Search({"original": ["a"], "good": ["b"]}, fail=("bad",))

    # Act
    outcome = _run(search, lambda _q: ["bad", "good"])

    # Assert
    report = outcome.stages[0]
    assert [r.document_id for r in outcome.results] == ["a", "b"]
    assert report.applied is True
    assert report.skipped_reason == "RuntimeError"
    notice = rewrite_notice(outcome.stages)
    assert notice is not None and "RuntimeError" in notice
    assert "synthetic failure" not in notice


def test_a_failed_paraphrase_logs_its_failure_class_not_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    search = _Search({"original": ["a"]}, fail=("privateparaphrase",))

    # Act
    with caplog.at_level(logging.DEBUG, logger="brain"):
        _run(search, lambda _q: ["privateparaphrase"])

    # Assert
    assert "RuntimeError" in caplog.text
    assert "privateparaphrase" not in caplog.text
    assert "synthetic failure" not in caplog.text


def test_every_paraphrase_failing_is_not_applied() -> None:
    # Arrange
    search = _Search({"original": ["a"]}, fail=("bad",))

    # Act
    outcome = _run(search, lambda _q: ["bad"])

    # Assert
    assert outcome.stages[0].applied is False
    assert outcome.stages[0].skipped_reason == "RuntimeError"
    assert [r.document_id for r in outcome.results] == ["a"]


def test_a_healthy_run_has_no_notice() -> None:
    report = StageReport(name="rewrite", applied=True, skipped_reason=None, ms=1.0)
    assert rewrite_notice([report]) is None
    assert rewrite_notice([]) is None


def test_the_originals_row_wins_for_a_shared_document() -> None:
    # Arrange
    def search(_conn: Any, **kwargs: Any) -> list[SearchResult]:
        return [
            SearchResult(
                document_id="shared",
                title="t",
                source_kind="manual",
                snippet=f"snippet for {kwargs['query']}",
                score=0.1,
                content_type="note",
                tags=[],
            )
        ]

    # Act
    outcome = _run(search, lambda _q: ["p1"])  # type: ignore[arg-type]

    # Assert
    assert outcome.results[0].snippet == "snippet for original"


def test_stages_see_the_fused_rows_and_the_original_query() -> None:
    # Arrange
    seen: list[tuple[str, list[str]]] = []

    class _Stage:
        name = "probe"

        def candidate_limit(self, limit: int) -> int:
            return limit

        def signature(self) -> dict[str, Any]:
            return {}

        def apply(self, _conn: Any, *, query: str, session_id: Any, results: Any) -> Any:
            from brain.search_pipeline import StageOutcome

            seen.append((query, [r.document_id for r in results]))
            return StageOutcome(
                results=results,
                report=StageReport(name="probe", applied=True, skipped_reason=None, ms=0.0),
            )

    search = _Search({"original": ["a"], "p1": ["b"]})

    # Act
    outcome = run_search(
        search,
        _Conn(),  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        query="original",
        limit=5,
        stages=(_Stage(),),
        rewriter=lambda _q: ["p1"],
    )

    # Assert
    assert seen == [("original", ["a", "b"])]
    assert [r.name for r in outcome.stages] == ["rewrite", "probe"]
