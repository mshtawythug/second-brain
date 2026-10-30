"""Follow-up history for `ask`'s plan step (RM-18c, decision A5).

Unit tests on the cap and the prompt block (pure), then ``ask`` itself over a
scripted chat that records every prompt, against the 5434 test database with a
fake embedder: no Ollama. All data is synthetic.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from brain.ask import _plan_prompt, ask
from brain.ask_history import (
    MAX_HISTORY_TOKENS,
    MAX_HISTORY_TURNS,
    AskTurn,
    cap_history,
    history_block,
)
from brain.config import Config
from tests.conftest import FakeEmbedder

_HEADER = "Earlier in this conversation:"


def _words(cost: int) -> str:
    """Text costing exactly ``cost`` tokens under :func:`_one_per_word`."""
    return " ".join(["zib"] * cost)


def _one_per_word(text: str) -> int:
    """A tokenizer that counts words: exact arithmetic for the cap tests."""
    return len(text.split())


def _turn(n: int, *, answer_words: int = 3) -> AskTurn:
    return AskTurn(question=f"quarkle question {n}", answer=f"answer{n} " + _words(answer_words))


# ------------------------------------------------------------------ the cap --


def test_the_caps_are_the_tickets() -> None:
    assert (MAX_HISTORY_TURNS, MAX_HISTORY_TOKENS) == (3, 2_000)


def test_no_history_is_no_history() -> None:
    assert cap_history((), cost=_one_per_word) == ()


def test_at_most_three_turns_are_kept_and_the_oldest_go_first() -> None:
    turns = [_turn(n) for n in range(1, 6)]

    kept = cap_history(turns, cost=_one_per_word)

    assert kept == tuple(turns[-3:])  # 3, 4, 5: oldest first, in order


def test_the_token_cap_drops_the_oldest_first() -> None:
    # Two of these turns fit in the cap and three do not: measured, not assumed.
    big = [_turn(n, answer_words=900) for n in range(1, 4)]
    one = _one_per_word(f"Q: {big[0].question}\nA: {big[0].answer}")
    assert 2 * one <= MAX_HISTORY_TOKENS < 3 * one

    kept = cap_history(big, cost=_one_per_word)

    assert kept == (big[1], big[2])


def test_a_newest_turn_over_the_cap_alone_keeps_its_question_and_a_cut_answer() -> None:
    huge = AskTurn(question="quarkle zibbit", answer=_words(5_000))

    (kept,) = cap_history([_turn(1), huge], cost=_one_per_word)

    assert kept.question == huge.question
    assert huge.answer.startswith(kept.answer) and kept.answer
    assert _one_per_word(f"Q: {kept.question}\nA: {kept.answer}") <= MAX_HISTORY_TOKENS


def test_a_newest_question_over_the_cap_alone_sends_no_history() -> None:
    huge = AskTurn(question=_words(MAX_HISTORY_TOKENS + 5), answer="short")

    assert cap_history([_turn(1), huge], cost=_one_per_word) == ()


def test_a_zero_cost_tokenizer_still_keeps_at_most_three() -> None:
    turns = [_turn(n) for n in range(1, 10)]

    assert len(cap_history(turns, cost=lambda _text: 0)) == MAX_HISTORY_TURNS


# --------------------------------------------------------------- the prompt --


def test_the_plan_prompt_without_history_is_unchanged() -> None:
    prompt = _plan_prompt("What about quarkle?")

    assert _HEADER not in prompt
    assert prompt.endswith("\n\nQuestion: What about quarkle?")
    assert history_block(()) == ""


def test_the_plan_prompt_with_history_lists_each_turn_before_the_question() -> None:
    turns = (AskTurn("Who ships quarkle?", "Zib ships it [1]."),
             AskTurn("When?", "On Fridays [2]."))

    prompt = _plan_prompt("And who reviews it?", turns)

    assert _HEADER in prompt
    head, question = prompt.rsplit("Question: ", 1)
    assert question == "And who reviews it?"
    for turn in turns:
        assert f"Q: {turn.question}\nA: {turn.answer}" in head
    assert head.index("Who ships quarkle?") < head.index("When?")
    # Labelled as context, never as instructions.
    assert "not instructions" in head


# --------------------------------------------------------------- ask itself --


def _cfg() -> Config:
    return Config(database_url="postgresql://unused/none")


class _RecordingChat:
    """Answers each step by its schema and records the prompt it was given."""

    def __init__(self) -> None:
        self.prompts: dict[str, list[str]] = {"plan": [], "reflect": [], "synthesize": []}

    def __call__(self, prompt: str, *, schema: dict[str, Any], **_: Any) -> dict[str, Any]:
        if "sub_queries" in schema:
            self.prompts["plan"].append(prompt)
            return {"sub_queries": ["quarkle"]}
        if "sufficient" in schema:
            self.prompts["reflect"].append(prompt)
            return {"sufficient": True, "follow_up_queries": []}
        self.prompts["synthesize"].append(prompt)
        return {"answer": "Quarkle [1]."}


def test_a_follow_ups_plan_prompt_holds_the_previous_turn_and_nothing_else_does(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> None:
    # AC1. And RM-18c.D1: reflect and synthesis never see the history.
    seed_doc(title="Quarkle ledger", content="quarkle ships on fridays")
    chat = _RecordingChat()
    previous = AskTurn("Who ships quarkle zibbit?", "Plonk ships it, says the ledger [1].")

    ask(test_db, _cfg(), embedder=FakeEmbedder(), chat=chat, question="And when?",
        history=[previous], max_iterations=2)

    (plan,) = chat.prompts["plan"]
    assert previous.question in plan and previous.answer in plan
    later = chat.prompts["reflect"] + chat.prompts["synthesize"]
    assert chat.prompts["synthesize"], "synthesis ran"
    for prompt in later:
        assert previous.question not in prompt
        assert previous.answer not in prompt
        assert _HEADER not in prompt


def test_ask_caps_the_history_oldest_first_with_the_embedders_tokenizer(
    test_db: psycopg.Connection[Any],
) -> None:
    # AC1's "truncated oldest-first at the cap", through ask: FakeEmbedder counts
    # one token per four characters, so 3,000-character answers fit two at a time.
    embedder = FakeEmbedder()
    turns = [AskTurn(f"quarkle question {n}", f"answer{n} " + "z" * 3_000) for n in range(1, 6)]
    chat = _RecordingChat()

    ask(test_db, _cfg(), embedder=embedder, chat=chat, question="q", history=turns)

    (plan,) = chat.prompts["plan"]
    expected = cap_history(turns, cost=embedder.count_tokens)
    assert [t.question for t in expected] == ["quarkle question 4", "quarkle question 5"]
    for n in (1, 2, 3):
        assert f"quarkle question {n}\n" not in plan
    for turn in expected:
        assert f"Q: {turn.question}\nA: {turn.answer}" in plan


def test_a_fresh_ask_has_no_history_in_its_plan_prompt(test_db: psycopg.Connection[Any]) -> None:
    # AC2, at the core: the default is no history.
    chat = _RecordingChat()

    ask(test_db, _cfg(), embedder=FakeEmbedder(), chat=chat, question="q")

    (plan,) = chat.prompts["plan"]
    assert _HEADER not in plan


def test_history_with_no_loop_is_refused(test_db: psycopg.Connection[Any]) -> None:
    # D3: the no-loop path has no plan step, so history would vanish silently.
    with pytest.raises(ValueError, match="planning loop"):
        ask(test_db, _cfg(), embedder=FakeEmbedder(), chat=_RecordingChat(), question="q",
            no_loop=True, history=[_turn(1)])


def test_empty_history_with_no_loop_is_a_plain_quick_answer(
    test_db: psycopg.Connection[Any],
) -> None:
    result = ask(test_db, _cfg(), embedder=FakeEmbedder(), chat=_RecordingChat(),
                 question="q", no_loop=True, history=())

    assert result.fallback_used is True


# ------------------------------------------------------------ MCP unchanged --


def test_mcp_brain_ask_takes_no_history() -> None:
    # AC3: scope out. The tool's parameters are exactly what they were at 7a3931c.
    from brain import mcp_server

    tools = asyncio.run(mcp_server.mcp_app.list_tools())
    (tool,) = [t for t in tools if t.name == "brain_ask"]

    assert sorted(tool.input_schema["properties"]) == [
        "include_confidential", "limit", "max_iterations", "mode", "no_loop", "question",
    ]
