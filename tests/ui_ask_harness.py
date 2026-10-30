"""The shared harness for the Agent tab's browser suite (RM-18b).

Used by ``tests/test_ui_browser_agent.py``. Same shape as
``tests/ui_ingest_harness.py``, whose ``static_origin`` and ``browser`` fixtures it
reuses: a plain static server for the real ``static/`` tree, and every ``/api/**``
call answered here by ``page.route``. Not a test module, and it carries no marker.

HERMETIC, as the ticket requires: ``/api/ask/config``, ``/api/ask``, ``/api/jobs``
and ``/api/jobs/{id}`` are a small scripted state machine (:class:`FakeAsk`). Its
payloads carry exactly the keys ``routes_jobs.snapshot_payload`` and
``ask_service._payload`` (through ``routes_jobs.lens_ask_result``) emit;
``tests/test_ui_ask_wire_contract.py`` proves that against the real server on the
5434 database, by comparing the real payload's keys with :data:`SNAPSHOT_KEYS`,
:data:`RESULT_KEYS` and :data:`CITATION_KEYS` below. So the stub cannot drift from
the wire without a test going red.

The scenario is chosen by a ``scenario:<name>`` marker in the QUESTION. A job
advances one frame per poll (the client polls once a second); assertions wait on
the DOM, never on a sleep.

No PII: nonsense words and synthetic UUIDs.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.ui_ingest_harness import ENABLED_CONFIG as INGEST_CONFIG

SETTLE_MS = 15_000
_NETWORK_NOISE = "Failed to load resource"
STAMP = "2026-03-01T10:00:00+00:00"

#: Synthetic, canonical (as Postgres prints a uuid).
DOC_ID = "3f2a9c1e-0b7d-4e5f-8a6b-1c2d3e4f5a6b"
OTHER_ID = "7c6b5a4f-3e2d-4c1b-9a8f-0e1d2c3b4a59"

SNAPSHOT_KEYS = frozenset({
    "id", "kind", "state", "cancellable", "cancel_requested", "created_at",
    "started_at", "finished_at", "error", "result", "events",
})
RESULT_KEYS = frozenset({
    "answer", "citations", "iterations_used", "sub_queries", "fallback_used",
    "session_id", "fallback", "fallback_reason", "fallback_transient", "fallback_note",
    "elapsed_ms", "title_withheld",
})
#: What ``GET /api/jobs`` gives per job (``snapshot_payload(..., listing=True)``):
#: no result and no events. Proven against the real route by the wire-contract test.
LISTED_KEYS = frozenset({
    "id", "kind", "state", "cancellable", "cancel_requested", "created_at",
    "started_at", "finished_at", "error",
})
CITATION_KEYS = frozenset({
    "ref", "document_id", "title", "source_kind", "snippet", "sensitivity", "withheld",
})

ENABLED: dict[str, Any] = {
    "enabled": True, "reason": None, "max_question_chars": 2000, "max_history_turns": 3,
    "modes": ["hybrid"],
}
#: The keys ``GET /api/ask/config`` emits, proven against the real route by
#: ``tests/test_ui_ask_wire_contract.py``.
CONFIG_KEYS = frozenset(ENABLED)

#: Every hostile string the tab must show as text. Each would raise a dialog, or
#: create an element, if it ever reached an HTML parser.
HOSTILE_ANSWER = (
    'Start <img src=x onerror="alert(1)"> then <script>alert(2)</script> and '
    '<a href="javascript:alert(3)">click</a> javascript:alert(4) cites [1], not [99], '
    "not [2], not [3] and not [4] either."
)
HOSTILE_SUBQUERY = '<img src=x onerror="alert(5)">'
HOSTILE_TITLE = '<b>Bold</b> <img src=x onerror="alert(6)">'
HOSTILE_SNIPPET = "<script>alert(7)</script>"
HOSTILE_NOTE = '<img src=x onerror="alert(8)">'
HOSTILE_ERROR = 'The chat model did not answer <img src=x onerror="alert(9)">'


#: The ask's own submit time is :data:`STAMP`; an ingest listed with one of these
#: was queued before it (ahead) or after it (behind, FIFO).
BEFORE = "2026-03-01T09:59:00+00:00"
AFTER = "2026-03-01T10:00:05+00:00"


def listed_job(state: str, created_at: str, kind: str = "ingest.paste") -> dict[str, Any]:
    """One ``GET /api/jobs`` entry, with every key the real listing emits."""
    return {
        "id": "f" * 32, "kind": kind, "state": state,
        "cancellable": state in ("queued", "running"), "cancel_requested": False,
        "created_at": created_at, "started_at": None, "finished_at": None, "error": None,
    }


def citation(ref: int, document_id: str | None, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ref": ref, "document_id": document_id, "title": "Quarkle ledger",
        "source_kind": "manual", "snippet": "quarkle ships on fridays",
        "sensitivity": "normal", "withheld": False,
    }
    return {**base, **overrides}


def result(**overrides: Any) -> dict[str, Any]:
    """A lensed ask result with every key the server emits."""
    base: dict[str, Any] = {
        "answer": "Quarkle ships on Fridays [1].",
        "citations": [citation(1, DOC_ID)],
        "iterations_used": 1,
        "sub_queries": ["quarkle schedule"],
        "fallback_used": False,
        "session_id": "5b1e2d3c-4a59-4687-9a0b-1c2d3e4f5061",
        "fallback": None, "fallback_reason": None, "fallback_transient": None,
        "fallback_note": None,
        "elapsed_ms": 4200,
        "title_withheld": False,
    }
    return {**base, **overrides}


@dataclass
class Frame:
    state: str
    steps: list[tuple[str, int, int]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    cancellable: bool = True


def _done(res: dict[str, Any]) -> Frame:
    return Frame("succeeded", [], res, cancellable=False)


SCRIPTS: dict[str, list[Frame]] = {
    "answer": [
        Frame("running", [("plan", 1, 3)]),
        Frame("running", [("retrieve", 1, 3)]),
        Frame("running", [("reflect", 1, 3)]),
        Frame("running", [("synthesize", 1, 3)]),
        _done(result(iterations_used=1)),
    ],
    "hostile": [_done(result(
        answer=HOSTILE_ANSWER,
        citations=[
            citation(1, DOC_ID, title=HOSTILE_TITLE, snippet=HOSTILE_SNIPPET,
                     source_kind="<i>manual</i>"),
            # In the list, but its id is not a UUID: it must never become a link.
            citation(2, "javascript:alert(10)"),
            # A UUID with something riding along: the check is ANCHORED.
            citation(3, f'{OTHER_ID}?x=" onmouseover="alert(12)'),
            # Withheld, yet still carrying an id: the client honours the flag
            # itself rather than relying on the server's lens having nulled the id.
            citation(4, OTHER_ID, withheld=True, title=None, snippet=None,
                     sensitivity="confidential"),
        ],
        sub_queries=[HOSTILE_SUBQUERY, "<script>alert(11)</script>"],
        fallback="fts", fallback_reason="embed_failed", fallback_transient=True,
        fallback_note=HOSTILE_NOTE,
    ))],
    "withheld": [_done(result(
        answer=None, sub_queries=None, title_withheld=True,
        citations=[citation(1, None, title=None, snippet=None,
                            sensitivity=None, withheld=True)],
    ))],
    "fails": [Frame("failed", [], None, {"code": "chat_unavailable", "message": HOSTILE_ERROR},
                    cancellable=False)],
    "hold": [Frame("running", [("plan", 1, 3)]), Frame("running", [("retrieve", 1, 3)])],
    # Phases that are names on Object.prototype: never a step.
    "proto": [Frame("running", [("toString", 1, 3), ("constructor", 1, 3),
                                ("__proto__", 1, 3), ("plan", 1, 3)]),
              _done(result())],
    "queue": [Frame("queued")],
    # Settles on its first poll: the follow-up rows chain several asks.
    "quick": [_done(result(answer="Plonk ships quarkle [1]."))],
}


@dataclass
class FakeJob:
    id: str
    script: list[Frame]
    polls: int = 0
    seq: int = 1
    events: list[dict[str, Any]] = field(default_factory=list)
    frame: Frame = field(default_factory=lambda: Frame("queued"))
    cancelled: bool = False


class FakeAsk:
    """``/api/ask*`` and ``/api/jobs*``, scripted per test."""

    def __init__(self) -> None:
        self.config: dict[str, Any] = dict(ENABLED)
        self.jobs: dict[str, FakeJob] = {}
        self.asks: list[Any] = []
        self.cancels: list[tuple[str, Any]] = []
        #: What ``GET /api/jobs`` lists (ingest jobs only; asks are never listed).
        self.listed: list[dict[str, Any]] = []

    def snapshot(self, job: FakeJob, after: int = 0) -> dict[str, Any]:
        frame = job.frame
        return {
            "id": job.id, "kind": "ask", "state": frame.state,
            "cancellable": frame.cancellable and frame.state in ("queued", "running"),
            "cancel_requested": job.cancelled and frame.state == "running",
            "created_at": STAMP, "started_at": None, "finished_at": None,
            "error": frame.error, "result": frame.result,
            "events": [e for e in job.events if e["seq"] > after],
        }

    def _advance(self, job: FakeJob) -> None:
        if job.cancelled and job.frame.state in ("queued", "running"):
            job.frame = Frame("cancelled", cancellable=False)
            return
        job.frame = job.script[min(job.polls, len(job.script) - 1)]
        if job.polls < len(job.script):
            for phase, done, total in job.frame.steps:
                job.seq += 1
                job.events.append({"seq": job.seq, "at": STAMP, "phase": phase,
                                   "done": done, "total": total, "code": None})
        job.polls += 1

    def handle(self, route: Any) -> bool:
        request = route.request
        split = urlsplit(request.url)
        path = split.path
        if path == "/api/ask/config":
            _json(route, 200, self.config)
            return True
        if path == "/api/ingest/config":
            _json(route, 200, INGEST_CONFIG)
            return True
        if path == "/api/ask" and request.method == "POST":
            body = json.loads(request.post_data or "{}")
            self.asks.append(body)
            marker = re.search(r"scenario:([a-z]+)", str(body.get("question", "")))
            job = FakeJob(id=f"{len(self.jobs) + 1:032x}",
                          script=SCRIPTS[marker.group(1) if marker else "answer"])
            job.events.append({"seq": 1, "at": STAMP, "phase": "queued",
                               "done": None, "total": None, "code": None})
            self.jobs[job.id] = job
            _json(route, 202, self.snapshot(job))
            return True
        if path == "/api/jobs" and request.method == "GET":
            _json(route, 200, {"jobs": self.listed})
            return True
        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)(/cancel)?", path)
        if not match:
            return False
        job = self.jobs.get(match.group(1))
        if job is None:
            _json(route, 404, {"error": {"code": "job_unknown", "message": "no such job"}})
            return True
        if match.group(2):
            self.cancels.append((job.id, json.loads(request.post_data or "null")))
            if job.frame.state == "queued":
                job.frame = Frame("cancelled", cancellable=False)
                _json(route, 200, self.snapshot(job))
                return True
            job.cancelled = True
            _json(route, 202, {**self.snapshot(job), "cancel_requested": True})
            return True
        self._advance(job)
        after = int(parse_qs(split.query).get("after", ["0"])[0])
        _json(route, 200, self.snapshot(job, after))
        return True


def _json(route: Any, status: int, body: Any) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


_BOOT: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": [],
                    "ingest_enabled": True},
    "/api/tree": {"count": 0, "name": "", "path": "", "empty_hint": "none",
                  "children": [], "notes": []},
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
    f"/api/notes/{DOC_ID}": {
        "id": DOC_ID, "title": "Quarkle ledger", "tier": "ingested",
        "content_type": "note", "draft": False, "tags": [], "source_kind": "manual",
        "vault_path": "_ingested/manual/2026-03-01-quarkle-ledger.md",
        "ingested_at": STAMP, "editable": False, "movable": False,
        "body": "quarkle\n", "body_hash": "sha256:x", "html": "<p>quarkle</p>",
    },
}


@dataclass
class AskHarness:
    page: Any
    fake: FakeAsk
    errors: list[str]
    dialogs: list[str]
    requests: list[str]
    origin: str

    def open_tab(self) -> None:
        self.page.click('.tab[data-tab="agent"]')
        self.page.wait_for_selector("#agent-fieldset:not([disabled])")

    def ask(self, scenario: str, *, words: str = "quarkle zibbit", quick: bool = False) -> None:
        self.page.fill("#agent-question", f"scenario:{scenario} {words}")
        if quick:
            self.page.check("#agent-no-loop")
        self.page.click("#agent-submit")

    def turn(self, index: int = 0) -> Any:
        return self.page.locator("#agent-turns > li").nth(index)

    def settled(self, index: int = 0) -> Any:
        row = self.turn(index)
        row.locator('[data-terminal="true"]').wait_for(timeout=SETTLE_MS)
        return row

    def phases(self) -> list[str]:
        """Every text any turn's phase line has held, in order."""
        texts: list[str] = self.page.evaluate("() => window.__phases")
        return texts

    def announced(self) -> list[str]:
        texts: list[str] = self.page.evaluate("() => window.__announced")
        return [t for t in texts if t]


@pytest.fixture(name="ask_harness")
def _ask_harness_fixture(browser: Any, static_origin: str) -> Iterator[AskHarness]:
    page = browser.new_page()
    fake = FakeAsk()
    errors: list[str] = []
    dialogs: list[str] = []
    requests: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: (
        errors.append(f"console: {msg.text}")
        if msg.type == "error" and _NETWORK_NOISE not in msg.text else None
    ))

    def on_dialog(dialog: Any) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", on_dialog)

    def route_api(route: Any) -> None:
        split = urlsplit(route.request.url)
        requests.append(f"{route.request.method} {split.path}")
        if fake.handle(route):
            return
        body = _BOOT.get(split.path)
        _json(route, 200 if body is not None else 404, body if body is not None else {})

    page.route("**/api/**", route_api)
    page.add_init_script(
        "window.__announced = []; window.__phases = [];"
        "document.addEventListener('DOMContentLoaded', () => {"
        "  const region = document.getElementById('agent-announce');"
        "  new MutationObserver(() => window.__announced.push(region.textContent))"
        "    .observe(region, {childList: true, characterData: true, subtree: true});"
        "  new MutationObserver(() => { for (const p of document.querySelectorAll("
        "    '#agent-turns .turn-phase')) window.__phases.push(p.textContent); })"
        "    .observe(document.getElementById('agent-turns'),"
        "             {childList: true, characterData: true, subtree: true});"
        "});"
    )
    page.goto(f"{static_origin}/static/index.html")
    page.wait_for_selector("#tab-notes")
    try:
        yield AskHarness(page, fake, errors, dialogs, requests, static_origin)
    finally:
        page.close()
    assert errors == [], errors
    assert dialogs == [], dialogs
