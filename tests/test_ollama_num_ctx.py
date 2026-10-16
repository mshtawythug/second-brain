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
from tests.conftest import TEST_DATABASE_URL

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
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: tmp_path / "p.env")
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "h.env"
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    for var in (
        "BRAIN_OLLAMA_NUM_CTX",
        "BRAIN_ENRICH_MAX_INPUT_TOKENS",
        "BRAIN_AUDIO_MAX_INPUT_TOKENS",
    ):
        monkeypatch.delenv(var, raising=False)
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


def test_system_prompts_fit_frame_reserve() -> None:
    """Every enricher system prompt must fit the reserve the budget assumes.

    The reserve covers system prompt + user framing labels + chat template; the
    template/label share is ``CHAT_TEMPLATE_MARGIN_TOKENS``. Enumerated, not
    listed, so a new ``*_SYSTEM_PROMPT`` is covered the day it lands.
    """
    enc = tiktoken.get_encoding("cl100k_base")
    prompts = {
        name: getattr(enrichment, name)
        for name in dir(enrichment)
        if name.endswith("_SYSTEM_PROMPT")
    }
    assert len(prompts) >= 7
    limit = PROMPT_FRAME_RESERVE_TOKENS - CHAT_TEMPLATE_MARGIN_TOKENS
    sizes = {name: len(enc.encode(text)) for name, text in prompts.items()}
    over = {name: size for name, size in sizes.items() if size > limit}
    assert over == {}, f"system prompts over the {limit}-token frame share: {over}"


def test_extract_chunk_constants_are_the_budgeted_ones() -> None:
    assert extract._DEFAULT_CHUNK_TARGET_TOKENS is EXTRACT_CHUNK_TARGET_TOKENS
    assert extract._DEFAULT_CHUNK_OVERLAP_TOKENS is EXTRACT_CHUNK_OVERLAP_TOKENS
    assert enrichment._EXTRACT_NUM_PREDICT is ollama_budget.EXTRACT_NUM_PREDICT


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


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # Ollama's own promptTooLongMessage (server-side check).
        (400, "the prompt is longer than the context length currently available"),
        # llama-server's exceed-context error, surfaced as a 5xx.
        (500, "the request exceeds the available context size, try increasing it"),
    ],
)
def test_context_overflow_5xx_is_overflow_not_unavailable(
    status: int, body: str
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"error": body})

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
