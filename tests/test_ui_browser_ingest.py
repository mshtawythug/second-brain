"""The Ingest tab's paste mode, EXECUTED in a real browser (RM-11, Ingest spec §9.4).

Hermetic, like ``tests/test_ui_browser_search_fallback.py``: a plain static server
for the real ``static/`` tree, and every ``/api/**`` call answered here by
``page.route``. The job routes are a small scripted state machine (:class:`FakeJobs`)
whose payloads carry exactly the keys ``routes_jobs.snapshot_payload`` and
``ingest_service._result_payload`` emit, so the panel is driven by the RM-10 wire
shape without a server, a database or Ollama. The spec's one real-worker cancel
test is the manual end-to-end pass recorded in the plan, not this module.

A job advances ONE frame per poll, and the client polls once a second, so a
three-frame job settles in about three seconds. Assertions wait on the DOM, never
on a sleep.

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

pytestmark = pytest.mark.browser

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


#: Scenario, chosen by the pasted title: the frames each poll walks through.
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
            job = FakeJob(id=f"{len(self.jobs) + 1:032x}", script=SCRIPTS[body["title"]],
                          refuse_cancel=body["title"] == "race")
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


@pytest.fixture(scope="module")
def static_origin() -> Iterator[str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(static_dir().parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
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

    def paste(self, title: str, body: str = "quarkle zibbit flonk") -> None:
        self.page.fill("#ingest-title", title)
        self.page.fill("#ingest-body", body)
        self.page.click("#ingest-submit")

    def row(self, index: int = 0) -> Any:
        return self.page.locator("#ingest-jobs > li").nth(index)

    def settled(self, index: int = 0) -> Any:
        row = self.row(index)
        row.locator('[data-terminal="true"]').wait_for(timeout=SETTLE_MS)
        return row


@pytest.fixture
def harness(browser: Any, static_origin: str) -> Iterator[Harness]:
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
    page.goto(f"{static_origin}/static/index.html")
    page.wait_for_selector("#tab-notes")
    try:
        yield Harness(page, jobs, errors, static_origin, requests)
    finally:
        page.close()
    assert errors == [], errors


# ------------------------------------------------------------------ the flows --


def test_a_paste_shows_progress_then_added_and_opens_the_note(harness: Harness) -> None:
    page = harness.page
    harness.open_tab()
    page.fill("#ingest-tags", "alpha, Beta Two")
    harness.paste("added")

    progress = harness.row().locator("progress")
    progress.wait_for(timeout=SETTLE_MS)
    page.wait_for_function(
        "() => { const p = document.querySelector('#ingest-jobs progress');"
        " return p && p.max === 2 && p.value === 1; }", timeout=SETTLE_MS)
    row = harness.settled()

    assert row.locator(".job-outcome").inner_text() == "Added"
    assert "Quarkle digest" in row.inner_text()
    assert "_ingested/manual/2026-03-01-quarkle-digest.md" in row.inner_text()
    link = row.locator("a.job-open")
    assert link.get_attribute("href") == f"/?id={DOC_ID}"
    assert harness.jobs.pastes == [{
        "title": "added", "body": "quarkle zibbit flonk", "tags": ["alpha", "Beta Two"],
        "content_type": "note", "sensitivity": "normal", "enrich": True,
    }]

    link.click()
    page.wait_for_selector("#tab-notes:not([hidden])")
    assert page.locator("#tab-ingest").is_hidden()
    page.wait_for_function(
        "() => document.querySelector('.note-title')"
        " && document.querySelector('.note-title').textContent === 'Quarkle digest'",
        timeout=SETTLE_MS)
    assert "id=" + DOC_ID in page.url


def test_a_repeat_paste_says_already_in_your_brain(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("again")
    row = harness.settled()

    assert row.locator(".job-outcome").inner_text() == "Already in your brain"
    assert row.locator("a.job-open").get_attribute("href") == f"/?id={DOC_ID}"


def test_mirror_repaired_and_empty_outcomes(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("repaired")
    harness.settled()
    harness.paste("empty")
    empty = harness.settled(0)
    repaired = harness.settled(1)

    assert repaired.locator(".job-outcome").inner_text() == (
        "Already in your brain. Its vault file was missing and has been rewritten.")
    assert empty.locator(".job-outcome").inner_text() == (
        "Nothing to index: no text could be extracted.")
    assert empty.locator("a.job-open").count() == 0


def test_the_notices_render_and_server_strings_stay_text(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("notices")
    row = harness.settled()
    text = row.inner_text()

    assert "<b>Bold</b> title" in text
    assert "<b>API key</b>" in text and "sk-…<i>x</i>" in text
    assert "line 3" in text
    assert "Stored with the matches redacted." in text
    assert "findable by full-text search only" in text
    assert "brain enrich --backfill" in text
    assert "brain graphrag build --backfill" in text
    assert row.locator("b, i").count() == 0


def test_a_withheld_result_has_no_link_and_never_the_typed_title(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("withheld")
    row = harness.settled()

    assert "title is withheld" in row.inner_text()
    assert row.locator("a").count() == 0
    # The typed title was "withheld"; the panel must not put it back as a title.
    assert row.locator(".job-title").count() == 0


def test_cancel_a_running_job(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("hold")
    cancel = harness.row().locator("button.job-cancel")
    cancel.wait_for(timeout=SETTLE_MS)
    assert cancel.get_attribute("aria-describedby")

    cancel.click()
    row = harness.settled()

    assert row.locator(".job-outcome").inner_text() == "Cancelled. Nothing was written."
    assert harness.jobs.cancels == [(next(iter(harness.jobs.jobs)), {})]
    assert row.locator("button.job-cancel").count() == 0


def test_a_refused_cancel_shows_the_servers_reason(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("race")
    cancel = harness.row().locator("button.job-cancel")
    cancel.wait_for(timeout=SETTLE_MS)

    cancel.click()
    note = harness.row().locator(".job-note", has_text="writing has started")
    note.wait_for(timeout=SETTLE_MS)

    assert note.inner_text() == "writing has started; the job will finish"
    assert harness.row().locator("button.job-cancel").count() == 0


def test_a_cancel_too_late_is_reported(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("too-late")
    row = harness.settled()

    assert row.locator(".job-outcome").inner_text() == "Added"
    assert "finished before it could stop" in row.inner_text()


def test_a_failed_job_shows_the_fixed_message(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("fails")
    row = harness.settled()

    assert row.locator(".job-outcome").inner_text() == "Failed"
    assert ("The embedding model did not answer. Nothing was written; retry."
            in row.inner_text())


def test_a_job_the_server_lost_says_the_outcome_is_unknown(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("hold")
    harness.row().locator("button.job-cancel").wait_for(timeout=SETTLE_MS)
    harness.jobs.gone.update(harness.jobs.jobs)  # the server restarted
    harness.page.reload()
    harness.page.wait_for_selector("#tab-notes")
    harness.open_tab()

    row = harness.settled()
    assert "outcome is unknown" in row.inner_text()
    assert "Re-submitting is safe" in row.inner_text()
    remembered = harness.page.evaluate("() => sessionStorage.getItem('brain-ui-jobs')")
    assert remembered in (None, "[]")


@pytest.mark.parametrize(("reason", "sentence"), [
    ("read_only", "This server is read-only."),
    ("requires_loopback", "only when brain ui is bound to this machine"),
])
def test_a_server_that_cannot_ingest_renders_the_tab_disabled(
    harness: Harness, reason: str, sentence: str
) -> None:
    harness.jobs.config = {**ENABLED_CONFIG, "enabled": False, "reason": reason}
    harness.page.click('.tab[data-tab="ingest"]')
    notice = harness.page.locator("#ingest-unavailable")
    notice.wait_for(timeout=SETTLE_MS)

    assert sentence in notice.inner_text()
    # The fieldset disables every control inside it; check the ones a user uses.
    for control in ("#ingest-title", "#ingest-body", "#ingest-submit"):
        assert harness.page.locator(control).is_disabled(), control


def test_an_oversize_body_is_refused_before_any_request(harness: Harness) -> None:
    harness.jobs.config = {**ENABLED_CONFIG,
                           "limits": {"paste_body_bytes": 10, "max_queued_jobs": 4}}
    harness.open_tab()
    harness.paste("added", body="ééééééé")  # 7 characters, 14 UTF-8 bytes

    error = harness.page.locator("#ingest-error")
    error.wait_for()
    assert "14 bytes" in error.inner_text() and "10 bytes" in error.inner_text()
    assert harness.jobs.pastes == []


def test_a_server_refusal_keeps_the_form(harness: Harness) -> None:
    harness.open_tab()
    harness.page.route("**/api/ingest/paste", lambda route: _json(route, 429, {"error": {
        "code": "queue_full", "message": "at most 4 jobs may wait; retry when one finishes"}}))
    harness.paste("added")

    harness.page.locator("#ingest-error").wait_for()
    assert "at most 4 jobs may wait" in harness.page.locator("#ingest-error").inner_text()
    assert harness.page.input_value("#ingest-body") == "quarkle zibbit flonk"


def test_the_whole_paste_works_from_the_keyboard(harness: Harness) -> None:
    page = harness.page
    page.focus('.tab[data-tab="ingest"]')
    page.keyboard.press("Enter")
    page.wait_for_selector("#ingest-fieldset:not([disabled])")

    order: list[str] = []
    for _ in range(12):
        page.keyboard.press("Tab")
        focused = page.evaluate("() => document.activeElement.id")
        if focused and focused.startswith("ingest-"):
            order.append(focused)
        if focused == "ingest-submit":
            break
    assert order == ["ingest-title", "ingest-body", "ingest-tags", "ingest-content-type",
                     "ingest-sensitivity-normal", "ingest-no-enrich", "ingest-submit"]

    page.focus("#ingest-title")
    page.keyboard.type("added")
    page.keyboard.press("Tab")
    page.keyboard.type("quarkle zibbit flonk")
    page.focus("#ingest-submit")
    page.keyboard.press("Enter")
    harness.settled()
    assert harness.jobs.pastes and harness.jobs.pastes[0]["title"] == "added"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_panel_renders_in_both_themes(harness: Harness, theme: str) -> None:
    page = harness.page
    page.evaluate(f"() => document.documentElement.setAttribute('data-theme', '{theme}')")
    harness.open_tab()
    harness.paste("added")
    row = harness.settled()

    ink, ground = page.evaluate(
        "() => { const s = getComputedStyle(document.querySelector('#tab-ingest'));"
        " return [s.color, s.backgroundColor]; }")
    assert ink != ground
    assert row.locator(".job-outcome").is_visible()


def test_poll_delay_backs_off(harness: Harness) -> None:
    delays = harness.page.evaluate(
        "async () => { const m = await import('/static/js/jobs.js');"
        " return [m.pollDelay(0, false), m.pollDelay(31000, false), m.pollDelay(0, true)]; }")

    assert delays == [1000, 3000, 3000]


def test_opening_the_tab_twice_loads_the_config_once(harness: Harness) -> None:
    harness.open_tab()
    harness.page.click('.tab[data-tab="notes"]')
    harness.page.click('.tab[data-tab="ingest"]')

    assert harness.requests.count("GET /api/ingest/config") == 1
