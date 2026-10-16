"""Ollama context-window budget: the ``num_ctx`` every chat call requests, and what must fit in it.

Spec: ``docs/specs/2026-10-15-ollama-num-ctx-design.md``.

The brain sends an explicit ``options.num_ctx`` on every ``/api/chat`` request
(``BRAIN_OLLAMA_NUM_CTX``) instead of inheriting the Ollama server's global
default — which on a machine tuned for other tools can be 65,536 tokens and cost
~4.3 GB of KV cache per ``llama3.1:8b`` runner for prompts the brain keeps under
~4k. One value, shared by every caller: Ollama reloads a runner whenever a
request's ``num_ctx`` differs from the loaded one, so per-caller values on a
shared model would thrash.

This module is a LEAF — it imports nothing from :mod:`brain` — so
:mod:`brain.config` can validate the budget at load time without an import
cycle. It is also the single definition of every number that must fit inside
``num_ctx``: the completion budgets, the GraphRAG extract chunk ceiling, and the
truncation-retry growth factor. ``chat``, ``enrichment``, ``audio`` and
``graph_rag.extract`` bind these objects under their historical names rather
than restating them, so the validated arithmetic and the wire values cannot
drift apart.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

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

#: The share of :data:`PROMPT_FRAME_RESERVE_TOKENS` kept for the chat template
#: and user-turn labels. Every enricher system prompt must fit in the rest —
#: asserted by ``tests/test_ollama_num_ctx.py`` so a prompt that outgrows the
#: reserve fails a test instead of silently eating the input budget.
CHAT_TEMPLATE_MARGIN_TOKENS = 256


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
