"""The Agent tab's HTTP contract (RM-18a): config, submit, listing, lens, retention.

Route tests on the 5434 database. Jobs run with ``run_next()`` on the test thread
(the client is never entered as a context manager, so the lifespan never starts
the worker), and the chat is a scripted ``ChatJson`` injected through
``UiContext.ask_runtime``: no Ollama. All data is synthetic.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.ask_history import MAX_HISTORY_ANSWER_CHARS, MAX_HISTORY_TURNS
from brain.ingest import ExtractedDoc, ingest_document
from brain.ui.app import create_app
from brain.ui.ask_service import ASK_KIND, ASK_RETAIN_FOR, MAX_QUESTION_CHARS, AskRuntime
from brain.ui.context import UiContext
from brain.ui.jobs import JobHandle, JobRegistry
from brain.ui.routes_jobs import lens_ask_result
from tests.conftest import FakeEmbedder
from tests.ui_ingest_support import ORIGIN, WRITE_HEADERS, make_context, make_registry, make_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _Chat:
    def __init__(self, bodies: list[dict[str, Any]]) -> None:
        self._bodies = list(bodies)

    def __call__(self, prompt: str, *, schema: dict[str, Any], **_: Any) -> dict[str, Any]:
        return dict(self._bodies.pop(0))


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _ctx(
    conn: psycopg.Connection[Any],
    tmp_path: Path,
    *,
    chat: Any = None,
    registry: JobRegistry | None = None,
    **overrides: Any,
) -> UiContext:
    runtime = AskRuntime(chat=chat or _Chat([]))
    ctx = make_context(
        conn, make_vault(tmp_path), embedder=FakeEmbedder(),
        jobs=registry or make_registry(), ask_runtime=runtime,
    )
    return dataclasses.replace(ctx, **overrides)


def _client(ctx: UiContext) -> TestClient:
    return TestClient(create_app(ctx), base_url=ORIGIN)


def _ask(client: TestClient, payload: Mapping[str, Any]) -> Any:
    return client.post("/api/ask", json=payload, headers=WRITE_HEADERS)


def _seed(conn: psycopg.Connection[Any], title: str, sensitivity: str) -> str:
    result = ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title=title, content=f"{title} quillbeam body", content_type="note",
            source_path=None, metadata={},
        ),
        source_kind="manual",
        sensitivity=sensitivity,
    )
    assert result.document_id is not None
    return result.document_id


# ------------------------------------------------------------------ config --


def test_config_on_a_loopback_writable_server(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    payload = _client(_ctx(test_db, tmp_path)).get("/api/ask/config").json()

    assert payload == {
        "enabled": True,
        "reason": None,
        "max_question_chars": MAX_QUESTION_CHARS,
        "max_history_turns": MAX_HISTORY_TURNS,
        "modes": ["hybrid"],
    }


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"read_only": True}, "read_only"),
        ({"read_only": True, "ingest_enabled": False}, "read_only"),
        ({"ingest_enabled": False}, "requires_loopback"),
        ({"ask_runtime": None}, "jobs_unavailable"),
        ({"jobs": None}, "jobs_unavailable"),
        ({"ingest_runtime": None}, "jobs_unavailable"),
    ],
)
def test_config_names_why_ask_is_off(
    test_db: psycopg.Connection[Any], tmp_path: Path, overrides: dict[str, Any], reason: str
) -> None:
    payload = _client(_ctx(test_db, tmp_path, **overrides)).get("/api/ask/config").json()

    assert (payload["enabled"], payload["reason"]) == (False, reason)


# ------------------------------------------------------------------ submit --


def test_submit_is_202_and_the_job_succeeds_by_id(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path, chat=_Chat([{"answer": "Nothing on that."}]))
    client = _client(ctx)

    response = _ask(client, {"question": "  What about quillbeam?  ", "no_loop": True})

    assert response.status_code == 202
    snap = response.json()
    assert (snap["kind"], snap["state"], snap["result"]) == (ASK_KIND, "queued", None)
    assert ctx.jobs is not None and ctx.jobs.run_next()
    done = client.get(f"/api/jobs/{snap['id']}").json()
    assert done["state"] == "succeeded"
    assert done["result"]["answer"] == "Nothing on that."
    assert done["result"]["sub_queries"] == ["What about quillbeam?"]  # stripped
    assert done["result"]["title_withheld"] is False


def test_no_loop_defaults_to_false(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    chat = _Chat([{"sub_queries": []}, {"answer": "None."}])
    ctx = _ctx(test_db, tmp_path, chat=chat)
    client = _client(ctx)

    snap = _ask(client, {"question": "q"}).json()
    assert ctx.jobs is not None and ctx.jobs.run_next()

    phases = [e["phase"] for e in client.get(f"/api/jobs/{snap['id']}").json()["events"]]
    assert phases[:2] == ["queued", "plan"]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "invalid_question"),
        ({"question": ""}, "invalid_question"),
        ({"question": "   "}, "invalid_question"),
        ({"question": 7}, "invalid_question"),
        ({"question": "x" * (MAX_QUESTION_CHARS + 1)}, "question_too_long"),
        ({"question": "q", "no_loop": "yes"}, "invalid_no_loop"),
        ({"question": "q", "mode": "fuse"}, "unknown_field"),
        ({"question": "q", "history": {}}, "invalid_history"),
        ({"question": "q", "history": ["q"]}, "invalid_history"),
        ({"question": "q", "history": [{"question": "a"}]}, "invalid_history"),
        ({"question": "q", "history": [{"question": "a", "answer": 7}]}, "invalid_history"),
        ({"question": "q", "history": [{"question": " ", "answer": "b"}]}, "invalid_history"),
        ({"question": "q", "history": [{"question": "a", "answer": ""}]}, "invalid_history"),
        ({"question": "q", "history": [{"question": "a", "answer": "b", "x": 1}]},
         "invalid_history"),
        ({"question": "q", "history": [{"question": "a" * (MAX_QUESTION_CHARS + 1),
                                        "answer": "b"}]}, "history_turn_too_long"),
        ({"question": "q", "history": [{"question": "a",
                                        "answer": "b" * (MAX_HISTORY_ANSWER_CHARS + 1)}]},
         "history_turn_too_long"),
        ({"question": "q", "no_loop": True, "history": [{"question": "a", "answer": "b"}]},
         "history_requires_loop"),
    ],
)
def test_a_bad_request_is_a_400_and_queues_nothing(
    test_db: psycopg.Connection[Any], tmp_path: Path, payload: dict[str, Any], code: str
) -> None:
    ctx = _ctx(test_db, tmp_path)
    response = _ask(_client(ctx), payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert ctx.jobs is not None and ctx.jobs.run_next() is False


class _PlanRecorder:
    """Records the plan prompt; answers every step."""

    def __init__(self) -> None:
        self.plans: list[str] = []
        self.others: list[str] = []

    def __call__(self, prompt: str, *, schema: dict[str, Any], **_: Any) -> dict[str, Any]:
        if "sub_queries" in schema:
            self.plans.append(prompt)
            return {"sub_queries": ["quillbeam"]}
        self.others.append(prompt)
        if "sufficient" in schema:
            return {"sufficient": True, "follow_up_queries": []}
        return {"answer": "None."}


def _turns(count: int, answer_chars: int) -> list[dict[str, str]]:
    return [
        {"question": f"quillbeam question {n}", "answer": f"answer{n} " + "z" * answer_chars}
        for n in range(1, count + 1)
    ]


def test_the_server_caps_a_follow_ups_history_oldest_first(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    # The route test the ticket names. Five turns arrive; the server keeps the
    # newest three, then drops the oldest until 2,000 tokens fit (FakeEmbedder
    # counts four characters a token, so two 3,000-character answers fit, three do
    # not). The client is not trusted to have done either.
    chat = _PlanRecorder()
    ctx = _ctx(test_db, tmp_path, chat=chat)
    client = _client(ctx)

    snap = _ask(client, {"question": "And then?", "history": _turns(5, 3_000)})
    assert snap.status_code == 202
    assert ctx.jobs is not None and ctx.jobs.run_next()

    assert client.get(f"/api/jobs/{snap.json()['id']}").json()["state"] == "succeeded"
    (plan,) = chat.plans
    assert "Earlier in this conversation:" in plan
    for kept in (4, 5):
        assert f"Q: quillbeam question {kept}\n" in plan
    for dropped in (1, 2, 3):
        assert f"quillbeam question {dropped}\n" not in plan
    # RM-18c.D1: no later prompt sees any of it.
    assert chat.others
    assert not any("quillbeam question" in prompt for prompt in chat.others)


def test_a_fresh_ask_has_no_history_in_its_plan(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    chat = _PlanRecorder()
    ctx = _ctx(test_db, tmp_path, chat=chat)

    assert _ask(_client(ctx), {"question": "q"}).status_code == 202
    assert ctx.jobs is not None and ctx.jobs.run_next()

    (plan,) = chat.plans
    assert "Earlier in this conversation:" not in plan


def test_an_empty_history_is_no_history_even_with_no_loop(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path, chat=_Chat([{"answer": "None."}]))
    client = _client(ctx)

    snap = _ask(client, {"question": "q", "no_loop": True, "history": []})

    assert snap.status_code == 202
    assert ctx.jobs is not None and ctx.jobs.run_next()
    assert client.get(f"/api/jobs/{snap.json()['id']}").json()["state"] == "succeeded"


def test_three_maximal_turns_fit_under_the_wire_cap(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    # Worst case on the wire: every character an astral one, escaped as two \uXXXX.
    astral = "\U0001f600"
    turn = {"question": astral * MAX_QUESTION_CHARS, "answer": astral * MAX_HISTORY_ANSWER_CHARS}
    body = {"question": astral * MAX_QUESTION_CHARS, "history": [turn] * MAX_HISTORY_TURNS}

    response = _ask(_client(_ctx(test_db, tmp_path)), body)

    assert response.status_code == 202


def test_a_lone_surrogate_is_a_400(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    # Valid JSON, invalid UTF-8: refused at submit, never a failed job.
    response = _client(_ctx(test_db, tmp_path)).post(
        "/api/ask", content=b'{"question": "\\ud800"}',
        headers={**WRITE_HEADERS, "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_question"


def test_a_question_of_exactly_the_cap_is_accepted(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    response = _ask(_client(_ctx(test_db, tmp_path)), {"question": "x" * MAX_QUESTION_CHARS})

    assert response.status_code == 202


def test_a_body_over_the_wire_cap_is_a_413(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    from brain.ui.routes_ask import MAX_ASK_WIRE_BYTES

    response = _client(_ctx(test_db, tmp_path)).post(
        "/api/ask",
        content=b'{"question": "' + b"x" * MAX_ASK_WIRE_BYTES + b'"}',
        headers={**WRITE_HEADERS, "Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_read_only_is_refused_by_the_middleware(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    response = _ask(_client(_ctx(test_db, tmp_path, read_only=True)), {"question": "q"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "read_only"


def test_a_non_loopback_bind_is_refused(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    ctx = _ctx(test_db, tmp_path, ingest_enabled=False)

    response = _ask(_client(ctx), {"question": "q"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ask_requires_loopback"
    assert ctx.jobs is not None and ctx.jobs.run_next() is False


@pytest.mark.parametrize("missing", ["ask_runtime", "jobs", "ingest_runtime"])
def test_a_missing_runtime_is_a_503(
    test_db: psycopg.Connection[Any], tmp_path: Path, missing: str
) -> None:
    response = _ask(_client(_ctx(test_db, tmp_path, **{missing: None})), {"question": "q"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "jobs_unavailable"


def test_a_queued_ask_can_be_cancelled_through_the_jobs_route(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    client = _client(_ctx(test_db, tmp_path))
    snap = _ask(client, {"question": "q"}).json()

    response = client.post(f"/api/jobs/{snap['id']}/cancel", json={}, headers=WRITE_HEADERS)

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


# ------------------------------------------------------ the confidential lens --


@pytest.mark.parametrize("serve_bodies", [True, False])
def test_a_confidential_document_is_cited_only_when_bodies_are_served(
    test_db: psycopg.Connection[Any], tmp_path: Path, serve_bodies: bool
) -> None:
    sealed = _seed(test_db, "Sealed memo", "confidential")
    ctx = _ctx(
        test_db, tmp_path, chat=_Chat([{"answer": "It says so [1]."}]),
        serve_confidential_bodies=serve_bodies,
    )
    client = _client(ctx)

    snap = _ask(client, {"question": "quillbeam", "no_loop": True}).json()
    assert ctx.jobs is not None and ctx.jobs.run_next()
    result = client.get(f"/api/jobs/{snap['id']}").json()["result"]

    cited = [c["document_id"] for c in result["citations"]]
    if serve_bodies:
        assert cited == [sealed]
        assert result["citations"][0]["title"] == "Sealed memo"
        # The STORED level, read back (aud-79 A1): never assumed "normal".
        assert result["citations"][0]["sensitivity"] == "confidential"
        assert result["citations"][0]["withheld"] is False
    else:
        assert cited == []
        assert "Sealed memo" not in str(result)


def _crafted_ask(registry: JobRegistry, sensitivity: str | None) -> str:
    """An ask job whose result cites one document at ``sensitivity`` (None: unknown)."""
    citation = {
        "ref": 1, "document_id": "0" * 31 + "9", "title": "Sealed memo",
        "source_kind": "manual", "snippet": "sealed words", "sensitivity": sensitivity,
    }

    def runner(handle: JobHandle) -> Mapping[str, Any]:
        return {"answer": "Sealed words [1].", "citations": [citation],
                "sub_queries": ["sealed"], "fallback_used": False}

    snap = registry.submit(ASK_KIND, runner)
    registry.run_next()
    return snap.id


@pytest.mark.parametrize("sensitivity", ["confidential", None])
def test_the_lens_withholds_a_non_normal_citation_when_bodies_are_not_served(
    test_db: psycopg.Connection[Any], tmp_path: Path, sensitivity: str | None
) -> None:
    registry = make_registry()
    job_id = _crafted_ask(registry, sensitivity)
    client = _client(_ctx(test_db, tmp_path, registry=registry, serve_confidential_bodies=False))

    result = client.get(f"/api/jobs/{job_id}").json()["result"]

    assert "Sealed" not in str(result) and "sealed" not in str(result)
    (citation,) = result["citations"]
    assert citation["withheld"] is True
    assert (citation["document_id"], citation["title"], citation["snippet"]) == (None,) * 3
    assert (result["answer"], result["sub_queries"]) == (None, None)
    assert result["title_withheld"] is True


def test_the_lens_shows_every_citation_when_bodies_are_served(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    registry = make_registry()
    job_id = _crafted_ask(registry, "confidential")
    client = _client(_ctx(test_db, tmp_path, registry=registry, serve_confidential_bodies=True))

    result = client.get(f"/api/jobs/{job_id}").json()["result"]

    assert result["citations"][0]["title"] == "Sealed memo"
    assert result["citations"][0]["withheld"] is False
    assert result["answer"] == "Sealed words [1]."
    assert result["title_withheld"] is False


def test_the_lens_keeps_a_normal_citation_when_bodies_are_not_served() -> None:
    result = {"answer": "a [1]", "sub_queries": ("a",), "citations": (
        {"ref": 1, "document_id": "d", "title": "Open", "snippet": "s",
         "sensitivity": "normal"},)}

    lensed = lens_ask_result(result, serve_confidential_bodies=False)

    assert lensed is not None
    assert lensed["citations"][0]["title"] == "Open"
    assert lensed["answer"] == "a [1]"
    assert lensed["title_withheld"] is False


def test_the_lens_passes_none_through() -> None:
    assert lens_ask_result(None, serve_confidential_bodies=False) is None


# --------------------------------------------------- listing and retention --


def test_ask_jobs_are_left_out_of_the_job_list(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    ctx = _ctx(test_db, tmp_path)
    client = _client(ctx)
    ask_id = _ask(client, {"question": "q"}).json()["id"]
    paste = client.post(
        "/api/ingest/paste", json={"title": "T", "body": "b", "enrich": False},
        headers=WRITE_HEADERS,
    ).json()

    listed = [j["id"] for j in client.get("/api/jobs").json()["jobs"]]

    assert listed == [paste["id"]]
    assert client.get(f"/api/jobs/{ask_id}").status_code == 200  # readable by id


def test_an_ask_is_gone_by_id_after_its_retention_while_ingest_stays(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    clock = _Clock()
    registry = make_registry(clock=clock, retain_for_by_kind={ASK_KIND: ASK_RETAIN_FOR})
    ctx = _ctx(test_db, tmp_path, chat=_Chat([{"answer": "None."}]), registry=registry)
    client = _client(ctx)
    ask_id = _ask(client, {"question": "q", "no_loop": True}).json()["id"]
    registry.run_next()
    other = registry.submit("ingest.paste", lambda handle: {})
    registry.run_next()

    clock.now += ASK_RETAIN_FOR - timedelta(seconds=1)
    assert client.get(f"/api/jobs/{ask_id}").status_code == 200
    clock.now += timedelta(seconds=2)

    gone = client.get(f"/api/jobs/{ask_id}")
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "job_unknown"
    assert client.get(f"/api/jobs/{other.id}").status_code == 200


@pytest.mark.parametrize(("host", "enabled"), [("127.0.0.1", True), ("192.0.2.10", False)])
def test_build_context_wires_the_ask_runtime_and_its_retention(
    test_db: psycopg.Connection[Any], host: str, enabled: bool
) -> None:
    from brain.chat import chat_json
    from brain.config import Config
    from brain.ui.ask_service import ask_unavailable_reason
    from brain.ui.ingest_limits import RETAIN_FOR
    from brain.ui.server import build_context
    from tests.conftest import TEST_DATABASE_URL

    context = build_context(
        Config(database_url=TEST_DATABASE_URL, embedder="none"),
        host=host, port=8765, read_only=False,
        token="" if enabled else "tok-123", include_confidential=False, embedder=None,
    )

    assert context.ask_runtime is not None
    assert context.ask_runtime.chat is chat_json
    assert context.jobs is not None
    assert context.jobs.retention(ASK_KIND) == timedelta(minutes=10)
    assert context.jobs.retention("ingest.paste") == RETAIN_FOR
    assert (ask_unavailable_reason(context) is None) is enabled


def test_the_offered_mode_is_asks_hybrid_mode() -> None:
    # ask_service spells it to keep brain.ask (and httpx) out of brain.ui.app's imports.
    from brain.ask import HYBRID_MODE
    from brain.ui.ask_service import ASK_MODES

    assert (HYBRID_MODE,) == ASK_MODES
