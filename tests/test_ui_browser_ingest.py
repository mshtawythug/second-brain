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
on a sleep; the few that prove an ABSENCE (no further poll, no re-announcement)
wait a bounded few poll intervals and say so.

**The scenario is chosen by a marker in the BODY** (``scenario:<name>``), never by
the title, so a test can type any title, including one it then proves the panel
never shows.

The harness (the fake, the fixtures, the recorders) is ``tests/ui_ingest_harness.py``;
the races, resumption and announcement tests are
``tests/test_ui_browser_ingest_lifecycle.py``.

No PII: nonsense titles and synthetic ids.
"""
from __future__ import annotations

import pytest

from tests.ui_ingest_harness import (
    DOC_ID,
    ENABLED_CONFIG,
    SETTLE_MS,
    Harness,
    _browser_fixture,  # noqa: F401 — registers `browser`, which `harness` uses
    _harness_fixture,  # noqa: F401 — registers `harness`
    _json,
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`
)

pytestmark = pytest.mark.browser


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
        "title": "Synthetic paste", "body": "scenario:added quarkle zibbit flonk",
        "tags": ["alpha", "Beta Two"],
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


#: Distinctive, so its absence cannot be satisfied by a coincidence of wording.
TYPED_TITLE = "Zorblax Quillfeather"


def test_a_withheld_result_has_no_link_and_never_the_typed_title(harness: Harness) -> None:
    page = harness.page
    harness.open_tab()
    harness.paste("withheld", title=TYPED_TITLE)
    row = harness.settled()

    assert harness.jobs.pastes[0]["title"] == TYPED_TITLE  # control: it was typed and sent
    assert "title is withheld" in row.inner_text()
    assert row.locator("a").count() == 0
    assert row.locator(".job-title").count() == 0
    assert TYPED_TITLE not in page.locator("#ingest-jobs").inner_text()
    assert all(TYPED_TITLE not in text for text in harness.announced(raw=True))
    assert TYPED_TITLE not in page.evaluate(
        "() => JSON.stringify(Object.assign({}, sessionStorage))")


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
    page.keyboard.type("Keyboard paste")
    page.keyboard.press("Tab")
    page.keyboard.type("scenario:added quarkle zibbit flonk")
    page.focus("#ingest-submit")
    page.keyboard.press("Enter")
    harness.settled()
    assert harness.jobs.pastes and harness.jobs.pastes[0]["title"] == "Keyboard paste"


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


def test_polling_stops_at_a_terminal_state(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("added")
    harness.settled()
    polls = [r for r in harness.requests if r.startswith("GET /api/jobs/")]

    # An ABSENCE, so it needs a bounded wait: three poll intervals at the fast rate.
    harness.page.wait_for_timeout(3000)

    assert [r for r in harness.requests if r.startswith("GET /api/jobs/")] == polls


def test_the_live_region_announces_the_outcome(harness: Harness) -> None:
    harness.open_tab()
    harness.paste("added")
    harness.settled()

    harness.page.wait_for_function(
        "() => document.getElementById('ingest-announce').textContent"
        " === 'Paste job: Added'", timeout=SETTLE_MS)
    region = harness.page.locator("#ingest-announce")
    assert region.get_attribute("aria-live") == "polite"
    # The job list itself is not live: progress would re-announce on every poll.
    assert harness.page.locator("#ingest-jobs").get_attribute("aria-live") is None
