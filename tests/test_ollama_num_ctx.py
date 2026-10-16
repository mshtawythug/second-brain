"""Tests for the brain's own Ollama context window (``BRAIN_OLLAMA_NUM_CTX``).

Spec: ``docs/specs/2026-10-15-ollama-num-ctx-design.md``. Every ``/api/chat``
request carries ``options.num_ctx`` plus ``truncate: false`` / ``shift: false``;
the capped call budgets are validated at config load; an over-window prompt is
an :class:`~brain.errors.OllamaContextOverflow`, never a silent truncation.
All HTTP goes through ``httpx.MockTransport`` — no real Ollama.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import tiktoken

from brain import audio, chat, enrichment, ollama_budget
from brain import config as config_module
from brain.chat import chat_json, chat_json_with_client
from brain.config import Config, ConfigError
from brain.enrichment import OllamaEnricher, make_enricher
from brain.errors import EnrichmentError, OllamaContextOverflow, OllamaUnavailable
from brain.graph_rag import extract
from brain.graph_rag.extract import make_extractor
from brain.ollama_budget import (
    AUDIO_NUM_PREDICT,
    CHAT_TEMPLATE_MARGIN_TOKENS,
    DEFAULT_OLLAMA_NUM_CTX,
    EXTRACT_CHUNK_OVERLAP_TOKENS,
    EXTRACT_CHUNK_TARGET_TOKENS,
    NUM_PREDICT_RETRY_FACTOR,
    PROMPT_FRAME_RESERVE_TOKENS,
    CallBudget,
    capped_call_budgets,
    num_ctx_violations,
)
from tests.conftest import TEST_DATABASE_URL, isolate_config_dotenv

_MESSAGES = [{"role": "user", "content": "q"}]


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))


def _ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"message": {"role": "assistant", "content": json.dumps(payload)}},
    )


def _capturing(captured: list[dict[str, Any]], payload: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return _ok(payload)

    return handler


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Block every .env source so only ``os.environ`` reaches ``Config.load``."""
    isolate_config_dotenv(
        monkeypatch,
        tmp_path,
        clear=(
            "BRAIN_OLLAMA_NUM_CTX",
            "BRAIN_ENRICH_MAX_INPUT_TOKENS",
            "BRAIN_AUDIO_MAX_INPUT_TOKENS",
        ),
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)


# ---------------------------------------------------------------------------
# ollama_budget — the arithmetic every capped call must satisfy
# ---------------------------------------------------------------------------


def test_required_num_ctx_counts_retry_output() -> None:
    budget = CallBudget(
        caller="c", input_knob="K", input_tokens=1000, num_predict=300
    )
    assert budget.required_num_ctx == (
        1000 + PROMPT_FRAME_RESERVE_TOKENS + 300 * NUM_PREDICT_RETRY_FACTOR
    )


def test_num_ctx_violations_boundary_exactly_fits() -> None:
    budget = CallBudget(caller="c", input_knob="K", input_tokens=10, num_predict=5)
    assert num_ctx_violations(budget.required_num_ctx, (budget,)) == []
    assert len(num_ctx_violations(budget.required_num_ctx - 1, (budget,))) == 1


def test_num_ctx_violation_message_names_caller_knob_and_requirement() -> None:
    budget = CallBudget(
        caller="audio script",
        input_knob="BRAIN_AUDIO_MAX_INPUT_TOKENS",
        input_tokens=5000,
        num_predict=2048,
    )
    (message,) = num_ctx_violations(8192, (budget,))
    assert "audio script" in message
    assert "BRAIN_AUDIO_MAX_INPUT_TOKENS=5000" in message
    assert str(budget.required_num_ctx) in message
    assert "BRAIN_OLLAMA_NUM_CTX=8192" in message


def test_default_capped_call_requirements_are_pinned() -> None:
    """Each capped call's worst case, from the spec's arithmetic.

    enrich 1200 + 1024 + 256x2; extract (1500 + 100) + 1024 + 1024x2;
    audio 3000 + 1024 + 2048x2. A dropped term in any budget moves a number.
    """
    budgets = capped_call_budgets(
        enrich_max_input_tokens=config_module.DEFAULT_ENRICH_MAX_INPUT_TOKENS,
        audio_max_input_tokens=config_module.DEFAULT_AUDIO_MAX_INPUT_TOKENS,
    )
    assert [b.required_num_ctx for b in budgets] == [2736, 4672, 8120]


def test_default_num_ctx_fits_every_default_capped_call() -> None:
    budgets = capped_call_budgets(
        enrich_max_input_tokens=config_module.DEFAULT_ENRICH_MAX_INPUT_TOKENS,
        audio_max_input_tokens=config_module.DEFAULT_AUDIO_MAX_INPUT_TOKENS,
    )
    assert {b.caller for b in budgets} == {
        "enrich summarize",
        "graphrag concept extract (one chunk)",
        "audio script",
    }
    assert num_ctx_violations(DEFAULT_OLLAMA_NUM_CTX, budgets) == []


# Every key any enricher method requires, so one canned reply satisfies them all.
_UNIVERSAL_REPLY = {
    "summary": "s",
    "tags": [],
    "entities": [],
    "title": "t",
    "rule": "r",
    "contradicts": False,
    "rationale": "x",
}


def _frame_calls(enricher: OllamaEnricher) -> dict[str, Any]:
    """Each enricher method, called with EMPTY variable inputs.

    What reaches the wire is then exactly the frame: the system prompt plus the
    fixed user-turn labels. ``summarize_bucket`` passes the ``prev_*`` values so
    its optional PREVIOUS block is measured too (the larger frame).
    """
    return {
        "summarize": lambda: enricher.summarize("", ""),
        "propose_tags": lambda: enricher.propose_tags(
            title="", summary="", existing_vocab=[], current_tags=[]
        ),
        "extract_entities": lambda: enricher.extract_entities(""),
        "summarize_group": lambda: enricher.summarize_group(
            person=None, entity_names=[], doc_titles=[]
        ),
        "summarize_bucket": lambda: enricher.summarize_bucket(
            bucket_label="",
            entity_name="",
            doc_titles=[],
            cotopics=[],
            doc_summaries=[],
            prev_bucket_label="p",
            prev_cotopics=[],
            prev_synthesis="p",
        ),
        "draft_rule": lambda: enricher.draft_rule(subject="", evidence_texts=[""]),
        "assess_contradiction": lambda: enricher.assess_contradiction(
            subject="", summaries=["", ""]
        ),
    }


def test_prompt_frames_fit_frame_reserve() -> None:
    """Every enricher call's system prompt + fixed user framing fits the reserve.

    The budget check assumes ``PROMPT_FRAME_RESERVE_TOKENS`` covers everything
    around a call's bounded input; ``CHAT_TEMPLATE_MARGIN_TOKENS`` of it is kept
    for the model's chat template and short variable fields (a title). The frame
    is measured off the real wire messages, not restated, and every
    ``*_SYSTEM_PROMPT`` must appear on the wire so a new prompt cannot dodge it.
    """
    enc = tiktoken.get_encoding("cl100k_base")
    captured: list[dict[str, Any]] = []
    enricher = OllamaEnricher(
        host="http://x", model="m", client=_client(_capturing(captured, _UNIVERSAL_REPLY))
    )
    frames: dict[str, int] = {}
    for method, call in _frame_calls(enricher).items():
        call()
        messages = captured[-1]["messages"]
        frames[method] = sum(len(enc.encode(m["content"])) for m in messages)

    system_prompts = {
        getattr(enrichment, name)
        for name in dir(enrichment)
        if name.endswith("_SYSTEM_PROMPT")
    }
    on_wire = {m["content"] for body in captured for m in body["messages"]}
    assert system_prompts <= on_wire, "a *_SYSTEM_PROMPT is not exercised here"
    limit = PROMPT_FRAME_RESERVE_TOKENS - CHAT_TEMPLATE_MARGIN_TOKENS
    over = {method: size for method, size in frames.items() if size > limit}
    assert over == {}, f"prompt frames over the {limit}-token share: {over}"


def test_extract_chunk_constants_are_the_budgeted_ones() -> None:
    assert extract._DEFAULT_CHUNK_TARGET_TOKENS is EXTRACT_CHUNK_TARGET_TOKENS
    assert extract._DEFAULT_CHUNK_OVERLAP_TOKENS is EXTRACT_CHUNK_OVERLAP_TOKENS
    assert enrichment._EXTRACT_NUM_PREDICT is ollama_budget.EXTRACT_NUM_PREDICT


def test_draft_rule_sends_the_budgeted_default_num_predict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``draft_rule`` must read the shared default, not restate ``256``.

    The sentinel value proves the call site reads the binding at call time; a
    restated literal would still send 256.
    """
    monkeypatch.setattr(enrichment, "CHAT_DEFAULT_NUM_PREDICT", 321)
    captured: list[dict[str, Any]] = []
    enricher = OllamaEnricher(
        host="http://x",
        model="m",
        client=_client(_capturing(captured, {"title": "t", "rule": "r"})),
    )
    enricher.draft_rule(subject="s", evidence_texts=["e"])
    assert captured[0]["options"]["num_predict"] == 321


def test_audio_num_predict_is_the_budgeted_one() -> None:
    assert audio._DEFAULT_NUM_PREDICT is AUDIO_NUM_PREDICT


def test_chat_default_num_predict_is_the_budgeted_one() -> None:
    assert chat.DEFAULT_NUM_PREDICT is ollama_budget.CHAT_DEFAULT_NUM_PREDICT


# ---------------------------------------------------------------------------
# chat transport — what goes on the wire
# ---------------------------------------------------------------------------


def test_chat_once_sends_num_ctx_truncate_shift() -> None:
    captured: list[dict[str, Any]] = []
    chat_json_with_client(
        _client(_capturing(captured, {"answer": 1})),
        model="m",
        messages=_MESSAGES,
        required_keys=("answer",),
        keep_alive="30m",
        num_ctx=12345,
    )
    (body,) = captured
    assert body["options"]["num_ctx"] == 12345
    assert body["truncate"] is False
    assert body["shift"] is False


def test_chat_json_threads_cfg_num_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        chat,
        "_build_client",
        lambda host, timeout: _client(_capturing(captured, {"answer": 1})),
    )
    cfg = Config(database_url="postgresql://u:p@localhost:5/db", ollama_num_ctx=6000)
    chat_json("q", schema={"answer": "int"}, cfg=cfg)
    assert captured[0]["options"]["num_ctx"] == 6000


def test_truncation_retry_grows_by_shared_factor() -> None:
    budgets: list[int] = []
    replies = [
        httpx.Response(200, json={"message": {"content": '{"answer": "cut'}}),
        _ok({"answer": "whole"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(json.loads(request.read())["options"]["num_predict"])
        return replies[len(budgets) - 1]

    chat_json_with_client(
        _client(handler),
        model="m",
        messages=_MESSAGES,
        required_keys=("answer",),
        keep_alive="30m",
        num_ctx=8192,
        num_predict=100,
    )
    assert budgets == [100, 100 * NUM_PREDICT_RETRY_FACTOR]


# The body Ollama 0.33.0 actually sends with truncate:false. llama-server's
# exceed-context error is relayed as HTTP 400 whose ``error`` field is the
# runner's raw JSON body as an escaped STRING (llm/llama_server.go:1648-1654,
# server/routes.go:3028-3031): 199 characters, "context size" at index 83, so a
# classifier that looked at a shorter prefix would miss it.
_OLLAMA_0_33_EXCEED_CONTEXT_BODY = json.dumps(
    {
        "error": json.dumps(
            {
                "error": {
                    "code": 400,
                    "message": (
                        "the request exceeds the available context size, "
                        "try increasing it"
                    ),
                    "type": "exceed_context_size_error",
                    "n_prompt_tokens": 9000,
                    "n_ctx": 8192,
                }
            },
            separators=(",", ":"),
        )
    },
    separators=(",", ":"),
)


def test_realistic_overflow_body_shape() -> None:
    """Pin the fixture to the measured wire shape the classifier must handle."""
    assert len(_OLLAMA_0_33_EXCEED_CONTEXT_BODY) == 199
    assert _OLLAMA_0_33_EXCEED_CONTEXT_BODY.find("context size") == 83


@pytest.mark.parametrize(
    ("status", "raw_body"),
    [
        # The real path (truncate:false): llama-server's error, nested, as a 400.
        (400, _OLLAMA_0_33_EXCEED_CONTEXT_BODY),
        # Ollama's own promptTooLongMessage. Only reachable when truncate is
        # TRUE (llama_server.go:281 passes the prompt through untouched when it
        # is false), so not on the brain's path; kept because the wording is
        # Ollama's and the match must not depend on which side refused.
        (400, json.dumps({"error": "the prompt is longer than the context length"})),
        # The same wording on a 5xx. Not what 0.33.0 sends; pins that the
        # classification is by BODY, not status, so a proxy or another version
        # that reports it as a 500 is still an overflow, not "Ollama down".
        (500, json.dumps({"error": "the request exceeds the available context size"})),
    ],
)
def test_context_overflow_is_overflow_not_unavailable(
    status: int, raw_body: str
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            status, content=raw_body, headers={"content-type": "application/json"}
        )

    with pytest.raises(OllamaContextOverflow) as excinfo:
        chat_json_with_client(
            _client(handler),
            model="m",
            messages=_MESSAGES,
            required_keys=("answer",),
            keep_alive="30m",
            num_ctx=4321,
        )
    assert not isinstance(excinfo.value, OllamaUnavailable)
    assert isinstance(excinfo.value, EnrichmentError)
    assert "4321" in str(excinfo.value)
    assert "BRAIN_OLLAMA_NUM_CTX" in str(excinfo.value)
    assert calls == [1]  # permanent: no retry


def test_plain_5xx_is_still_unavailable() -> None:
    with pytest.raises(OllamaUnavailable):
        chat_json_with_client(
            _client(lambda request: httpx.Response(503, text="overloaded")),
            model="m",
            messages=_MESSAGES,
            required_keys=("answer",),
            keep_alive="30m",
            num_ctx=8192,
        )


# ---------------------------------------------------------------------------
# enricher / extractor wiring
# ---------------------------------------------------------------------------


def test_enricher_sends_its_num_ctx() -> None:
    captured: list[dict[str, Any]] = []
    enricher = OllamaEnricher(
        host="http://x",
        model="m",
        client=_client(_capturing(captured, {"summary": "s"})),
        num_ctx=7777,
    )
    enricher.summarize("t", "body")
    assert captured[0]["options"]["num_ctx"] == 7777


def test_enricher_default_num_ctx_is_the_budget_default() -> None:
    captured: list[dict[str, Any]] = []
    enricher = OllamaEnricher(
        host="http://x",
        model="m",
        client=_client(_capturing(captured, {"summary": "s"})),
    )
    enricher.summarize("t", "body")
    assert captured[0]["options"]["num_ctx"] == DEFAULT_OLLAMA_NUM_CTX


def test_make_enricher_threads_num_ctx() -> None:
    cfg = Config(database_url="postgresql://u:p@localhost:5/db", ollama_num_ctx=9000)
    assert make_enricher(cfg)._num_ctx == 9000


@pytest.mark.real_llm_backends
def test_make_extractor_threads_num_ctx() -> None:
    cfg = Config(database_url="postgresql://u:p@localhost:5/db", ollama_num_ctx=9001)
    assert make_extractor(cfg)._enricher._num_ctx == 9001


# ---------------------------------------------------------------------------
# config — the knob and its load-time budget check
# ---------------------------------------------------------------------------


def test_config_num_ctx_default_is_8192(isolated_dotenv: None) -> None:
    assert DEFAULT_OLLAMA_NUM_CTX == 8192
    assert config_module.DEFAULT_OLLAMA_NUM_CTX is DEFAULT_OLLAMA_NUM_CTX
    assert Config.load().ollama_num_ctx == 8192


def test_config_num_ctx_from_env(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None
) -> None:
    monkeypatch.setenv("BRAIN_OLLAMA_NUM_CTX", "16384")
    assert Config.load().ollama_num_ctx == 16384


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "8k"])
def test_config_rejects_malformed_num_ctx(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None, raw: str
) -> None:
    monkeypatch.setenv("BRAIN_OLLAMA_NUM_CTX", raw)
    with pytest.raises(ConfigError, match="BRAIN_OLLAMA_NUM_CTX"):
        Config.load()


def test_config_rejects_num_ctx_below_budget(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None
) -> None:
    # 4096 cannot hold the default audio script (3000 + 1024 + 2048x2 = 8120).
    monkeypatch.setenv("BRAIN_OLLAMA_NUM_CTX", "4096")
    with pytest.raises(ConfigError, match="audio script") as excinfo:
        Config.load()
    assert "BRAIN_AUDIO_MAX_INPUT_TOKENS=3000" in str(excinfo.value)


def test_config_rejects_raised_cap_at_explicit_num_ctx(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None
) -> None:
    monkeypatch.setenv("BRAIN_AUDIO_MAX_INPUT_TOKENS", "3073")
    monkeypatch.setenv("BRAIN_OLLAMA_NUM_CTX", "8192")
    with pytest.raises(ConfigError, match="BRAIN_OLLAMA_NUM_CTX=8192"):
        Config.load()


def test_config_unset_num_ctx_grows_to_fit_raised_cap(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None
) -> None:
    """REGRESSION (suite of record, 2026-10-15): an existing
    ``BRAIN_ENRICH_MAX_INPUT_TOKENS=8000`` made ``Config.load`` raise once the
    budget check landed, breaking every command for a user who had raised the
    cap. With ``BRAIN_OLLAMA_NUM_CTX`` unset the default grows to the smallest
    window that holds every capped call instead.
    """
    monkeypatch.setenv("BRAIN_ENRICH_MAX_INPUT_TOKENS", "8000")
    cfg = Config.load()
    assert cfg.ollama_num_ctx == 8000 + PROMPT_FRAME_RESERVE_TOKENS + 256 * 2
    assert cfg.ollama_num_ctx > DEFAULT_OLLAMA_NUM_CTX


def test_config_accepts_raised_cap_with_raised_num_ctx(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: None
) -> None:
    monkeypatch.setenv("BRAIN_AUDIO_MAX_INPUT_TOKENS", "6000")
    monkeypatch.setenv("BRAIN_OLLAMA_NUM_CTX", "12000")
    cfg = Config.load()
    assert (cfg.audio_max_input_tokens, cfg.ollama_num_ctx) == (6000, 12000)
