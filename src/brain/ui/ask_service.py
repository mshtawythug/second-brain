"""Ask jobs: the Agent tab's runner on the shared job registry (RM-18a).

Roadmap spec RM-18, decisions A1–A10. An ask runs on the Ingest tab's one worker as
job kind ``"ask"`` (A1): this machine cannot hold two chat-model runs and an embed
at once. The runner calls :func:`brain.ask.ask`, the function ``brain ask`` and MCP
``brain_ask`` call, in ``hybrid`` mode only (A9).

**Not a mutator.** Ask writes nothing to the corpus, so the Ingest spec's
two-mutator rule (§1.3) is unchanged. The only rows it causes are ``interactions``
rows, written through :func:`brain.ui.telemetry.record_ui_open` like every other UI
open, and only after the last cancel point.

**Cancel** is cooperative (A4): at every step boundary (``ask``'s ``on_step``),
before every chat call and before every embed. ``enter_commit()`` is never called,
so a cancel is honoured until the result is recorded. **The deadline** caps the
whole job at ``ask_max_iterations × 3 × ask_timeout_seconds``, checked at the same
points; passing it fails the job with code ``timeout``.

**Logging** carries the job id and counts only: never the question, the answer,
a follow-up's earlier turns or a title. Failures are logged by the registry, class
only.

**Follow-ups** (RM-18c): :attr:`AskSpec.history` holds the earlier turns the tab
sent, shape-checked by the route and capped by ``ask`` itself before the plan step,
the only step that sees them.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

import psycopg

from ..ask_errors import describe_ask_failure
from ..ask_history import AskTurn
from ..errors import (
    BrainError,
    ChatOutputTruncated,
    EmbedError,
    EnrichmentError,
    OllamaContextOverflow,
    OllamaUnavailable,
)
from ..ingest import Embedder
from .errors import UiForbidden, UiUnavailable
from .ingest_service import IngestRuntime
from .jobs import JobError, JobHandle, JobRegistry, JobSnapshot
from .telemetry import record_ui_open

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ..ask import AskResult, ChatJson
    from ..config import Config
    from .context import UiContext

logger = logging.getLogger(__name__)

ASK_KIND = "ask"

#: A10: ask results quote document bodies, so they are kept for less time than
#: ingest results, and ``GET /api/jobs`` never lists them.
ASK_RETAIN_FOR = timedelta(minutes=10)

#: The longest question ``POST /api/ask`` accepts, in characters.
MAX_QUESTION_CHARS = 2_000

#: Chat calls per iteration the deadline allows for: plan, reflect, synthesize.
_CHAT_CALLS_PER_ITERATION = 3

#: The one mode v1 offers (A9, RM-18a.D1): ``brain.ask.HYBRID_MODE``, spelled here
#: because importing ``brain.ask`` loads ``brain.chat`` and with it ``httpx``, which
#: ``brain.ui.app`` must not (``tests/test_import_cycles.py``). A test pins the two equal.
ASK_MODES: tuple[str, ...] = ("hybrid",)

_TIMEOUT_MESSAGE = (
    "The question took longer than the agent's time limit. Ask a narrower question, "
    "or retry."
)


class AskTimedOut(BrainError):
    """The ask passed its job deadline. Maps to the ``timeout`` code."""


@dataclass(frozen=True)
class AskSpec:
    """A validated ask request."""

    question: str
    no_loop: bool
    #: A follow-up's earlier turns, oldest first (RM-18c); empty for a fresh ask.
    history: tuple[AskTurn, ...] = ()


@dataclass(frozen=True)
class AskRuntime:
    """What an ask needs beyond the worker's embedder: the chat seam and a clock.

    Injected on :class:`~brain.ui.context.UiContext`, so a test passes a scripted
    ``ChatJson`` and a hand-moved clock; production passes
    :func:`brain.chat.chat_json` and :func:`time.monotonic`.
    """

    chat: ChatJson
    clock: Callable[[], float] = time.monotonic


def deadline_seconds(cfg: Config) -> float:
    """The whole job's budget: every iteration's three chat calls at their timeout."""
    return cfg.ask_max_iterations * _CHAT_CALLS_PER_ITERATION * cfg.ask_timeout_seconds


class Deadline:
    """A point on ``clock`` the job may not pass. Reaching it exactly is not late."""

    def __init__(self, clock: Callable[[], float], seconds: float) -> None:
        self._clock = clock
        self._ends = clock() + seconds

    def check(self) -> None:
        if self._clock() > self._ends:
            raise AskTimedOut("the ask job passed its deadline")


class _Cancellable(Protocol):
    """The one :class:`~brain.ui.jobs.JobHandle` method a guard point needs."""

    def check_cancelled(self) -> None: ...


class _Guard:
    """One job's cancel and deadline check, shared by every guard point."""

    def __init__(self, handle: _Cancellable, deadline: Deadline) -> None:
        self._handle = handle
        self._deadline = deadline

    def __call__(self) -> None:
        self._handle.check_cancelled()
        self._deadline.check()


class _GuardedChat:
    """A :class:`~brain.ask.ChatJson` with a cancel and deadline check before every call."""

    def __init__(self, chat: ChatJson, guard: _Guard) -> None:
        self._chat = chat
        self._guard = guard

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
        self._guard()
        return self._chat(
            prompt, schema=schema, cfg=cfg, model=model, num_predict=num_predict,
            timeout=timeout,
        )


def guarded_chat(chat: ChatJson, handle: _Cancellable, deadline: Deadline) -> ChatJson:
    """``chat`` with a cancel and deadline check before every call."""
    return _GuardedChat(chat, _Guard(handle, deadline))


class StepGuardEmbedder:
    """The worker's embedder, with a cancel and deadline check before every embed.

    Not :class:`~brain.ui.ingest_progress.ProgressEmbedder`: that one closes the
    cancel window (``enter_commit``) after its last batch, which would make an ask
    uncancellable after its first query embed, and its ``embedding`` events would
    interleave with the step events (plan doc, D2).
    """

    def __init__(self, inner: Embedder, handle: _Cancellable, deadline: Deadline) -> None:
        self._inner = inner
        self._guard: _Guard | None = _Guard(handle, deadline)
        self.dim = inner.dim
        # C5, as in ProgressEmbedder: both flags live outside the Embedder
        # Protocol, and each is read with the default its one reader uses. Dropped,
        # the F6 egress veto and the FTS-only path would see the wrapper, not the
        # backend.
        self.hosted_egress: bool = getattr(inner, "hosted_egress", False)
        self.produces_embeddings: bool = getattr(inner, "produces_embeddings", True)
        # The query-embed cache keys on the embedder's class, ``_model`` and ``dim``
        # (``search._embedder_identity``). Keyed on this wrapper alone, two backends
        # of one dimension would share cached query vectors, so ``_model`` names the
        # wrapped backend and its model. A cache hit skips ``embed`` and so this
        # guard, which is harmless: it costs no time, and the next step checks.
        self._model = (
            f"{type(inner).__module__}.{type(inner).__qualname__}"
            f"|{getattr(inner, '_model', '')}"
        )

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        if self._guard is not None:
            self._guard()
        return self._inner.embed(texts, input_type=input_type)

    def release(self) -> None:
        """Drop the guard, and with it the job, when the job ends.

        ``search._query_embed`` registers every query embedder in a module-level
        dict that outlives the job. Through the guard, that entry would keep the
        handle, the job and its result (quoted document text) alive past the
        10-minute retention (A10). After release the wrapper only embeds.
        """
        self._guard = None


def ask_failure(exc: Exception) -> JobError:
    """Map a runner's exception to its code and a message that never quotes it.

    The model-side families take :func:`brain.ask_errors.describe_ask_failure`'s
    sentence, so the CLI, MCP and the tab describe one failure one way.
    """
    if isinstance(exc, AskTimedOut):
        return JobError("timeout", _TIMEOUT_MESSAGE)
    code: str | None = None
    if isinstance(exc, OllamaUnavailable):
        code = "chat_unavailable"
    elif isinstance(exc, OllamaContextOverflow):
        code = "context_overflow"
    elif isinstance(exc, ChatOutputTruncated):
        code = "output_truncated"
    elif isinstance(exc, EnrichmentError):
        code = "synthesis_failed"
    elif isinstance(exc, EmbedError):
        code = "embedder_unavailable"
    if code is not None:
        return JobError(code, describe_ask_failure(exc))
    if isinstance(exc, psycopg.Error):
        return JobError("database_error", f"Database error ({type(exc).__name__}).")
    return JobError("internal_error", f"Internal error ({type(exc).__name__}).")


def submit_ask(ctx: UiContext, jobs: JobRegistry, spec: AskSpec) -> JobSnapshot:
    """Queue an ask. Raises ``QueueFull`` / ``ShuttingDown`` from the registry."""
    ingest_runtime, ask_runtime = require_runtimes(ctx)
    return jobs.submit(
        ASK_KIND, ask_runner(ctx, ingest_runtime, ask_runtime, spec), on_error=ask_failure
    )


def ask_runner(
    ctx: UiContext, ingest_runtime: IngestRuntime, ask_runtime: AskRuntime, spec: AskSpec
) -> Callable[[JobHandle], Mapping[str, Any]]:
    """The runner for one ask. See the module docstring for the cancel points."""

    def run(handle: JobHandle) -> Mapping[str, Any]:
        handle.check_cancelled()  # on pick-up
        started = ask_runtime.clock()
        deadline = Deadline(ask_runtime.clock, deadline_seconds(ctx.cfg))
        guard = _Guard(handle, deadline)
        total = 1 if spec.no_loop else ctx.cfg.ask_max_iterations

        def on_step(step: str, iteration: int) -> None:
            guard()
            handle.progress(step, done=iteration, total=total)

        embedder = StepGuardEmbedder(ingest_runtime.embedder(), handle, deadline)
        try:
            result, levels = _ask_and_record(
                ctx, handle, embedder, guarded_chat(ask_runtime.chat, handle, deadline),
                spec, on_step,
            )
        finally:
            embedder.release()
        elapsed_ms = int((ask_runtime.clock() - started) * 1000)
        logger.info(
            "ui ask job %s: %d citations, %d earlier turns sent, in %d ms",
            handle.job_id, len(result.citations), len(spec.history), elapsed_ms,
        )
        return _payload(ctx, result, levels, elapsed_ms)

    return run


def _ask_and_record(
    ctx: UiContext,
    handle: JobHandle,
    embedder: StepGuardEmbedder,
    chat: ChatJson,
    spec: AskSpec,
    on_step: Callable[[str, int], None],
) -> tuple[AskResult, dict[str, str]]:
    """Run the ask, read the cited levels back, then (past the last cancel point) record."""
    # Here, not at module level: see ASK_MODES.
    from ..ask import HYBRID_MODE, ask

    with ctx.connect() as conn:
        result = ask(
            conn,
            ctx.cfg,
            embedder=embedder,
            chat=chat,
            question=spec.question,
            mode=HYBRID_MODE,
            no_loop=spec.no_loop,
            limit=ctx.cfg.ask_docs_per_iter,
            max_iterations=ctx.cfg.ask_max_iterations,
            # A7: an answer draws on confidential bodies only where this
            # server would serve them.
            exclude_confidential=not ctx.serve_confidential_bodies,
            on_step=on_step,
            history=spec.history,
        )
        levels = _stored_sensitivity(conn, [c.document_id for c in result.citations])
        # The last cancel point: nothing has been recorded yet (A4).
        handle.check_cancelled()
        _record_opens(ctx, conn, result)
    return result, levels


def _stored_sensitivity(
    conn: psycopg.Connection[Any], document_ids: list[str]
) -> dict[str, str]:
    """Each cited document's stored level; a document that is gone has none."""
    if not document_ids:
        return {}
    rows = conn.execute(
        "SELECT id::text, sensitivity FROM documents WHERE id = ANY(%s::uuid[])",
        (document_ids,),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _record_opens(ctx: UiContext, conn: psycopg.Connection[Any], result: AskResult) -> None:
    """One ``opened`` interaction per citation, grouped by the ask's session (as the CLI)."""
    session_id = uuid.UUID(result.session_id)
    for citation in result.citations:
        record_ui_open(
            conn,
            enabled=ctx.logging_enabled,
            document_id=citation.document_id,
            query=None,
            session_id=session_id,
        )


def _payload(
    ctx: UiContext, result: AskResult, levels: Mapping[str, str], elapsed_ms: int
) -> dict[str, Any]:
    """``AskResult.to_dict()``, each snippet cut and each level read back."""
    payload = result.to_dict()
    cap = ctx.cfg.snippet_max_chars
    payload["citations"] = [
        {
            **citation,
            "snippet": citation["snippet"][:cap].rstrip(),
            # Read back, never assumed: routes_jobs.lens_ask_result withholds a
            # citation whose level is not "normal", a missing one included.
            "sensitivity": levels.get(citation["document_id"]),
        }
        for citation in payload["citations"]
    ]
    payload["elapsed_ms"] = elapsed_ms
    return payload


def ask_unavailable_reason(ctx: UiContext) -> str | None:
    """Why this server may not ask, or ``None`` (A6).

    ``read_only``, ``requires_loopback`` or ``jobs_unavailable``, in that order: the
    same bind rule as ingest (``ctx.ingest_enabled`` is "loopback and writable").
    """
    if ctx.read_only:
        return "read_only"
    if not ctx.ingest_enabled:
        return "requires_loopback"
    if ctx.jobs is None or ctx.ingest_runtime is None or ctx.ask_runtime is None:
        return "jobs_unavailable"
    return None


def require_ask(ctx: UiContext) -> JobRegistry:
    """Gate ``POST /api/ask``; return the registry. ``--read-only`` never reaches here."""
    if not ctx.ingest_enabled:
        raise UiForbidden(
            "ask is available only when brain ui is bound to this machine; use brain ask",
            code="ask_requires_loopback",
        )
    if ctx.jobs is None:
        raise UiUnavailable("the job runner is not available", code="jobs_unavailable")
    require_runtimes(ctx)
    return ctx.jobs


def require_runtimes(ctx: UiContext) -> tuple[IngestRuntime, AskRuntime]:
    """The worker's embedder factory and the ask runtime, or 503 ``jobs_unavailable``."""
    if ctx.ingest_runtime is None or ctx.ask_runtime is None:
        raise UiUnavailable("the job runner is not available", code="jobs_unavailable")
    return ctx.ingest_runtime, ctx.ask_runtime
