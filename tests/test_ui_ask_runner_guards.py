"""The ask runner's guards, beyond the happy path (RM-18a, PR #79 review round 1).

- A finished ask is not kept alive by ``search``'s module-level query-embedder
  registry past its retention (rev-79, 1).
- The guarded chat forwards every keyword ``ask`` passes (rev-79, 2).
- A cancel or a deadline raised at a query embed propagates through the search
  pipeline instead of becoming a text-only fallback (rev-79, 3).
- A cited document's level is read back, and one that is gone has none, so the lens
  withholds it (aud-79, A1).

Driven with ``run_next()``, a scripted ``ChatJson`` and injected clocks; synthetic data.
"""
from __future__ import annotations

import gc
import logging
import uuid
import weakref
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.ui.ask_service import ASK_KIND, ASK_RETAIN_FOR, AskSpec, submit_ask
from brain.ui.jobs import JobUnknown
from brain.ui.routes_jobs import lens_ask_result
from tests.conftest import FakeEmbedder
from tests.test_ui_ask_service import Clock, ScriptedChat, _context, _run, _seed
from tests.ui_ingest_support import make_registry

_ENOUGH = {"sufficient": True, "follow_up_queries": []}


def _unique(word: str) -> str:
    """A query no earlier test embedded: a cached query vector skips the embed."""
    return f"{word} {uuid.uuid4().hex}"


# ------------------------------------------------------------- no lingering --


class _DateClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_an_evicted_ask_is_not_kept_alive_by_the_query_embedder_registry(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    # search._query_embed keeps the last query embedder in a module-level dict.
    # Before release(), that entry held the guard -> handle -> job -> result
    # (quoted document text) for as long as the process ran.
    query = _unique("alpha")
    _seed(test_db, "Alpha note", f"body of {query}", "normal")
    clock = _DateClock()
    registry = make_registry(clock=clock, retain_for_by_kind={ASK_KIND: ASK_RETAIN_FOR})
    chat = ScriptedChat([{"sub_queries": [query]}, _ENOUGH, {"answer": "A [1]."}])
    ctx = _context(test_db, tmp_path, chat, jobs=registry)
    snap = submit_ask(ctx, registry, AskSpec(question=query, no_loop=False))
    registry.run_next()
    done = registry.get(snap.id)
    assert done.state == "succeeded"
    assert done.result is not None and len(done.result["citations"]) == 1
    del done
    job_ref = weakref.ref(registry._jobs[snap.id])  # the live record behind the id

    clock.now += ASK_RETAIN_FOR + timedelta(seconds=1)
    with pytest.raises(JobUnknown):
        registry.get(snap.id)
    gc.collect()

    assert job_ref() is None


# ------------------------------------------------------- chat kwargs forwarded --


class RecordingChat:
    """Answers by schema and records every keyword it was called with."""

    def __init__(self, sub_query: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self._sub_query = sub_query

    def __call__(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        schema = kwargs["schema"]
        if "sub_queries" in schema:
            return {"sub_queries": [self._sub_query]}
        if "sufficient" in schema:
            return dict(_ENOUGH)
        return {"answer": "A [1]."}


def test_the_guarded_chat_forwards_every_keyword_on_every_call(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    query = _unique("alpha")
    _seed(test_db, "Alpha note", f"body of {query}", "normal")
    chat = RecordingChat(query)
    ctx = _context(
        test_db, tmp_path, chat,
        cfg_overrides={"ask_timeout_seconds": 7.5, "ask_model": "synthetic-model"},
    )

    assert _run(ctx, question=query).state == "succeeded"

    assert [sorted(c["schema"]) for c in chat.calls] == [
        ["sub_queries"], ["follow_up_queries", "sufficient"], ["answer"],
    ]
    assert [c["num_predict"] for c in chat.calls] == [128, 128, 512]
    for call in chat.calls:
        assert call["timeout"] == 7.5
        assert call["model"] == "synthetic-model"
        assert call["cfg"] is ctx.cfg


# -------------------------------------------- guards at a query embed propagate --


class _HookedEmbedder(FakeEmbedder):
    """Runs ``hook`` inside its first embed, then counts every embed it serves."""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Any = None
        self.embeds = 0

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.embeds += 1
        if self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        return super().embed(texts, input_type)


@pytest.mark.parametrize("trigger", ["cancel", "deadline"])
def test_a_guard_at_the_second_query_embed_propagates_and_never_falls_back(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    trigger: str,
) -> None:
    # Two sub-queries, so two query embeds in one retrieve step: the cancel (or
    # the clock jump) lands in the first, and the second embed's guard raises
    # inside hybrid_search. search_with_fts_fallback catches EmbedError only, so
    # it must not turn JobCancelled / AskTimedOut into a text-only retry.
    caplog.set_level(logging.INFO, logger="brain.search_fallback")
    _seed(test_db, "Alpha note", "alpha body", "normal")
    clock = Clock()
    embedder = _HookedEmbedder()
    chat = ScriptedChat(
        [{"sub_queries": [_unique("alpha"), _unique("beta")]}, _ENOUGH, {"answer": "A [1]."}]
    )
    ctx = _context(
        test_db, tmp_path, chat, clock=clock, embedder=embedder,
        cfg_overrides={"ask_max_iterations": 1, "ask_timeout_seconds": 1.0},
    )
    assert ctx.jobs is not None
    jobs = ctx.jobs
    snap = submit_ask(ctx, jobs, AskSpec(question=_unique("q"), no_loop=False))

    def jump() -> None:
        clock.now += 3.0 + 0.001

    embedder.hook = (lambda: jobs.cancel(snap.id)) if trigger == "cancel" else jump
    jobs.run_next()

    final = jobs.get(snap.id)
    assert final.state == ("cancelled" if trigger == "cancel" else "failed")
    if trigger == "deadline":
        assert final.error is not None and final.error.code == "timeout"
    assert embedder.embeds == 1  # the second embed never reached the backend
    assert chat.calls == 1  # the plan only: no synthesis after the guard fired
    assert not [r for r in caplog.records if "text-only" in r.getMessage()]


# ------------------------------------------------------ the level is read back --


def test_a_cited_document_deleted_before_the_read_back_has_no_level_and_is_withheld(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    query = _unique("alpha")
    doc_id = _seed(test_db, "Alpha note", f"body of {query}", "normal")

    def delete_then_cite() -> dict[str, Any]:
        test_db.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
        return {"answer": "A [1]."}

    chat = ScriptedChat([{"sub_queries": [query]}, _ENOUGH, delete_then_cite])
    ctx = _context(test_db, tmp_path, chat)

    snap = _run(ctx, question=query)

    assert snap.state == "succeeded"
    assert snap.result is not None
    (citation,) = snap.result["citations"]
    assert citation["document_id"] == doc_id
    assert citation["sensitivity"] is None
    lensed = lens_ask_result(snap.result, serve_confidential_bodies=False)
    assert lensed is not None
    assert lensed["citations"][0]["withheld"] is True
    assert lensed["answer"] is None
