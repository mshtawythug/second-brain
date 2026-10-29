"""The Ingest tab's job lifecycle in a real browser: races, resumption, announcements.

RM-11 review round 1 (rev-74). Stale answers that must not un-settle a row, a
finished job the server evicted, jobs resumed after a reload, the live region,
reduced motion, and the server's title refusal. Hermetic: the harness and its
scripted fake are ``tests/ui_ingest_harness.py``.

No PII: nonsense titles and synthetic ids.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from tests.ui_ingest_harness import (
    SETTLE_MS,
    Harness,
    _browser_fixture,  # noqa: F401 — registers `browser`, which `harness` uses
    _harness_fixture,  # noqa: F401 — registers `harness`
    _json,
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`
)

pytestmark = pytest.mark.browser


# --------------------------------------------- review round 1 (rev-74) ------


def test_a_stale_poll_cannot_unsettle_a_cancelled_row(harness: Harness) -> None:
    """A poll sent before the cancel answers after it: the row stays cancelled."""
    harness.open_tab()
    harness.paste("queue")
    cancel = harness.row().locator("button.job-cancel")
    cancel.wait_for(timeout=SETTLE_MS)
    job = next(iter(harness.jobs.jobs.values()))
    stale = harness.jobs.snapshot(job)  # queued and cancellable, as it was
    held = harness.hold("GET")
    harness.wait_until(lambda: held, "the next poll")

    cancel.click()
    row = harness.settled()
    polls = harness.polls()
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps(stale))
    # An ABSENCE: a poller that did not stop would send its next poll within 1 s.
    harness.page.wait_for_timeout(2500)

    assert row.locator(".job-outcome").inner_text() == "Cancelled. Nothing was written."
    assert row.locator("button.job-cancel").count() == 0
    assert harness.announced().count("Paste job: Cancelled. Nothing was written.") == 1
    assert harness.polls() == polls


def test_a_stale_cancel_answer_cannot_unsettle_a_finished_row(harness: Harness) -> None:
    """The cancel's 202 is still in flight when the poll reports the job finished."""
    harness.open_tab()
    harness.paste("added")
    held = harness.hold("POST")
    cancel = harness.row().locator("button.job-cancel")
    cancel.wait_for(timeout=SETTLE_MS)
    cancel.click()
    harness.wait_until(lambda: held, "the cancel request")
    row = harness.settled()
    job = next(iter(harness.jobs.jobs.values()))
    stale = {**harness.jobs.snapshot(job), "state": "running", "cancellable": True,
             "cancel_requested": True, "result": None, "events": []}

    held[0].fulfill(status=202, content_type="application/json", body=json.dumps(stale))
    harness.page.wait_for_timeout(1500)  # an absence: no un-settling answer applied

    assert row.locator(".job-outcome").inner_text() == "Added"
    assert row.locator(".job-phase").inner_text() == ""
    assert row.locator("button.job-cancel").count() == 0
    assert harness.announced().count("Paste job running.") == 1


def test_a_requested_cancel_stays_requested(harness: Harness) -> None:
    """A poll sent before the 202 says cancel_requested false; Cancel is not offered again."""
    harness.open_tab()
    harness.paste("hold")
    harness.page.wait_for_selector("#ingest-jobs .job-phase:text('Embedding 1 of 4 chunks')",
                                   timeout=SETTLE_MS)
    job = next(iter(harness.jobs.jobs.values()))
    stale = harness.jobs.snapshot(job, after=10**6)  # running, cancel not yet requested
    held = harness.hold("GET")
    harness.wait_until(lambda: held, "the next poll")

    harness.row().locator("button.job-cancel").click()
    harness.page.wait_for_selector("#ingest-jobs .job-phase:text('Cancelling…')")
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps(stale))
    row = harness.settled()

    assert row.locator(".job-outcome").inner_text() == "Cancelled. Nothing was written."
    assert harness.page.evaluate("() => window.__cancelAdds") == 1


def test_an_evicted_finished_job_is_dropped_silently(harness: Harness) -> None:
    """A 404 for a job this tab saw finish is eviction (1 h / 50 jobs), not a lost outcome."""
    page = harness.page
    harness.open_tab()
    harness.paste("added")
    harness.settled()
    harness.jobs.gone.update(harness.jobs.jobs)  # evicted by the server
    polls = harness.polls()
    page.reload()
    page.wait_for_selector("#tab-notes")
    harness.open_tab()
    harness.wait_until(lambda: harness.polls() > polls, "the resumed poll")
    page.wait_for_function("() => document.querySelectorAll('#ingest-jobs > li').length === 0")

    assert page.locator("#ingest-jobs-empty").is_visible()
    assert "outcome is unknown" not in page.locator("#tab-ingest").inner_text()
    stored = page.evaluate(
        "() => [sessionStorage.getItem('brain-ui-jobs'),"
        " sessionStorage.getItem('brain-ui-jobs-settled')]")
    assert stored == ["[]", "[]"]
    assert harness.announced() == []


def test_resumed_jobs_are_not_announced(harness: Harness) -> None:
    page = harness.page
    harness.open_tab()
    harness.paste("added")
    harness.settled()
    page.reload()
    page.wait_for_selector("#tab-notes")
    harness.open_tab()
    row = harness.settled()

    assert row.locator(".job-outcome").inner_text() == "Added"
    assert harness.announced() == []


def test_identical_announcements_are_cleared_and_re_set(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("again")
    harness.settled()
    harness.paste("again", body="scenario:again another body")
    harness.page.wait_for_function(
        "() => window.__announced.filter((t) => t === 'Paste job: Already in your brain')"
        ".length === 2", timeout=SETTLE_MS)

    # The region starts empty, so the first sentence has nothing to clear; every
    # later one must be preceded by a clear.
    raw = harness.announced(raw=True)
    assert raw[0] != ""
    for index, text in enumerate(raw[1:], start=1):
        if text:
            assert raw[index - 1] == "", raw


def test_reduced_motion_stills_the_indeterminate_bar(harness: Harness) -> None:
    page = harness.page
    page.emulate_media(reduced_motion="reduce")
    harness.open_tab()
    harness.paste("writing")
    page.wait_for_selector("#ingest-jobs .job-phase:text('Writing')", timeout=SETTLE_MS)
    appearance = (
        "() => getComputedStyle(document.querySelector("
        "'#ingest-jobs progress.job-progress')).appearance"
    )

    assert page.evaluate(appearance) == "none"
    page.emulate_media(reduced_motion="no-preference")  # control: the rule is what did it
    assert page.evaluate(appearance) != "none"


def test_a_server_title_refusal_reaches_the_form(harness: Harness) -> None:
    """No maxlength in the markup: the server's limit and its 400 are the authority."""
    page = harness.page
    harness.open_tab()
    page.route("**/api/ingest/paste", lambda route: _json(route, 400, {"error": {
        "code": "invalid_title", "message": "title must be at most 200 characters"}}))
    long_title = "q" * 201
    harness.paste("added", title=long_title)

    page.locator("#ingest-error").wait_for()
    assert page.input_value("#ingest-title") == long_title
    assert page.locator("#ingest-error").inner_text() == "title must be at most 200 characters"


def test_the_writing_phase_shows_an_indeterminate_bar(harness: Harness) -> None:
    """After embedding, "Writing" has no total: the bar drops the value it had."""
    page = harness.page
    harness.open_tab()
    harness.paste("writing")
    page.wait_for_function(
        "() => { const p = document.querySelector('#ingest-jobs progress');"
        " return p && p.value === 1 && p.max === 2; }", timeout=SETTLE_MS)
    page.wait_for_selector("#ingest-jobs .job-phase:text('Writing')", timeout=SETTLE_MS)

    assert page.evaluate(
        "() => document.querySelector('#ingest-jobs progress').matches(':indeterminate')") is True


def test_an_unreachable_server_is_reported_and_polling_recovers(harness: Harness) -> None:
    page = harness.page
    harness.open_tab()
    failed: list[str] = []

    def one_failure(route: Any) -> None:
        if route.request.method == "GET" and not failed:
            failed.append(route.request.url)
            _json(route, 503, {"error": {"code": "unavailable", "message": "try later"}})
            return
        route.fallback()

    page.route("**/api/jobs/**", one_failure)
    harness.paste("added")
    note = harness.row().locator(".job-note", has_text="Cannot reach the server; still trying.")
    note.wait_for(timeout=SETTLE_MS)
    row = harness.settled()

    assert failed, "the failing poll was never sent"
    assert row.locator(".job-outcome").inner_text() == "Added"
    assert row.locator(".job-note", has_text="Cannot reach the server").count() == 0
