"""The Agent tab's ask runner (RM-18a): steps, cancel, deadline, errors, payload.

Driven with ``run_next()`` on the test thread (no worker, no sleep), a scripted
``ChatJson`` (never a live Ollama) and an injected clock. Documents are synthetic,
ingested into the 5434 test database with the suite's fake embedder.
"""
from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.ask_errors import describe_ask_failure
from brain.errors import (
    ChatOutputTruncated,
    EmbedError,
    EnrichmentError,
    OllamaContextOverflow,
    OllamaUnavailable,
)
from brain.ingest import ExtractedDoc, ingest_document
from brain.ui.ask_service import (
    ASK_KIND,
    AskRuntime,
    AskSpec,
    AskTimedOut,
    StepGuardEmbedder,
    ask_failure,
    deadline_seconds,
    guarded_chat,
    submit_ask,
)
from brain.ui.context import UiContext
from brain.ui.jobs import JobCancelled, JobRegistry, JobSnapshot
from tests.conftest import FakeEmbedder
from tests.ui_ingest_support import make_context, make_registry, make_vault

# ------------------------------------------------------------------ doubles --


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ScriptedChat:
    """Returns queued bodies in order; an item may be a callable run instead."""

    def __init__(self, bodies: list[Any]) -> None:
        self._bodies = list(bodies)
        self.calls = 0

    def __call__(self, prompt: str, *, schema: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls += 1
        item = self._bodies.pop(0)
        if callable(item):
            result: dict[str, Any] = item()
            return result
        if isinstance(item, BaseException):
            raise item
        return dict(item)


def _seed(conn: psycopg.Connection[Any], title: str, body: str, sensitivity: str) -> str:
    result = ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title=title, content=body, content_type="note", source_path=None, metadata={}
        ),
        source_kind="manual",
        sensitivity=sensitivity,
    )
    assert result.document_id is not None
    return result.document_id


def _context(
    conn: psycopg.Connection[Any],
    tmp_path: Path,
    chat: Any,
    *,
    clock: Clock | None = None,
    embedder: Any | None = None,
    jobs: JobRegistry | None = None,
    logging_enabled: bool = False,
    cfg_overrides: dict[str, Any] | None = None,
    **overrides: Any,
) -> UiContext:
    ctx = make_context(
        conn,
        make_vault(tmp_path),
        embedder=embedder or FakeEmbedder(),
        jobs=jobs or make_registry(),
        cfg_overrides=cfg_overrides,
        ask_runtime=AskRuntime(chat=chat, clock=clock or Clock()),
        **overrides,
    )
    return dataclasses.replace(ctx, logging_enabled=logging_enabled)


def _run(
    ctx: UiContext, *, question: str = "What about alpha?", no_loop: bool = False
) -> JobSnapshot:
    assert ctx.jobs is not None
    snap = submit_ask(ctx, ctx.jobs, AskSpec(question=question, no_loop=no_loop))
    assert snap.kind == ASK_KIND
    assert ctx.jobs.run_next() is True
    return ctx.jobs.get(snap.id)


def _ui_interactions(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT count(*) FROM interactions WHERE source = 'ui'").fetchone()
    assert row is not None
    return int(row[0])


_PLAN = {"sub_queries": ["alpha"]}
_ENOUGH = {"sufficient": True, "follow_up_queries": []}
_ANSWER = {"answer": "Alpha is covered [1]."}

# ------------------------------------------------------------------ success --


def test_a_submitted_ask_succeeds_with_the_payload(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    doc_id = _seed(test_db, "Alpha note", "alpha body " * 40, "normal")
    ctx = _context(
        test_db, tmp_path, ScriptedChat([_PLAN, _ENOUGH, _ANSWER]),
        cfg_overrides={"snippet_max_chars": 12},
    )

    snap = _run(ctx)

    assert snap.state == "succeeded", snap.error
    result = snap.result
    assert result is not None
    assert result["answer"] == "Alpha is covered [1]."
    (citation,) = result["citations"]
    assert citation["document_id"] == doc_id
    assert citation["title"] == "Alpha note"
    assert citation["sensitivity"] == "normal"
    assert 0 < len(citation["snippet"]) <= 12
    assert result["sub_queries"] == ("alpha",)
    assert isinstance(result["elapsed_ms"], int)
    for key in ("fallback", "fallback_reason", "fallback_transient", "fallback_note"):
        assert key in result


def test_the_events_name_each_step_in_order(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    _seed(test_db, "Alpha note", "alpha body", "normal")
    ctx = _context(test_db, tmp_path, ScriptedChat([_PLAN, _ENOUGH, _ANSWER]))

    snap = _run(ctx)

    steps = [(e.phase, e.done, e.total) for e in snap.events]
    max_iter = ctx.cfg.ask_max_iterations
    assert steps == [
        ("queued", None, None),
        ("plan", 1, max_iter),
        ("retrieve", 1, max_iter),
        ("reflect", 1, max_iter),
        ("synthesize", 1, max_iter),
    ]


def test_no_loop_reports_two_steps_out_of_one(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _context(test_db, tmp_path, ScriptedChat([{"answer": "Nothing found."}]))

    snap = _run(ctx, no_loop=True)

    assert snap.state == "succeeded"
    assert [(e.phase, e.done, e.total) for e in snap.events[1:]] == [
        ("retrieve", 1, 1), ("synthesize", 1, 1),
    ]


def test_a_succeeded_ask_records_one_ui_open_per_citation(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    # The anti-vacuity twin of the cancel tests: with telemetry on, rows ARE written.
    _seed(test_db, "Alpha note", "alpha body", "normal")
    ctx = _context(
        test_db, tmp_path, ScriptedChat([_PLAN, _ENOUGH, _ANSWER]), logging_enabled=True
    )

    snap = _run(ctx)

    assert snap.state == "succeeded"
    assert _ui_interactions(test_db) == 1


# ------------------------------------------------------------------- cancel --


class CancellingEmbedder(FakeEmbedder):
    """Requests a cancel of the running job from inside its first embed."""

    def __init__(self) -> None:
        super().__init__()
        self.cancel: Callable[[], object] | None = None

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        if self.cancel is not None:
            self.cancel()
            self.cancel = None
        return super().embed(texts, input_type)


@pytest.mark.parametrize("no_loop", [False, True])
def test_a_cancel_during_retrieve_ends_cancelled_with_no_interaction_row(
    test_db: psycopg.Connection[Any], tmp_path: Path, no_loop: bool
) -> None:
    _seed(test_db, "Alpha note", "alpha body", "normal")
    embedder = CancellingEmbedder()
    # Unique query text: a cached query vector would skip the embed this test needs.
    query = f"alpha {uuid.uuid4().hex}"
    plan = {"sub_queries": [query]}
    chat = ScriptedChat([plan, _ENOUGH, _ANSWER] if not no_loop else [_ANSWER])
    ctx = _context(test_db, tmp_path, chat, embedder=embedder, logging_enabled=True)
    assert ctx.jobs is not None
    snap = submit_ask(ctx, ctx.jobs, AskSpec(question=query, no_loop=no_loop))
    jobs = ctx.jobs
    embedder.cancel = lambda: jobs.cancel(snap.id)

    jobs.run_next()

    final = jobs.get(snap.id)
    assert final.state == "cancelled"
    assert final.result is None
    assert _ui_interactions(test_db) == 0
    assert chat.calls == (1 if not no_loop else 0)  # nothing after the retrieve


def test_a_cancel_during_synthesis_is_honoured_before_anything_is_recorded(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    # The last cancel point: after ask returns, before the interaction rows.
    _seed(test_db, "Alpha note", "alpha body", "normal")
    holder: dict[str, Any] = {}

    def cancel_then_answer() -> dict[str, Any]:
        holder["jobs"].cancel(holder["id"])
        return dict(_ANSWER)

    ctx = _context(
        test_db, tmp_path, ScriptedChat([_PLAN, _ENOUGH, cancel_then_answer]),
        logging_enabled=True,
    )
    assert ctx.jobs is not None
    snap = submit_ask(ctx, ctx.jobs, AskSpec(question="What about alpha?", no_loop=False))
    holder.update(jobs=ctx.jobs, id=snap.id)

    ctx.jobs.run_next()

    assert ctx.jobs.get(snap.id).state == "cancelled"
    assert _ui_interactions(test_db) == 0


def test_a_cancel_before_pick_up_never_calls_the_chat(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    chat = ScriptedChat([])
    ctx = _context(test_db, tmp_path, chat)
    assert ctx.jobs is not None
    snap = submit_ask(ctx, ctx.jobs, AskSpec(question="q", no_loop=False))

    ctx.jobs.cancel(snap.id)

    assert ctx.jobs.get(snap.id).state == "cancelled"
    assert chat.calls == 0


# ------------------------------------------------------------------- errors --


def test_chat_down_fails_with_chat_unavailable_and_the_fixed_message(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _context(test_db, tmp_path, ScriptedChat([OllamaUnavailable("conn refused: body")]))

    snap = _run(ctx)

    assert snap.state == "failed"
    assert snap.error is not None
    assert snap.error.code == "chat_unavailable"
    assert snap.error.message == describe_ask_failure(OllamaUnavailable("x"))
    assert "body" not in snap.error.message


_SECRET = "quoted-document-text"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (OllamaUnavailable(_SECRET), "chat_unavailable"),
        (OllamaContextOverflow(_SECRET), "context_overflow"),
        (ChatOutputTruncated(_SECRET, partial=_SECRET, num_predict=1), "output_truncated"),
        (EnrichmentError(_SECRET), "synthesis_failed"),
        (EmbedError(_SECRET, transient=True), "embedder_unavailable"),
        (EmbedError(_SECRET, transient=False), "embedder_unavailable"),
        (psycopg.OperationalError(_SECRET), "database_error"),
        (AskTimedOut(_SECRET), "timeout"),
        (ValueError(_SECRET), "internal_error"),
    ],
)
def test_every_failure_maps_to_its_code_and_never_quotes_the_exception(
    exc: Exception, code: str
) -> None:
    error = ask_failure(exc)

    assert error.code == code
    assert _SECRET not in error.message
    assert error.message


def test_the_timeout_message_is_fixed() -> None:
    assert ask_failure(AskTimedOut("a")).message == ask_failure(AskTimedOut("b")).message


# ----------------------------------------------------------------- deadline --


def test_the_deadline_is_iterations_times_three_times_the_chat_timeout(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _context(
        test_db, tmp_path, ScriptedChat([]),
        cfg_overrides={"ask_max_iterations": 2, "ask_timeout_seconds": 5.0},
    )

    assert deadline_seconds(ctx.cfg) == 30.0


def test_a_chat_that_runs_past_the_deadline_ends_timeout_at_the_next_step(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    _seed(test_db, "Alpha note", "alpha body", "normal")
    clock = Clock()

    def slow_plan() -> dict[str, Any]:
        clock.now += 3.0 + 0.001  # 1 iteration x 3 x 1 s, and a hair more
        return dict(_PLAN)

    chat = ScriptedChat([slow_plan, _ENOUGH, _ANSWER])
    ctx = _context(
        test_db, tmp_path, chat, clock=clock,
        cfg_overrides={"ask_max_iterations": 1, "ask_timeout_seconds": 1.0},
    )

    snap = _run(ctx)

    assert snap.state == "failed"
    assert snap.error is not None and snap.error.code == "timeout"
    assert snap.events[-1].phase == "plan"  # the retrieve step never started
    assert chat.calls == 1


def test_a_chat_that_ends_exactly_on_the_deadline_is_not_late(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    clock = Clock()

    def plan_on_the_line() -> dict[str, Any]:
        clock.now += 3.0
        return dict(_PLAN)

    ctx = _context(
        test_db, tmp_path, ScriptedChat([plan_on_the_line, {"answer": "None."}]), clock=clock,
        cfg_overrides={"ask_max_iterations": 1, "ask_timeout_seconds": 1.0},
    )

    assert _run(ctx).state == "succeeded"


class _Handle:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled("cancel requested")


def test_the_guarded_chat_checks_the_deadline_before_calling() -> None:
    from brain.ui.ask_service import Deadline

    clock = Clock()
    deadline = Deadline(clock, 10.0)
    inner = ScriptedChat([{"answer": "ok"}])
    chat = guarded_chat(inner, _Handle(), deadline)
    clock.now += 10.5

    with pytest.raises(AskTimedOut):
        chat("prompt", schema={"answer": "str"}, cfg=None)  # type: ignore[arg-type]
    assert inner.calls == 0


def test_the_guarded_chat_checks_cancel_before_calling() -> None:
    from brain.ui.ask_service import Deadline

    inner = ScriptedChat([{"answer": "ok"}])
    chat = guarded_chat(inner, _Handle(cancelled=True), Deadline(Clock(), 10.0))

    with pytest.raises(JobCancelled):
        chat("prompt", schema={"answer": "str"}, cfg=None)  # type: ignore[arg-type]
    assert inner.calls == 0


class _Hosted(FakeEmbedder):
    hosted_egress = True
    produces_embeddings = False


def test_the_step_guard_embedder_checks_cancel_and_deadline_before_each_embed() -> None:
    from brain.ui.ask_service import Deadline

    clock = Clock()
    guarded = StepGuardEmbedder(FakeEmbedder(dim=8), _Handle(), Deadline(clock, 1.0))
    assert len(guarded.embed(["a"], input_type="query")) == 1
    clock.now += 2.0
    with pytest.raises(AskTimedOut):
        guarded.embed(["a"], input_type="query")

    cancelled = StepGuardEmbedder(FakeEmbedder(dim=8), _Handle(cancelled=True),
                                  Deadline(Clock(), 1.0))
    with pytest.raises(JobCancelled):
        cancelled.embed(["a"], input_type="query")


def test_the_step_guard_embedder_forwards_the_capability_flags() -> None:
    # C5: dropping these would silently disable the F6 egress veto and the FTS-only path.
    from brain.ui.ask_service import Deadline

    plain = StepGuardEmbedder(FakeEmbedder(dim=8), _Handle(), Deadline(Clock(), 1.0))
    hosted = StepGuardEmbedder(_Hosted(dim=8), _Handle(), Deadline(Clock(), 1.0))

    assert (plain.dim, plain.hosted_egress, plain.produces_embeddings) == (8, False, True)
    assert (hosted.hosted_egress, hosted.produces_embeddings) == (True, False)
    assert plain.count_tokens("abcdefgh") == 2


def test_the_step_guard_embedder_keeps_the_backends_query_cache_identity_apart() -> None:
    # search._query_embed caches by class, `_model` and dim: wrapped, two backends
    # of one dim must still get different keys.
    from brain.search import _embedder_identity
    from brain.ui.ask_service import Deadline

    def wrap(inner: Any) -> StepGuardEmbedder:
        return StepGuardEmbedder(inner, _Handle(), Deadline(Clock(), 1.0))

    assert _embedder_identity(wrap(FakeEmbedder(dim=8))) != _embedder_identity(
        wrap(_Hosted(dim=8))
    )
    assert _embedder_identity(wrap(FakeEmbedder(dim=8))) == _embedder_identity(
        wrap(FakeEmbedder(dim=8))
    )
