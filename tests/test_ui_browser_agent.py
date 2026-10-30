"""The Agent tab, EXECUTED in a real browser (RM-18b's five acceptance criteria).

Hermetic, like the rest of the CI browser job (``tests/test_ui_browser.py``, "WHY
IT IS HERMETIC"): ``/api/ask/config``, ``/api/ask`` and ``/api/jobs/{id}`` are
stubbed through ``page.route`` by ``tests/ui_ask_harness.py``, with the malicious
payloads the ticket names. The harness's payload keys are proven equal to the real
server's by ``tests/test_ui_ask_wire_contract.py`` (5434), the route test that
injects a fake ``ChatJson`` through ``UiContext``.

Every test also fails if the page raised, logged an error, or opened a dialog:
the harness fixture asserts all three at teardown.

No PII: nonsense words and synthetic UUIDs.
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.ui_ask_harness import (
    DOC_ID,
    HOSTILE_ANSWER,
    HOSTILE_ERROR,
    HOSTILE_NOTE,
    HOSTILE_SNIPPET,
    HOSTILE_SUBQUERY,
    HOSTILE_TITLE,
    SETTLE_MS,
    AskHarness,
    _ask_harness_fixture,  # noqa: F401 — registers `ask_harness`
)
from tests.ui_ingest_harness import (
    _browser_fixture,  # noqa: F401 — registers `browser`
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`
)

pytestmark = pytest.mark.browser

_PANE = "#tab-agent"


def _elements(page: Any, selector: str) -> int:
    count: int = page.locator(f"{_PANE} {selector}").count()
    return count


# ------------------------------------------------- AC1: model text is text --


def test_a_hostile_answer_renders_as_text_and_builds_no_element(
    ask_harness: AskHarness,
) -> None:
    page = ask_harness.page
    ask_harness.open_tab()
    ask_harness.ask("hostile")
    turn = ask_harness.settled()

    answer = turn.locator(".turn-answer")
    # Every character visible, as typed by the model.
    assert answer.inner_text() == HOSTILE_ANSWER
    text = turn.inner_text()
    for hostile in (HOSTILE_SUBQUERY, "<script>alert(11)</script>", HOSTILE_TITLE,
                    HOSTILE_SNIPPET, HOSTILE_NOTE, "<i>manual</i>"):
        assert hostile in text, hostile
    # No element came out of any of it.
    assert _elements(page, "img") == 0
    assert _elements(page, "script") == 0
    for tag in ("b", "i", "iframe"):
        assert _elements(page, tag) == 0, tag
    # Exactly two links: [1] in the answer and its "Open in Notes". [99] is not a
    # citation, and [2]'s id is not a UUID, so both stay text.
    links = page.locator(f"{_PANE} a")
    assert links.count() == 2
    assert {links.nth(i).get_attribute("href") for i in range(2)} == {f"/?id={DOC_ID}"}
    cites = answer.locator("a")
    assert cites.count() == 1
    assert cites.first.inner_text() == "[1]"
    assert "[99]" in answer.inner_text() and "[2]" in answer.inner_text()
    # The only attributes on a link are the ones this tab sets.
    for i in range(links.count()):
        names = links.nth(i).evaluate("a => [...a.attributes].map((x) => x.name).sort()")
        assert names == ["class", "data-action", "href"], names
    # Give a would-be onerror a moment to run; the fixture then asserts no dialog.
    page.wait_for_timeout(300)
    assert ask_harness.dialogs == []


def test_a_hostile_error_message_is_text(ask_harness: AskHarness) -> None:
    ask_harness.open_tab()
    ask_harness.ask("fails")
    turn = ask_harness.settled()

    assert turn.locator(".turn-outcome").inner_text() == "Failed"
    assert HOSTILE_ERROR in turn.inner_text()
    assert turn.locator(".turn-code").inner_text() == "chat_unavailable"
    assert _elements(ask_harness.page, "img") == 0


# --------------------------------------- AC2: [1] opens the cited note -------


def test_a_citation_marker_opens_the_cited_note_in_the_notes_tab(
    ask_harness: AskHarness,
) -> None:
    page = ask_harness.page
    ask_harness.open_tab()
    ask_harness.ask("answer")
    turn = ask_harness.settled()

    cite = turn.locator(".turn-answer a")
    assert cite.inner_text() == "[1]"
    assert cite.get_attribute("href") == f"/?id={DOC_ID}"
    cite.click()

    page.wait_for_selector("#tab-notes:not([hidden])")
    assert page.locator(_PANE).is_hidden()
    page.wait_for_function(
        "() => document.querySelector('.note-title')"
        " && document.querySelector('.note-title').textContent === 'Quarkle ledger'",
        timeout=SETTLE_MS)
    assert f"id={DOC_ID}" in page.url


def test_the_steps_render_in_order_and_the_answer_is_cited(ask_harness: AskHarness) -> None:
    page = ask_harness.page
    ask_harness.open_tab()
    ask_harness.ask("answer")
    page.wait_for_selector(f"{_PANE} .turn-progress", timeout=SETTLE_MS)
    turn = ask_harness.settled()

    steps = turn.locator(".turn-step").all_inner_texts()
    assert steps == [
        "Planning the search",
        "Retrieving notes (round 1 of 3)",
        "Checking whether that is enough (round 1 of 3)",
        "Writing the answer",
    ]
    assert turn.locator(".turn-question").inner_text() == "scenario:answer quarkle zibbit"
    assert turn.locator(".turn-answer").inner_text() == "Quarkle ships on Fridays [1]."
    assert turn.locator(".turn-citation").inner_text().startswith("[1] Quarkle ledger")
    assert turn.locator("progress").count() == 0
    assert ask_harness.fake.asks == [
        {"question": "scenario:answer quarkle zibbit", "no_loop": False}]
    assert ask_harness.announced()[-1] == "Agent: answer ready."


def test_a_withheld_answer_says_so_and_links_nothing(ask_harness: AskHarness) -> None:
    ask_harness.open_tab()
    ask_harness.ask("withheld")
    turn = ask_harness.settled()

    assert "The answer is withheld on this server" in turn.inner_text()
    assert "A confidential note (withheld on this server)" in turn.inner_text()
    assert turn.locator("a").count() == 0
    assert turn.locator(".turn-answer").count() == 0


# ------------------------------- AC3: cancel, and the waiting-for-ingest state --


def test_cancel_ends_the_ask_and_the_button_goes(ask_harness: AskHarness) -> None:
    page = ask_harness.page
    ask_harness.open_tab()
    ask_harness.ask("hold")
    cancel = ask_harness.turn().locator("button.turn-cancel")
    cancel.wait_for(timeout=SETTLE_MS)
    # One question at a time: the Ask button waits for this one.
    assert page.locator("#agent-submit").is_disabled()
    cancel.click()
    turn = ask_harness.settled()

    assert turn.locator(".turn-outcome").inner_text() == "Cancelled."
    assert turn.locator("button.turn-cancel").count() == 0
    assert [body for _, body in ask_harness.fake.cancels] == [{}]
    assert page.locator("#agent-submit").is_enabled()


def test_a_queued_ask_says_it_waits_for_the_ingest_job(ask_harness: AskHarness) -> None:
    page = ask_harness.page
    ask_harness.fake.listed = [{"id": "f" * 32, "kind": "ingest.paste", "state": "running"}]
    ask_harness.open_tab()
    ask_harness.ask("queue")

    phase = ask_harness.turn().locator(".turn-phase")
    page.wait_for_function(
        "() => document.querySelector('#agent-turns .turn-phase')?.textContent"
        " === 'Waiting for the ingest job to finish'", timeout=SETTLE_MS)
    assert ask_harness.turn().locator("button.turn-cancel").is_visible()

    ask_harness.fake.listed = []
    page.wait_for_function(
        "() => document.querySelector('#agent-turns .turn-phase')?.textContent"
        " === 'Queued: waiting for the job runner'", timeout=SETTLE_MS)
    assert phase.is_visible()
    ask_harness.turn().locator("button.turn-cancel").click()
    assert ask_harness.settled().locator(".turn-outcome").inner_text() == "Cancelled."


# --------------------------------------------------- availability and storage --


@pytest.mark.parametrize(
    ("reason", "says"),
    [("requires_loopback", "bound to this machine"), ("read_only", "read-only"),
     ("jobs_unavailable", "job runner")],
)
def test_a_server_that_cannot_ask_says_why(
    ask_harness: AskHarness, reason: str, says: str
) -> None:
    page = ask_harness.page
    ask_harness.fake.config = {**ask_harness.fake.config, "enabled": False, "reason": reason}
    page.click('.tab[data-tab="agent"]')
    notice = page.locator("#agent-unavailable")
    notice.wait_for(state="visible", timeout=SETTLE_MS)

    assert says in notice.inner_text()
    assert page.locator("#agent-fieldset").get_attribute("disabled") is not None
    assert page.locator("#agent-question").is_disabled()
    assert page.locator("#agent-submit").is_disabled()
    assert ask_harness.fake.asks == []


def test_a_question_over_the_limit_is_refused_before_any_request(
    ask_harness: AskHarness,
) -> None:
    ask_harness.fake.config = {**ask_harness.fake.config, "max_question_chars": 20}
    ask_harness.open_tab()
    ask_harness.ask("answer", words="a question well past twenty characters")

    error = ask_harness.page.locator("#agent-error")
    error.wait_for(state="visible")
    assert "the limit is 20" in error.inner_text()
    assert ask_harness.fake.asks == []


def test_an_ask_resumes_after_a_reload_and_never_shows_in_the_ingest_tab(
    ask_harness: AskHarness,
) -> None:
    """Plan doc D2/D3: ask ids live in their own scope, and ids only are stored."""
    page = ask_harness.page
    ask_harness.open_tab()
    ask_harness.ask("answer")
    ask_harness.settled()

    stored = page.evaluate("() => JSON.stringify(Object.entries(sessionStorage))")
    assert "quarkle" not in stored.lower()       # no question, no answer
    page.reload()
    page.wait_for_selector("#tab-notes")
    ask_harness.open_tab()
    turn = ask_harness.settled()
    assert turn.locator(".turn-question").inner_text().startswith("Asked at ")
    assert turn.locator(".turn-answer").inner_text() == "Quarkle ships on Fridays [1]."

    page.click('.tab[data-tab="ingest"]')
    page.wait_for_selector("#ingest-fieldset:not([disabled])")
    page.wait_for_timeout(1500)           # past one poll: nothing else is resumed
    assert page.locator("#ingest-jobs > li").count() == 0
    assert page.locator("#ingest-jobs-empty").is_visible()
