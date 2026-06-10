"""Unit + integration tests for the agentic `brain ask` loop (Plan 06)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from brain.ask import (
    AskResult,
    Citation,
    _build_citations,
    _call_plan_step,
    _call_reflect_step,
    _clean_query_list,
    _graph_summary,
    ask,
    ask_no_loop,
)
from brain.config import Config
from brain.search import SearchResult
from tests.conftest import FakeEmbedder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg() -> Config:
    """A minimal Config (DB url unused by the pure-logic unit tests)."""
    return Config(database_url="postgresql://unused/none")


def _doc(doc_id: str, title: str = "Doc", snippet: str = "snippet") -> SearchResult:
    return SearchResult(
        document_id=doc_id,
        title=title,
        source_kind="manual",
        snippet=snippet,
        score=1.0,
        content_type="note",
        tags=[],
    )


class _ScriptedChat:
    """A fake ``ChatJson`` returning queued JSON bodies in call order.

    Records every prompt it received so tests can assert prompt content. Each
    queued item is the dict the real ``chat_json`` would have parsed from the
    model. Standard test double (no monkey-patching of production modules).
    """

    def __init__(self, bodies: list[dict[str, Any]]) -> None:
        self._bodies = list(bodies)
        self.prompts: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    def __call__(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if not self._bodies:
            raise AssertionError("scripted chat exhausted")
        return self._bodies.pop(0)


# ---------------------------------------------------------------------------
# Plan / reflect parsing (pure logic)
# ---------------------------------------------------------------------------


def test_plan_parses_sub_queries() -> None:
    # Arrange
    chat = _ScriptedChat([{"sub_queries": ["interview prep", "negotiation"]}])

    # Act
    sub_queries = _call_plan_step(chat, _cfg(), "What did I learn negotiating?")

    # Assert
    assert sub_queries == ["interview prep", "negotiation"]
    assert "Question: What did I learn negotiating?" in chat.prompts[0]


def test_plan_clamps_to_three_and_dedupes() -> None:
    chat = _ScriptedChat([{"sub_queries": ["a", "a", "b", "c", "d", 5, ""]}])
    sub_queries = _call_plan_step(chat, _cfg(), "q")
    assert sub_queries == ["a", "b", "c"]


def test_reflect_stops_on_sufficient() -> None:
    chat = _ScriptedChat([{"sufficient": True, "follow_up_queries": []}])
    verdict = _call_reflect_step(chat, _cfg(), "q", [_doc("d1")])
    assert verdict.sufficient is True
    assert verdict.follow_up_queries == []


def test_reflect_returns_followups() -> None:
    chat = _ScriptedChat(
        [{"sufficient": False, "follow_up_queries": ["follow up", "extra", "third"]}]
    )
    verdict = _call_reflect_step(chat, _cfg(), "q", [_doc("d1")])
    assert verdict.sufficient is False
    # Clamped to 2 follow-ups.
    assert verdict.follow_up_queries == ["follow up", "extra"]


def test_clean_query_list_rejects_non_list() -> None:
    assert _clean_query_list(None, limit=3) == []
    assert _clean_query_list("not a list", limit=3) == []


# ---------------------------------------------------------------------------
# Citation tracking (pure logic)
# ---------------------------------------------------------------------------


def test_synthesize_citation_tracking() -> None:
    # Arrange
    docs = [_doc("doc-1", "First"), _doc("doc-2", "Second"), _doc("doc-3", "Third")]
    answer = "[1] detail about first. [2] detail about second."

    # Act
    citations = _build_citations(answer, docs)

    # Assert: two citations mapped from the numbered list, in order.
    assert [c.ref for c in citations] == [1, 2]
    assert [c.document_id for c in citations] == ["doc-1", "doc-2"]


def test_citations_ordered_by_first_appearance_not_renumbered() -> None:
    docs = [_doc("doc-1"), _doc("doc-2"), _doc("doc-3")]
    answer = "First point [3], then [1]."
    citations = _build_citations(answer, docs)
    # Order = appearance; refs preserved (3 then 1) so markers stay valid.
    assert [c.ref for c in citations] == [3, 1]
    assert [c.document_id for c in citations] == ["doc-3", "doc-1"]


def test_citations_drop_out_of_range_markers() -> None:
    docs = [_doc("doc-1"), _doc("doc-2")]
    answer = "Real [1] and hallucinated [5] and zero [0]."
    citations = _build_citations(answer, docs)
    assert [c.ref for c in citations] == [1]


def test_citations_dedupe_repeated_markers() -> None:
    docs = [_doc("doc-1"), _doc("doc-2")]
    answer = "[1] then again [1] and [2]."
    citations = _build_citations(answer, docs)
    assert [c.ref for c in citations] == [1, 2]


def test_sanitize_answer_strips_out_of_range_markers() -> None:
    from brain.ask import _sanitize_answer

    # 2 docs available; [99] and [0] are dangling and must be removed from text,
    # [1] kept.
    cleaned = _sanitize_answer("Real claim [1] but fake claim [99] and [0].", 2)
    assert "[99]" not in cleaned
    assert "[0]" not in cleaned
    assert "[1]" in cleaned
    # No double spaces left where markers were removed.
    assert "  " not in cleaned


def test_sanitize_answer_empty_docs_strips_all() -> None:
    from brain.ask import _sanitize_answer

    assert _sanitize_answer("nothing found [1] here", 0) == "nothing found here"


# ---------------------------------------------------------------------------
# Graph summary (pure logic)
# ---------------------------------------------------------------------------


def test_graph_summary_empty_context() -> None:
    from brain.graph_rag.schema import GraphContext

    ctx = GraphContext(session_id="s", mode="hybrid", query="q")
    assert _graph_summary(ctx) == ""


def test_graph_summary_from_entities() -> None:
    from brain.graph_rag.schema import GraphContext, GraphEntity

    ctx = GraphContext(
        session_id="s",
        mode="local",
        query="q",
        entities=[
            GraphEntity(id="1", entity_type="person", name="Alpha", canonical_key="alpha"),
            GraphEntity(id="2", entity_type="person", name="Beta", canonical_key="beta"),
        ],
    )
    assert _graph_summary(ctx) == "Alpha, Beta"


def test_graph_summary_from_themes_and_communities() -> None:
    from brain.graph_rag.schema import (
        CommunityGroup,
        GraphContext,
        GraphEntity,
        ThemeGroup,
    )

    theme = ThemeGroup(
        group_id=1,
        entities=[
            GraphEntity(id="1", entity_type="concept", name="Latency", canonical_key="latency"),
        ],
        summary="latency reduction work",
    )
    community = CommunityGroup(community_key="c1", summary="reliability cluster")
    ctx = GraphContext(
        session_id="s",
        mode="themes",
        query="q",
        themes=[theme],
        communities=[community],
    )
    summary = _graph_summary(ctx)
    assert "latency reduction work" in summary
    assert "reliability cluster" in summary


# ---------------------------------------------------------------------------
# Graph-leg retrieval (mode != hybrid)
# ---------------------------------------------------------------------------


def test_retrieve_graph_requires_backend() -> None:
    from brain.ask import _retrieve

    with pytest.raises(ValueError, match="requires a graph backend"):
        _retrieve(
            None,
            _cfg(),
            embedder=FakeEmbedder(),
            query="q",
            limit=5,
            mode="local",
            backend=None,
        )


def test_retrieve_graph_merges_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain.ask import _retrieve
    from brain.graph_rag.schema import GraphContext

    # Hybrid leg returns doc-1; graph leg returns doc-1 (dup) + doc-2 (new).
    monkeypatch.setattr(
        "brain.ask._retrieve_hybrid",
        lambda conn, cfg, *, embedder, query, limit: [_doc("doc-1")],
    )
    graph_ctx = GraphContext(
        session_id="s",
        mode="local",
        query="q",
        docs=[_doc("doc-1"), _doc("doc-2")],
    )
    monkeypatch.setattr(
        "brain.graph_rag.graph_rag_search",
        lambda *a, **k: graph_ctx,
    )

    docs, summary = _retrieve(
        None,
        _cfg(),
        embedder=FakeEmbedder(),
        query="q",
        limit=5,
        mode="local",
        backend=object(),  # sentinel; graph_rag_search is patched
    )
    assert [d.document_id for d in docs] == ["doc-1", "doc-2"]


def test_ask_no_loop_graph_requires_backend() -> None:
    with pytest.raises(ValueError, match="requires a graph backend"):
        ask_no_loop(
            None,
            _cfg(),
            embedder=FakeEmbedder(),
            chat=_ScriptedChat([]),
            question="q",
            mode="local",
            backend=None,
        )


# ---------------------------------------------------------------------------
# Loop orchestration (mocked retrieval via a stub conn + scripted chat)
# ---------------------------------------------------------------------------


def _patch_retrieve(
    monkeypatch: pytest.MonkeyPatch, batches: list[list[SearchResult]]
) -> list[str]:
    """Replace ``brain.ask._retrieve`` with a stub yielding queued doc batches.

    Returns a list that records every sub-query the loop retrieved for. Each
    call pops the next batch; when exhausted it returns the last batch again so
    a longer-than-scripted loop stays deterministic.

    ``mocker.patch``-equivalent (pytest ``monkeypatch``) — a standard test
    double with automatic cleanup, NOT production monkey-patching.
    """
    seen_queries: list[str] = []
    queue = list(batches)

    def _fake_retrieve(
        conn: Any,
        cfg: Config,
        *,
        embedder: Any,
        query: str,
        limit: int,
        mode: str,
        backend: Any,
    ) -> tuple[list[SearchResult], str]:
        seen_queries.append(query)
        batch = queue.pop(0) if queue else batches[-1]
        return batch, ""

    monkeypatch.setattr("brain.ask._retrieve", _fake_retrieve)
    return seen_queries


def test_no_new_docs_stops_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: plan yields one sub-query; first retrieve finds a doc, the loop
    # would reflect+follow-up, but the follow-up retrieve returns the SAME doc
    # → no new docs → loop stops.
    chat = _ScriptedChat(
        [
            {"sub_queries": ["q1"]},  # plan
            {"sufficient": False, "follow_up_queries": ["q2"]},  # reflect (iter1)
            {"answer": "[1] answer"},  # synthesize
        ]
    )
    _patch_retrieve(
        monkeypatch,
        [[_doc("doc-1")], [_doc("doc-1")]],  # iter1 new, iter2 same → stop
    )

    # Act
    result = ask(
        None,  # conn unused (retrieval patched)
        _cfg(),
        embedder=FakeEmbedder(),
        chat=chat,
        question="q",
        max_iterations=3,
    )

    # Assert: ran two iterations then stopped (no new docs in iter2).
    assert result.iterations_used == 2
    assert result.fallback_used is False
    assert [c.document_id for c in result.citations] == ["doc-1"]


def test_loop_stops_on_sufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = _ScriptedChat(
        [
            {"sub_queries": ["q1"]},
            {"sufficient": True, "follow_up_queries": []},  # reflect says enough
            {"answer": "[1] done"},
        ]
    )
    _patch_retrieve(monkeypatch, [[_doc("doc-1")], [_doc("doc-2")]])
    result = ask(
        None, _cfg(), embedder=FakeEmbedder(), chat=chat, question="q", max_iterations=3
    )
    assert result.iterations_used == 1


def test_loop_respects_max_iterations(monkeypatch: pytest.MonkeyPatch) -> None:
    # reflect always says insufficient + provides a follow-up that finds a NEW
    # doc each round, so only max_iterations caps the loop.
    chat = _ScriptedChat(
        [
            {"sub_queries": ["q1"]},  # plan
            {"sufficient": False, "follow_up_queries": ["q2"]},  # reflect iter1
            {"sufficient": False, "follow_up_queries": ["q3"]},  # reflect iter2
            {"answer": "[1] capped"},  # synthesize (no reflect on last iter)
        ]
    )
    _patch_retrieve(
        monkeypatch,
        [[_doc("doc-1")], [_doc("doc-2")], [_doc("doc-3")]],
    )
    result = ask(
        None, _cfg(), embedder=FakeEmbedder(), chat=chat, question="q", max_iterations=3
    )
    assert result.iterations_used == 3
    assert result.sub_queries == ["q1", "q2", "q3"]


def test_plan_fallback_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = _ScriptedChat(
        [
            {"sub_queries": []},  # plan yields nothing → fall back to question
            {"answer": "[1] fallback answer"},  # synthesize (single iter)
        ]
    )
    seen = _patch_retrieve(monkeypatch, [[_doc("doc-1")]])
    result = ask(
        None,
        _cfg(),
        embedder=FakeEmbedder(),
        chat=chat,
        question="the question",
        max_iterations=1,
    )
    assert result.fallback_used is True
    assert result.sub_queries == ["the question"]
    assert seen == ["the question"]


def test_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        ask(
            None,
            _cfg(),
            embedder=FakeEmbedder(),
            chat=_ScriptedChat([]),
            question="q",
            mode="bogus",
        )


def test_ask_rejects_non_positive_max_iterations() -> None:
    with pytest.raises(ValueError, match="max_iterations must be >= 1"):
        ask(
            None,
            _cfg(),
            embedder=FakeEmbedder(),
            chat=_ScriptedChat([]),
            question="q",
            max_iterations=0,
        )


def test_ask_loop_sanitizes_out_of_range_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The synthesize step emits a dangling [5] (only 1 doc retrieved). The final
    # answer must not contain [5], and citations must not include it.
    chat = _ScriptedChat(
        [
            {"sub_queries": ["q1"]},
            {"answer": "Real [1] but fabricated [5]."},
        ]
    )
    _patch_retrieve(monkeypatch, [[_doc("doc-1")]])
    result = ask(
        None, _cfg(), embedder=FakeEmbedder(), chat=chat, question="q", max_iterations=1
    )
    assert "[5]" not in result.answer
    assert "[1]" in result.answer
    assert [c.ref for c in result.citations] == [1]


def test_to_dict_round_trip() -> None:
    result = AskResult(
        answer="[1] a",
        citations=[Citation(1, "doc-1", "T", "manual", "snip")],
        iterations_used=2,
        sub_queries=["a", "b"],
        fallback_used=False,
        session_id="abc",
    )
    payload = result.to_dict()
    assert payload["answer"] == "[1] a"
    assert payload["citations"][0]["document_id"] == "doc-1"
    assert payload["iterations_used"] == 2
    assert payload["sub_queries"] == ["a", "b"]
    assert payload["fallback_used"] is False
    assert payload["session_id"] == "abc"


# ---------------------------------------------------------------------------
# Integration (real Postgres test DB, fake embedder fixture)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ask_end_to_end_no_loop(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    # Arrange: ingest synthetic docs about a distinctive topic.
    doc_id = seed_doc(
        title="Synthetic onboarding playbook",
        content=(
            "The synthetic onboarding playbook describes how the platform team "
            "ramps new engineers using paired mentorship and a staged checklist."
        ),
    )
    seed_doc(title="Unrelated note", content="Grocery list and weekend plans.")

    fake_embedder = FakeEmbedder()

    def _chat(
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # Cite the first source so a citation maps to a real ingested doc id.
        return {"answer": "The playbook uses paired mentorship [1]."}

    cfg = Config(database_url="postgresql://unused/none")

    # Act
    result = ask_no_loop(
        test_db,
        cfg,
        embedder=fake_embedder,
        chat=_chat,
        question="synthetic onboarding playbook",
        limit=5,
    )

    # Assert
    assert result.answer
    assert result.fallback_used is True
    assert result.iterations_used == 1
    assert len(result.citations) >= 1
    # The cited doc id must be a real ingested document id.
    cited_ids = {c.document_id for c in result.citations}
    assert doc_id in cited_ids


@pytest.mark.integration
def test_ask_loop_end_to_end(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    doc_id = seed_doc(
        title="Synthetic pipeline decision",
        content=(
            "We decided the data pipeline would use streaming ingestion with a "
            "batch fallback for nightly reconciliation."
        ),
    )

    chat = _ScriptedChat(
        [
            {"sub_queries": ["synthetic pipeline"]},
            {"answer": "The pipeline uses streaming ingestion [1]."},
        ]
    )
    cfg = Config(database_url="postgresql://unused/none")

    result = ask(
        test_db,
        cfg,
        embedder=FakeEmbedder(),
        chat=chat,
        question="what did we decide about the data pipeline?",
        max_iterations=1,
    )
    assert result.iterations_used == 1
    assert doc_id in {c.document_id for c in result.citations}
