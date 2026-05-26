"""Tests for T5 perf changes: LLM-02 (enrich cap default) + LLM-03 (keep_alive).

Coverage:
- Config.enrich_max_input_tokens default == 1200 (LLM-02)
- BRAIN_OLLAMA_KEEP_ALIVE parsing: valid strings accepted, invalid rejected (LLM-03)
- keep_alive appears in every /api/embed request from ArcticEmbedder (LLM-03)
- keep_alive appears in every /api/embed request from Qwen3Embedder (LLM-03)
- keep_alive appears in every /api/chat request from OllamaEnricher (LLM-03)
- make_embedder threads cfg.ollama_keep_alive into the embedder (LLM-03)
- make_enricher threads cfg.ollama_keep_alive into the enricher (LLM-03)
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from brain import config as config_module
from brain.config import (
    DEFAULT_ENRICH_MAX_INPUT_TOKENS,
    DEFAULT_OLLAMA_KEEP_ALIVE,
    Config,
    ConfigError,
)
from brain.embeddings import ArcticEmbedder, Qwen3Embedder, make_embedder
from brain.enrichment import OllamaEnricher, make_enricher

_TEST_DB_URL = "postgresql://brain:brain@localhost:5434/second_brain_test"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Block all .env file sources so only os.environ values reach Config.load()."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: tmp_path / "project.env")
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)


def _embed_client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(base_url="http://x", transport=transport)


def _ok_embed(n: int = 1, dim: int = 1024) -> httpx.Response:
    return httpx.Response(200, json={"embeddings": [[0.0] * dim] * n})


def _ok_chat(content: str = '{"summary": "ok"}') -> httpx.Response:
    return httpx.Response(
        200,
        json={"message": {"role": "assistant", "content": content}, "done": True},
    )


# ---------------------------------------------------------------------------
# LLM-02: enrich_max_input_tokens default
# ---------------------------------------------------------------------------


def test_default_enrich_max_input_tokens_is_1200() -> None:
    """DEFAULT_ENRICH_MAX_INPUT_TOKENS must be 1200 (was 4000 before T5)."""
    assert DEFAULT_ENRICH_MAX_INPUT_TOKENS == 1200


def test_config_enrich_max_input_tokens_default_is_1200(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """Config.enrich_max_input_tokens defaults to 1200 when env var is absent."""
    monkeypatch.setenv("DATABASE_URL", _TEST_DB_URL)
    monkeypatch.delenv("BRAIN_ENRICH_MAX_INPUT_TOKENS", raising=False)
    cfg = Config.load()
    assert cfg.enrich_max_input_tokens == 1200


# ---------------------------------------------------------------------------
# LLM-03: Config.ollama_keep_alive parsing
# ---------------------------------------------------------------------------


def test_default_ollama_keep_alive_is_30m() -> None:
    """DEFAULT_OLLAMA_KEEP_ALIVE must be '30m'."""
    assert DEFAULT_OLLAMA_KEEP_ALIVE == "30m"


@pytest.mark.parametrize("value", ["30m", "1h", "60s", "60", "5m", "120"])
def test_keep_alive_valid_values_accepted(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """All valid keep_alive formats are accepted and stored verbatim."""
    monkeypatch.setenv("DATABASE_URL", _TEST_DB_URL)
    monkeypatch.setenv("BRAIN_OLLAMA_KEEP_ALIVE", value)
    cfg = Config.load()
    assert cfg.ollama_keep_alive == value


@pytest.mark.parametrize(
    "value",
    [
        # NOTE: empty / whitespace-only strings fall back to the default (not an error),
        # consistent with every other config field's unset-vs-invalid semantics.
        "0",      # zero seconds — unloads model immediately
        "0m",     # zero minutes
        "-1",     # negative integer
        "-30m",   # negative duration
        "abc",    # non-numeric
        "1x",     # unknown unit
        "1.5m",   # float not accepted (Ollama only takes integers)
    ],
)
def test_keep_alive_invalid_values_rejected(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """Invalid keep_alive values raise ConfigError at load time."""
    monkeypatch.setenv("DATABASE_URL", _TEST_DB_URL)
    monkeypatch.setenv("BRAIN_OLLAMA_KEEP_ALIVE", value)
    with pytest.raises(ConfigError):
        Config.load()


def test_keep_alive_absent_uses_default(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """Absent BRAIN_OLLAMA_KEEP_ALIVE falls back to DEFAULT_OLLAMA_KEEP_ALIVE."""
    monkeypatch.setenv("DATABASE_URL", _TEST_DB_URL)
    monkeypatch.delenv("BRAIN_OLLAMA_KEEP_ALIVE", raising=False)
    cfg = Config.load()
    assert cfg.ollama_keep_alive == DEFAULT_OLLAMA_KEEP_ALIVE


# ---------------------------------------------------------------------------
# LLM-03: keep_alive in ArcticEmbedder /api/embed payload
# ---------------------------------------------------------------------------


def test_arctic_embedder_keep_alive_in_request() -> None:
    """ArcticEmbedder sends keep_alive in every /api/embed request body."""
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_embed(1, dim=1024)

    emb = ArcticEmbedder(
        host="http://x",
        client=_embed_client(httpx.MockTransport(handler)),
        keep_alive="45m",
    )
    emb.embed(["hello"], input_type="document")
    body = json.loads(captured[0])
    assert body["keep_alive"] == "45m"


def test_arctic_embedder_default_keep_alive() -> None:
    """ArcticEmbedder defaults to '30m' keep_alive when not specified."""
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_embed(1, dim=1024)

    emb = ArcticEmbedder(
        host="http://x",
        client=_embed_client(httpx.MockTransport(handler)),
    )
    emb.embed(["hello"], input_type="document")
    body = json.loads(captured[0])
    assert body["keep_alive"] == "30m"


def test_arctic_embedder_keep_alive_in_every_batch() -> None:
    """keep_alive appears in every batch request, not just the first."""
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body_bytes = request.read()
        captured.append(body_bytes)
        n = len(json.loads(body_bytes)["input"])
        return _ok_embed(n, dim=1024)

    emb = ArcticEmbedder(
        host="http://x",
        client=_embed_client(httpx.MockTransport(handler)),
        batch_size=2,
        keep_alive="1h",
    )
    emb.embed(["a", "b", "c", "d"], input_type="document")
    assert len(captured) == 2  # two batches
    for raw in captured:
        assert json.loads(raw)["keep_alive"] == "1h"


# ---------------------------------------------------------------------------
# LLM-03: keep_alive in Qwen3Embedder /api/embed payload
# ---------------------------------------------------------------------------


def test_qwen3_embedder_keep_alive_in_request() -> None:
    """Qwen3Embedder sends keep_alive in every /api/embed request body."""
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_embed(1, dim=4096)

    emb = Qwen3Embedder(
        host="http://x",
        client=_embed_client(httpx.MockTransport(handler)),
        keep_alive="2h",
    )
    emb.embed(["hello"], input_type="document")
    body = json.loads(captured[0])
    assert body["keep_alive"] == "2h"


def test_qwen3_embedder_default_keep_alive() -> None:
    """Qwen3Embedder defaults to '30m' keep_alive when not specified."""
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_embed(1, dim=4096)

    emb = Qwen3Embedder(
        host="http://x",
        client=_embed_client(httpx.MockTransport(handler)),
    )
    emb.embed(["hello"], input_type="document")
    body = json.loads(captured[0])
    assert body["keep_alive"] == "30m"


# ---------------------------------------------------------------------------
# LLM-03: keep_alive in OllamaEnricher /api/chat payload
# ---------------------------------------------------------------------------


def test_enricher_keep_alive_in_chat_request() -> None:
    """OllamaEnricher includes keep_alive in the /api/chat request body."""
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_chat()

    enricher = OllamaEnricher(
        host="http://x",
        model="llama3.1:8b",
        client=httpx.Client(
            base_url="http://x", transport=httpx.MockTransport(handler)
        ),
        keep_alive="45m",
    )
    enricher.summarize("title", "some content")
    body = json.loads(captured[0])
    assert body["keep_alive"] == "45m"


def test_enricher_default_keep_alive() -> None:
    """OllamaEnricher defaults to '30m' keep_alive when not specified."""
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return _ok_chat()

    enricher = OllamaEnricher(
        host="http://x",
        model="llama3.1:8b",
        client=httpx.Client(
            base_url="http://x", transport=httpx.MockTransport(handler)
        ),
    )
    enricher.summarize("title", "some content")
    body = json.loads(captured[0])
    assert body["keep_alive"] == "30m"


def test_enricher_keep_alive_in_retry_requests() -> None:
    """keep_alive is present in both the initial and retry /api/chat calls."""
    captured: list[bytes] = []
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "message": {"role": "assistant", "content": "bad json"},
                    "done": True,
                },
            ),
            _ok_chat(),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return next(responses)

    enricher = OllamaEnricher(
        host="http://x",
        model="llama3.1:8b",
        client=httpx.Client(
            base_url="http://x", transport=httpx.MockTransport(handler)
        ),
        keep_alive="1h",
    )
    enricher.summarize("title", "content")
    assert len(captured) == 2  # initial + 1 retry
    for raw in captured:
        assert json.loads(raw)["keep_alive"] == "1h"


# ---------------------------------------------------------------------------
# LLM-03: make_embedder and make_enricher thread cfg.ollama_keep_alive
# ---------------------------------------------------------------------------


def test_make_embedder_arctic_threads_keep_alive(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """make_embedder passes cfg.ollama_keep_alive to the ArcticEmbedder."""
    monkeypatch.setenv("DATABASE_URL", _TEST_DB_URL)
    monkeypatch.setenv("BRAIN_EMBEDDER", "arctic")
    monkeypatch.setenv("BRAIN_OLLAMA_KEEP_ALIVE", "45m")
    cfg = Config.load()
    emb = make_embedder(cfg)
    assert emb._keep_alive == "45m"  # type: ignore[attr-defined]


def test_make_enricher_threads_keep_alive(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """make_enricher passes cfg.ollama_keep_alive to the OllamaEnricher."""
    monkeypatch.setenv("DATABASE_URL", _TEST_DB_URL)
    monkeypatch.setenv("BRAIN_OLLAMA_KEEP_ALIVE", "2h")
    cfg = Config.load()
    enricher = make_enricher(cfg)
    assert enricher._keep_alive == "2h"  # type: ignore[attr-defined]
