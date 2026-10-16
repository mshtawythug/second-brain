"""Ollama context-window budget: the ``num_ctx`` every brain call requests, and what must fit in it.

Specs: ``docs/specs/2026-10-15-ollama-num-ctx-design.md`` (chat) and
``docs/specs/2026-10-16-ollama-embed-num-ctx-design.md`` (embed).

The brain sends an explicit ``options.num_ctx`` on every ``/api/chat`` request
(``BRAIN_OLLAMA_NUM_CTX``) instead of inheriting the Ollama server's global
default — which on a machine tuned for other tools can be 65,536 tokens and cost
4,352 MiB (~4.25 GiB) of KV cache per ``llama3.1:8b`` runner for prompts the brain keeps under
~4k. One value, shared by every caller: Ollama reloads a runner whenever a
request's ``num_ctx`` differs from the loaded one, so per-caller values on a
shared model would thrash.

This module is a LEAF — it imports nothing from :mod:`brain` — so
:mod:`brain.config` can validate the budget at load time without an import
cycle. It is also the single definition of every number the load-time check
reads: the shared default completion budget and the capped calls' budgets
(extract, audio), the GraphRAG extract chunk ceiling, and the truncation-retry
growth factor. ``chat``, ``enrichment``, ``audio`` and ``graph_rag.extract``
bind these objects under their historical names rather than restating them, so
the validated arithmetic and the wire values cannot drift apart. Budgets that
belong to uncapped callers and differ from the default (``ask``'s 128/128/512,
``assess_contradiction``'s 200) stay with their callers; the server-side refusal
bounds those calls, not this check.

The embedder has its OWN window (``BRAIN_OLLAMA_EMBED_NUM_CTX``): it is a
different model, so it loads a different runner and shares nothing with the
chat value. Its floor is the ingest chunker's ceiling times a tokenizer margin;
``chunker.chunk_text`` binds the chunk constants below as its defaults for the
same no-drift reason as the extract ones. :data:`CONTEXT_OVERFLOW_RE` is the
one classifier both transports use for the server's over-window refusal.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# An HTTP error body naming the context window. With ``truncate: false`` Ollama
# 0.33.0 relays llama-server's exceed-context chat error ("... exceeds the
# available context size ...") as an HTTP 400 whose ``error`` is the runner's
# raw JSON body as an escaped string -- the phrase sits ~83 characters in, inside
# the 200-character preview ``chat`` searches. Ollama's own
# ``promptTooLongMessage`` ("... longer than the context length ...") is the
# truncate:true wording, kept in the pattern. ``/api/embed`` answers an
# over-window input with ``{"error":"the input length exceeds the context
# length"}`` (``server/routes.go:EmbedHandler``; runner-side refusals are
# normalised to the same text by ``llm/llama_server.go:normalizeEmbeddingError``).
# Matched on the body, not the status, so the classification does not depend on
# which side refused or what code it chose. ONE pattern for both endpoints.
CONTEXT_OVERFLOW_RE = re.compile(r"context (length|size)", re.IGNORECASE)

#: Default ``options.num_ctx`` for every brain ``/api/chat`` request. The
#: largest brain call (the audio script: a 3,000-token prompt plus a 2,048-token
#: completion that may double on retry) needs ~7.1k; 8,192 holds it. Measured on
#: ``llama3.1:8b`` with a q8_0 KV cache: 544 MiB of KV at 8,192 vs 4,352 MiB at
#: 65,536.
DEFAULT_OLLAMA_NUM_CTX = 8192

#: ``chat.chat_json_with_client`` multiplies ``num_predict`` by this on a retry
#: after a truncated JSON reply, so a call's worst-case output is
#: ``num_predict * NUM_PREDICT_RETRY_FACTOR``.
NUM_PREDICT_RETRY_FACTOR = 2

#: Default completion budget for a JSON-mode chat call (summary, tags, group /
#: bucket synthesis, elicit drafts, brief suggestions).
CHAT_DEFAULT_NUM_PREDICT = 256

#: Completion budget for GraphRAG concept extraction — larger than the default
#: because one chunk can yield a long entity list.
EXTRACT_NUM_PREDICT = 1024

#: Completion budget for the ``brain audio`` dialogue script.
AUDIO_NUM_PREDICT = 2048

#: GraphRAG concept extraction splits a document into chunks of this many
#: ``cl100k_base`` tokens (one model call each) ...
EXTRACT_CHUNK_TARGET_TOKENS = 1500

#: ... with this much overlap, and ``chunk_text`` guarantees no chunk exceeds
#: target + overlap. Together they bound one extraction call's input.
EXTRACT_CHUNK_OVERLAP_TOKENS = 100

#: Tokens reserved on top of a call's bounded input for everything around it:
#: the system prompt, the user-turn labels, and the model's chat template.
PROMPT_FRAME_RESERVE_TOKENS = 1024

#: The share of :data:`PROMPT_FRAME_RESERVE_TOKENS` kept for the model's chat
#: template and short variable fields (a title). Every enricher call's system
#: prompt plus its fixed user-turn labels must fit in the rest — measured off
#: the real wire messages by ``tests/test_ollama_num_ctx.py``, so a frame that
#: outgrows the reserve fails a test instead of silently eating the input budget.
CHAT_TEMPLATE_MARGIN_TOKENS = 256


#: The ingest chunker's defaults (``chunker.chunk_text`` binds these objects):
#: chunks of this many ``cl100k_base`` tokens ...
INGEST_CHUNK_TARGET_TOKENS = 600

#: ... plus this much overlap. ``chunk_text`` guarantees no chunk exceeds
#: target + overlap (700) — the bound on what ingest hands ``/api/embed``.
INGEST_CHUNK_OVERLAP_TOKENS = 100

#: ``count_tokens`` is cl100k, not the embedding model's tokenizer. Measured
#: over every stored chunk (2026-10-16, spec §2.2): worst model/cl100k ratio
#: 1.90 (arctic, XLM-R) and 1.76 (qwen3), p99 1.55 / 1.46.
EMBED_TOKENIZER_MARGIN_FACTOR = 2

#: The smallest ``BRAIN_OLLAMA_EMBED_NUM_CTX`` that holds what the chunker
#: produces today under the margin: (600 + 100) x 2 = 1,400. An explicit value
#: below this is a ``ConfigError`` at load.
MIN_OLLAMA_EMBED_NUM_CTX = (
    INGEST_CHUNK_TARGET_TOKENS + INGEST_CHUNK_OVERLAP_TOKENS
) * EMBED_TOKENIZER_MARGIN_FACTOR

#: Default ``options.num_ctx`` for every brain ``/api/embed`` request. Equal to
#: Ollama's ``DefaultEmbeddingNumBatch``, so an embedding runner's ``n_ubatch``
#: — already arctic's real per-input ceiling — is unchanged; holds the measured
#: corpus maximum (1,689 qwen3 tokens, legacy pre-``0d8a97b`` chunks included);
#: and, on a server whose ``OLLAMA_CONTEXT_LENGTH`` is >= 40,960, shrinks
#: qwen3-embedding:8b's q8_0 KV cache from ~3,060 MiB (its 40,960 trained
#: window) to ~153 MiB. Arctic (non-causal BERT) has no KV cache at all.
DEFAULT_OLLAMA_EMBED_NUM_CTX = 2048


@dataclass(frozen=True)
class CallBudget:
    """One capped chat call: its bounded input and its completion budget."""

    caller: str
    input_knob: str
    input_tokens: int
    num_predict: int

    @property
    def required_num_ctx(self) -> int:
        """The smallest ``num_ctx`` that holds this call's worst case."""
        return (
            self.input_tokens
            + PROMPT_FRAME_RESERVE_TOKENS
            + self.num_predict * NUM_PREDICT_RETRY_FACTOR
        )


def capped_call_budgets(
    *, enrich_max_input_tokens: int, audio_max_input_tokens: int
) -> tuple[CallBudget, ...]:
    """The chat calls whose input is bounded by configuration or a fixed ceiling.

    Callers whose input has no cap (the tag vocabulary, timeline titles, ``ask``
    snippets) are not listed: they are bounded at runtime by the server, which
    refuses an over-window prompt because the transport sends
    ``truncate: false`` / ``shift: false``.
    """
    return (
        CallBudget(
            caller="enrich summarize",
            input_knob="BRAIN_ENRICH_MAX_INPUT_TOKENS",
            input_tokens=enrich_max_input_tokens,
            num_predict=CHAT_DEFAULT_NUM_PREDICT,
        ),
        CallBudget(
            caller="graphrag concept extract (one chunk)",
            input_knob="EXTRACT_CHUNK_TARGET_TOKENS+EXTRACT_CHUNK_OVERLAP_TOKENS",
            input_tokens=EXTRACT_CHUNK_TARGET_TOKENS + EXTRACT_CHUNK_OVERLAP_TOKENS,
            num_predict=EXTRACT_NUM_PREDICT,
        ),
        CallBudget(
            caller="audio script",
            input_knob="BRAIN_AUDIO_MAX_INPUT_TOKENS",
            input_tokens=audio_max_input_tokens,
            num_predict=AUDIO_NUM_PREDICT,
        ),
    )


def num_ctx_violations(num_ctx: int, budgets: Iterable[CallBudget]) -> list[str]:
    """One human-readable message per budget that does not fit in ``num_ctx``."""
    return [
        f"{budget.caller} needs num_ctx >= {budget.required_num_ctx} "
        f"({budget.input_knob}={budget.input_tokens} input + "
        f"{PROMPT_FRAME_RESERVE_TOKENS} prompt-frame reserve + "
        f"{budget.num_predict}x{NUM_PREDICT_RETRY_FACTOR} output) but "
        f"BRAIN_OLLAMA_NUM_CTX={num_ctx}"
        for budget in budgets
        if budget.required_num_ctx > num_ctx
    ]
