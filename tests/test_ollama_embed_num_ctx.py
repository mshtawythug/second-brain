"""Tests for the embedder's own Ollama context window (``BRAIN_OLLAMA_EMBED_NUM_CTX``).

Spec: ``docs/specs/2026-10-16-ollama-embed-num-ctx-design.md``. Every
``/api/embed`` request carries ``options.num_ctx`` plus ``truncate: false``; the
knob is validated at config load against the chunker ceiling times a tokenizer
margin; an over-window input is an
:class:`~brain.embeddings.OllamaEmbedContextOverflow`, never a silent prefix.
All HTTP goes through ``httpx.MockTransport`` — no real Ollama.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from brain import chat, ollama_budget
from brain import config as config_module
from brain.config import Config, ConfigError
from brain.embeddings import (
    ArcticEmbedder,
    OllamaEmbedContextOverflow,
    OllamaEmbedError,
    Qwen3Embedder,
    make_embedder,
)
from brain.ingest.chunker import chunk_text
from brain.ollama_budget import (
    CONTEXT_OVERFLOW_RE,
    DEFAULT_OLLAMA_EMBED_NUM_CTX,
    DEFAULT_OLLAMA_NUM_CTX,
    EMBED_TOKENIZER_MARGIN_FACTOR,
    INGEST_CHUNK_OVERLAP_TOKENS,
    INGEST_CHUNK_TARGET_TOKENS,
    MIN_OLLAMA_EMBED_NUM_CTX,
)
from tests.conftest import TEST_DATABASE_URL

# Ollama 0.33.0's wire body for an over-window ``/api/embed`` input under
# ``truncate: false`` (``server/routes.go:EmbedHandler`` -> ``api.StatusError``,
# and ``llm/llama_server.go:normalizeEmbeddingError`` for runner-side refusals).
_OLLAMA_EMBED_OVERFLOW_BODY = {"error": "the input length exceeds the context length"}

_BACKENDS = [ArcticEmbedder, Qwen3Embedder]


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))


def _recording(bodies: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        bodies.append(body)
        return httpx.Response(200, json={"embeddings": [[0.0] * 4] * len(body["input"])})

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
    for var in ("BRAIN_OLLAMA_EMBED_NUM_CTX", "BRAIN_OLLAMA_NUM_CTX", "BRAIN_EMBEDDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)


# --- the budget (leaf) ----------------------------------------------------------


def test_min_embed_num_ctx_is_chunk_ceiling_times_margin() -> None:
    """The floor is what the chunker guarantees, widened for the model tokenizer."""
    ceiling = INGEST_CHUNK_TARGET_TOKENS + INGEST_CHUNK_OVERLAP_TOKENS
    assert ceiling == 700
    assert EMBED_TOKENIZER_MARGIN_FACTOR == 2
    assert MIN_OLLAMA_EMBED_NUM_CTX == ceiling * EMBED_TOKENIZER_MARGIN_FACTOR == 1400


def test_default_embed_num_ctx_holds_the_floor() -> None:
    """2,048: Ollama's embedding num_batch default, and above the floor."""
    assert DEFAULT_OLLAMA_EMBED_NUM_CTX == 2048
    assert DEFAULT_OLLAMA_EMBED_NUM_CTX >= MIN_OLLAMA_EMBED_NUM_CTX


def test_chunker_defaults_are_the_budgeted_ones() -> None:
    """``chunk_text``'s defaults ARE the leaf's objects, so the floor cannot drift."""
    params = inspect.signature(chunk_text).parameters
    assert params["target_tokens"].default is INGEST_CHUNK_TARGET_TOKENS
    assert params["overlap_tokens"].default is INGEST_CHUNK_OVERLAP_TOKENS


def test_chat_and_embed_share_one_overflow_pattern() -> None:
    """One pattern classifies both endpoints; chat keeps its historical name."""
    assert chat._CONTEXT_OVERFLOW_RE is ollama_budget.CONTEXT_OVERFLOW_RE
    assert CONTEXT_OVERFLOW_RE.search(json.dumps(_OLLAMA_EMBED_OVERFLOW_BODY))


# --- the wire -------------------------------------------------------------------


@pytest.mark.parametrize("backend", _BACKENDS, ids=["arctic", "qwen3"])
def test_embed_wire_pins_num_ctx_and_truncate(backend: Any) -> None:
    """Regression pin: every batch carries ``options.num_ctx`` and ``truncate:false``.

    ``shift`` is deliberately absent: ``api.EmbedRequest`` has no such field.
    """
    bodies: list[dict[str, Any]] = []
    emb = backend(
        host="http://x", client=_client(_recording(bodies)), batch_size=2, num_ctx=1500
    )
    emb.embed(["a", "b", "c"], input_type="document")
    emb.embed(["q"], input_type="query")
    assert len(bodies) == 3
    for body in bodies:
        assert body["options"] == {"num_ctx": 1500}
        assert body["truncate"] is False
        assert "shift" not in body


@pytest.mark.parametrize("backend", _BACKENDS, ids=["arctic", "qwen3"])
def test_embedder_default_num_ctx_is_2048(backend: Any) -> None:
    """A backend built without ``num_ctx`` sends the budgeted default."""
    bodies: list[dict[str, Any]] = []
    backend(host="http://x", client=_client(_recording(bodies))).embed(["a"])
    assert bodies[0]["options"] == {"num_ctx": DEFAULT_OLLAMA_EMBED_NUM_CTX}


def test_embed_context_overflow_is_typed() -> None:
    """Ollama's over-window 400 is a typed, actionable error — still an OllamaEmbedError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_OLLAMA_EMBED_OVERFLOW_BODY)

    emb = ArcticEmbedder(host="http://x", client=_client(handler), num_ctx=1500)
    longest = " ".join(["word"] * 40)
    assert emb.count_tokens(longest) == 40
    with pytest.raises(OllamaEmbedContextOverflow) as excinfo:
        emb.embed(["short", longest])
    assert isinstance(excinfo.value, OllamaEmbedError)
    message = str(excinfo.value)
    assert "num_ctx=1500" in message
    assert "BRAIN_OLLAMA_EMBED_NUM_CTX" in message
    assert "2 inputs" in message
    assert "40 cl100k tokens" in message


def test_embed_non_overflow_400_stays_plain() -> None:
    """A 400 that is not about the window (bad model) is not misfiled as overflow."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "model 'nope' not found"})

    emb = ArcticEmbedder(host="http://x", client=_client(handler))
    with pytest.raises(OllamaEmbedError) as excinfo:
        emb.embed(["a"])
    assert not isinstance(excinfo.value, OllamaEmbedContextOverflow)


# --- config ---------------------------------------------------------------------


@pytest.mark.usefixtures("isolated_dotenv")
def test_config_embed_num_ctx_default_is_2048() -> None:
    assert Config.load().ollama_embed_num_ctx == 2048


@pytest.mark.usefixtures("isolated_dotenv")
def test_config_embed_num_ctx_honours_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_OLLAMA_EMBED_NUM_CTX", "4096")
    assert Config.load().ollama_embed_num_ctx == 4096


@pytest.mark.usefixtures("isolated_dotenv")
@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_config_rejects_bad_embed_num_ctx(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("BRAIN_OLLAMA_EMBED_NUM_CTX", raw)
    with pytest.raises(ConfigError, match="BRAIN_OLLAMA_EMBED_NUM_CTX"):
        Config.load()


@pytest.mark.usefixtures("isolated_dotenv")
def test_config_rejects_embed_num_ctx_below_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_OLLAMA_EMBED_NUM_CTX", str(MIN_OLLAMA_EMBED_NUM_CTX - 1))
    with pytest.raises(ConfigError, match="BRAIN_OLLAMA_EMBED_NUM_CTX") as excinfo:
        Config.load()
    assert str(MIN_OLLAMA_EMBED_NUM_CTX) in str(excinfo.value)


@pytest.mark.usefixtures("isolated_dotenv")
def test_config_accepts_embed_num_ctx_at_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_OLLAMA_EMBED_NUM_CTX", str(MIN_OLLAMA_EMBED_NUM_CTX))
    assert Config.load().ollama_embed_num_ctx == MIN_OLLAMA_EMBED_NUM_CTX


@pytest.mark.usefixtures("isolated_dotenv")
def test_embed_num_ctx_independent_of_chat_num_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two knobs size different runners; neither moves the other."""
    monkeypatch.setenv("BRAIN_OLLAMA_NUM_CTX", "16384")
    cfg = Config.load()
    assert cfg.ollama_num_ctx == 16384
    assert cfg.ollama_embed_num_ctx == DEFAULT_OLLAMA_EMBED_NUM_CTX
    assert DEFAULT_OLLAMA_EMBED_NUM_CTX != DEFAULT_OLLAMA_NUM_CTX


@pytest.mark.parametrize("backend", ["arctic", "qwen3"])
def test_make_embedder_threads_embed_num_ctx(backend: str) -> None:
    """Production construction threads the EMBED knob — not the chat one.

    The two knobs hold different values so a constructor that read
    ``cfg.ollama_num_ctx`` instead would fail here; the wire pin above proves
    ``_num_ctx`` is what reaches ``options.num_ctx``.
    """
    cfg = Config(
        database_url="postgresql://u:p@localhost:5/db",
        embedder=backend,
        ollama_num_ctx=9000,
        ollama_embed_num_ctx=3000,
    )
    emb = make_embedder(cfg)
    assert isinstance(emb, ArcticEmbedder | Qwen3Embedder)
    assert emb._num_ctx == 3000
