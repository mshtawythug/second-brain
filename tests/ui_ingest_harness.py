"""The shared harness for the Ingest tab's browser suites (RM-11, Ingest spec §9.4).

Used by ``tests/test_ui_browser_ingest.py`` (the flows) and
``tests/test_ui_browser_ingest_lifecycle.py`` (races, resumption, announcements),
split in two so neither crosses the 800-line test-file precedent. Same shape as
``tests/ui_graph_harness.py``: the fixtures are registered under their public
names and imported by each module. Not a test module, and it carries no marker.


Hermetic, like ``tests/test_ui_browser_search_fallback.py``: a plain static server
for the real ``static/`` tree, and every ``/api/**`` call answered here by
``page.route``. The job routes are a small scripted state machine (:class:`FakeJobs`)
whose payloads carry exactly the keys ``routes_jobs.snapshot_payload`` and
``ingest_service._result_payload`` emit, so the panel is driven by the RM-10 wire
shape without a server, a database or Ollama. The spec's one real-worker cancel
test is the manual end-to-end pass recorded in the plan, not this module.

A job advances ONE frame per poll, and the client polls once a second, so a
three-frame job settles in about three seconds. Assertions wait on the DOM, never
on a sleep; the few that prove an ABSENCE (no further poll, no re-announcement)
wait a bounded few poll intervals and say so.

**The scenario is chosen by a marker in the BODY** (``scenario:<name>``), never by
the title, so a test can type any title, including one it then proves the panel
never shows.

No PII: nonsense titles and synthetic ids.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from brain.ui.app import static_dir

#: Long enough for a scripted job of a few frames at one poll a second.
SETTLE_MS = 12_000

#: Chromium logs every non-2xx fetch as a console error; that is the network.
_NETWORK_NOISE = "Failed to load resource"

_BOOT: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": [],
                    "ingest_enabled": True},
    "/api/tree": {"count": 0, "name": "", "path": "", "empty_hint": "none",
                  "children": [], "notes": []},
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
}

ENABLED_CONFIG: dict[str, Any] = {
    "enabled": True,
    "reason": None,
    "extensions": [],
    "limits": {"paste_body_bytes": 2 * 1024 * 1024, "max_queued_jobs": 4},
    "content_types": ["note", "transcript"],
    "default_content_type": "note",
    "sensitivity_levels": ["confidential", "normal"],
    "default_sensitivity": "normal",
}

DOC_ID = "0" * 31 + "7"
STAMP = "2026-03-01T10:00:00+00:00"


def _result(**overrides: Any) -> dict[str, Any]:
    """A success payload with every key ``_result_payload`` and the lens emit."""
    base: dict[str, Any] = {
        "outcome": "ingested",
        "mirror_repaired": False,
        "document_id": DOC_ID,
        "title": "Quarkle digest",
        "vault_path": "_ingested/manual/2026-03-01-quarkle-digest.md",
        "chunk_count": 2,
        "sensitivity": "normal",
        "summary_status": "written",
        "secret_guard": "warn",
        "secret_findings": [],
        "egress_notice": "",
        "graph": "disabled",
        "possible_duplicates": [],
        "title_withheld": False,
    }
    return {**base, **overrides}


@dataclass
class Frame:
    """What the job looks like at one poll: its state, new events, and outcome."""

    state: str
    events: list[tuple[str, int | None, int | None, str | None]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    cancellable: bool = True


def _running(done: int, total: int) -> Frame:
    return Frame("running", [("embedding", done, total, None)])


#: Scenario, chosen by the ``scenario:<name>`` marker in the pasted BODY (never the
#: title): the frames each poll walks through.
SCRIPTS: dict[str, list[Frame]] = {
    "added": [_running(1, 2), Frame("succeeded", [("finishing", None, None, None)],
                                    _result(), cancellable=False)],
    "again": [Frame("succeeded", [], _result(outcome="skipped", chunk_count=2),
                    cancellable=False)],
    "repaired": [Frame("succeeded", [], _result(outcome="mirror_repaired",
                                                mirror_repaired=True),
                       cancellable=False)],
    "empty": [Frame("succeeded", [], _result(outcome="empty", document_id=None,
                                             vault_path=None, chunk_count=0,
                                             summary_status="skipped"),
                    cancellable=False)],
    "notices": [Frame("succeeded", [("finishing", None, None, "secret_findings")], _result(
        secret_guard="redact",
        secret_findings=[{"kind": "api_key", "label": "<b>API key</b>", "line": 3,
                          "col_start": 5, "col_end": 25, "preview": "sk-…<i>x</i>"}],
        egress_notice="vetoed", summary_status="pending", graph="not_synced",
        title="<b>Bold</b> title",
    ), cancellable=False)],
    "withheld": [Frame("succeeded", [], _result(
        title=None, vault_path=None, document_id=None, sensitivity="confidential",
        title_withheld=True), cancellable=False)],
    "hold": [_running(1, 4)],
    # Never picked up: a queued job, cancelled with a 200.
    "queue": [Frame("queued")],
    # Embedding, then writing: the bar has had a value and must become indeterminate.
    "writing": [_running(1, 2), Frame("running", [("finishing", None, None, None)])],
    # Cancellable when polled, refused when cancelled: enter_commit() closed in between.
    "race": [_running(1, 4)],
    "too-late": [Frame("succeeded", [("finishing", None, None, "cancel_too_late")],
                       _result(), cancellable=False)],
    "fails": [Frame("failed", [], None, {
        "code": "embedder_busy",
        "message": "The embedding model did not answer. Nothing was written; retry.",
    }, cancellable=False)],
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
    refuse_cancel: bool = False


class FakeJobs:
    """``/api/ingest/*`` and ``/api/jobs/*``, scripted per test."""

    def __init__(self) -> None:
        self.config: dict[str, Any] = dict(ENABLED_CONFIG)
        self.jobs: dict[str, FakeJob] = {}
        self.pastes: list[dict[str, Any]] = []
        self.cancels: list[tuple[str, Any]] = []
        self.gone: set[str] = set()

    def snapshot(self, job: FakeJob, after: int = 0) -> dict[str, Any]:
        frame = job.frame
        return {
            "id": job.id, "kind": "ingest.paste", "state": frame.state,
            "cancellable": frame.cancellable and frame.state in ("queued", "running"),
            "cancel_requested": job.cancelled and frame.state == "running",
            "created_at": STAMP, "started_at": None, "finished_at": None,
            "error": frame.error,
            "result": frame.result,
            "events": [e for e in job.events if e["seq"] > after],
        }

    def _advance(self, job: FakeJob) -> None:
        if job.cancelled and job.frame.cancellable:
            job.frame = Frame("cancelled", cancellable=False)
            return
        job.frame = job.script[min(job.polls, len(job.script) - 1)]
        if job.polls < len(job.script):
            for phase, done, total, code in job.frame.events:
                job.seq += 1
                job.events.append({"seq": job.seq, "at": STAMP, "phase": phase,
                                   "done": done, "total": total, "code": code})
        job.polls += 1

    def handle(self, route: Any) -> bool:
        request = route.request
        split = urlsplit(request.url)
        path = split.path
        if path == "/api/ingest/config":
            _json(route, 200, self.config)
            return True
        if path == "/api/ingest/paste" and request.method == "POST":
            body = json.loads(request.post_data or "{}")
            self.pastes.append(body)
            marker = re.search(r"scenario:([a-z-]+)", body.get("body", ""))
            scenario = marker.group(1) if marker else "added"
            job = FakeJob(id=f"{len(self.jobs) + 1:032x}", script=SCRIPTS[scenario],
                          refuse_cancel=scenario == "race")
            job.events.append({"seq": 1, "at": STAMP, "phase": "queued",
                               "done": None, "total": None, "code": None})
            self.jobs[job.id] = job
            _json(route, 202, self.snapshot(job))
            return True
        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)(/cancel)?", path)
        if not match:
            return False
        job_id = match.group(1)
        if job_id in self.gone or job_id not in self.jobs:
            _json(route, 404, {"error": {
                "code": "job_unknown",
                "message": "no such job; its outcome is unknown. Re-submitting is safe "
                           "(identical content is a no-op)"}})
            return True
        job = self.jobs[job_id]
        if match.group(2):
            self.cancels.append((job_id, json.loads(request.post_data or "null")))
            if job.refuse_cancel or not job.frame.cancellable:
                _json(route, 409, {"error": {
                    "code": "not_cancellable",
                    "message": "writing has started; the job will finish"}})
                return True
            if job.frame.state == "queued":
                job.frame = Frame("cancelled", cancellable=False)
                _json(route, 200, self.snapshot(job))
                return True
            job.cancelled = True
            _json(route, 202, self.snapshot(job))
            return True
        self._advance(job)
        after = int(parse_qs(split.query).get("after", ["0"])[0])
        _json(route, 200, self.snapshot(job, after))
        return True


def _json(route: Any, status: int, body: Any) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


NOTE: dict[str, Any] = {
    "id": DOC_ID, "title": "Quarkle digest", "tier": "ingested",
    "content_type": "note", "draft": False, "tags": [],
    "source_kind": "manual", "vault_path": "_ingested/manual/2026-03-01-quarkle-digest.md",
    "ingested_at": STAMP, "editable": False, "movable": False,
    "body": "quarkle\n", "body_hash": "sha256:x", "html": "<p>quarkle</p>",
}


@pytest.fixture(scope="module", name="static_origin")
def _static_origin_fixture() -> Iterator[str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(static_dir().parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module", name="browser")
def _browser_fixture() -> Iterator[Any]:
    sync_api = pytest.importorskip(
        "playwright.sync_api",
        reason='Playwright not installed — `pip install -e ".[browser]"`',
    )
    with sync_api.sync_playwright() as play:
        instance = play.chromium.launch()
        try:
            yield instance
        finally:
            instance.close()


@dataclass
class Harness:
    page: Any
    jobs: FakeJobs
    errors: list[str]
    origin: str
    requests: list[str]

    def open_tab(self) -> None:
        self.page.click('.tab[data-tab="ingest"]')
        self.page.wait_for_selector("#ingest-fieldset:not([disabled])")

    def paste(self, scenario: str, *, title: str = "Synthetic paste",
              body: str | None = None) -> None:
        self.page.fill("#ingest-title", title)
        self.page.fill("#ingest-body",
                       body if body is not None else f"scenario:{scenario} quarkle zibbit flonk")
        self.page.click("#ingest-submit")

    def announced(self, *, raw: bool = False) -> list[str]:
        """Every text the live region has held, in order (``raw`` keeps the clears)."""
        texts: list[str] = self.page.evaluate("() => window.__announced")
        return texts if raw else [t for t in texts if t]

    def polls(self) -> int:
        return sum(1 for r in self.requests if r.startswith("GET /api/jobs/"))

    def hold(self, method: str) -> list[Any]:
        """Hold the NEXT ``/api/jobs/...`` request of ``method``; let every other through."""
        held: list[Any] = []

        def handler(route: Any) -> None:
            if route.request.method == method and not held:
                held.append(route)
                return
            route.fallback()

        self.page.route("**/api/jobs/**", handler)
        return held

    def wait_until(self, condition: Any, what: str) -> None:
        """A bounded wait on a condition only Python can see (a held route)."""
        for _ in range(SETTLE_MS // 100):
            if condition():
                return
            self.page.wait_for_timeout(100)
        raise AssertionError(f"timed out waiting for {what}")

    def row(self, index: int = 0) -> Any:
        return self.page.locator("#ingest-jobs > li").nth(index)

    def settled(self, index: int = 0) -> Any:
        row = self.row(index)
        row.locator('[data-terminal="true"]').wait_for(timeout=SETTLE_MS)
        return row


@pytest.fixture(name="harness")
def _harness_fixture(browser: Any, static_origin: str) -> Iterator[Harness]:
    page = browser.new_page()
    jobs = FakeJobs()
    errors: list[str] = []
    requests: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: (
        errors.append(f"console: {msg.text}")
        if msg.type == "error" and _NETWORK_NOISE not in msg.text else None
    ))

    def route_api(route: Any) -> None:
        split = urlsplit(route.request.url)
        requests.append(f"{route.request.method} {split.path}")
        if jobs.handle(route):
            return
        if split.path == f"/api/notes/{DOC_ID}":
            _json(route, 200, NOTE)
            return
        body = _BOOT.get(split.path)
        _json(route, 200 if body is not None else 404, body if body is not None else {})

    page.route("**/api/**", route_api)
    # Recorders: every text the live region holds, and every Cancel button
    # created (counted inside added rows too: a row is built before it is added).
    page.add_init_script(
        "window.__announced = []; window.__cancelAdds = 0;"
        "document.addEventListener('DOMContentLoaded', () => {"
        "  const region = document.getElementById('ingest-announce');"
        "  new MutationObserver(() => window.__announced.push(region.textContent))"
        "    .observe(region, {childList: true, characterData: true, subtree: true});"
        "  new MutationObserver((records) => { for (const r of records)"
        "    for (const n of r.addedNodes) { if (n.nodeType !== 1) continue;"
        "      window.__cancelAdds += (n.matches('button.job-cancel') ? 1 : 0)"
        "        + n.querySelectorAll('button.job-cancel').length; } })"
        "    .observe(document.getElementById('ingest-jobs'), {childList: true, subtree: true});"
        "});"
    )
    page.goto(f"{static_origin}/static/index.html")
    page.wait_for_selector("#tab-notes")
    try:
        yield Harness(page, jobs, errors, static_origin, requests)
    finally:
        page.close()
    assert errors == [], errors
