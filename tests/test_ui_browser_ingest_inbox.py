"""The Ingest tab's inbox mode in a real browser (RM-40).

Hermetic, on the RM-11 harness (``tests/ui_ingest_harness.py``): every ``/api/**``
call is answered by ``page.route``; the inbox routes list two synthetic names and
record what the submit sent. The list and the pick, keyboard included, the folder's
sensitivity preselected, names drawn as text, and each refusal the listing reports.

No PII: nonsense file names.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from tests.ui_ingest_harness import (
    Harness,
    _browser_fixture,  # noqa: F401 — registers `browser`, which `harness` uses
    _harness_fixture,  # noqa: F401 — registers `harness`
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`
)

pytestmark = pytest.mark.browser


def _inbox_mode(harness: Harness) -> Any:
    harness.open_tab()
    page = harness.page
    page.check("#ingest-mode-inbox")
    page.wait_for_selector("#ingest-inbox-fields:not([hidden])")
    return page


def _labels(page: Any) -> list[str]:
    return page.locator("#ingest-inbox-list label").all_text_contents()


def test_the_inbox_lists_names_and_sizes_and_a_pick_is_ingested_by_name(
    harness: Harness,
) -> None:
    page = _inbox_mode(harness)
    page.wait_for_selector("#ingest-inbox-list li")

    assert _labels(page) == ["zibbit plan.md (2 KiB)", "flonk.txt (7 bytes)"]
    assert page.is_hidden("#ingest-body") and page.is_hidden("#ingest-drop")
    assert page.is_checked("#ingest-sensitivity-confidential")  # the folder's level

    page.check("#ingest-inbox-file-0")
    page.fill("#ingest-tags", "alpha")
    page.click("#ingest-submit")
    row = harness.settled()

    assert harness.jobs.inbox_posts == [{"name": "zibbit plan.md", "tags": ["alpha"],
                                         "sensitivity": "confidential", "enrich": True}]
    assert row.locator(".job-kind").text_content() == "Inbox"
    assert row.locator(".job-outcome").text_content() == "Added"
    assert row.locator(".job-file").text_content() == "From zibbit plan.md"
    assert _labels(page) == ["flonk.txt (7 bytes)"]  # the picked file left the list


def test_submitting_without_a_pick_asks_for_one(harness: Harness) -> None:
    page = _inbox_mode(harness)
    page.wait_for_selector("#ingest-inbox-list li")

    page.click("#ingest-submit")

    assert page.text_content("#ingest-error") == "Pick a file from the inbox."
    assert harness.jobs.inbox_posts == []


def test_the_optional_title_and_a_changed_sensitivity_are_sent(harness: Harness) -> None:
    page = _inbox_mode(harness)
    page.wait_for_selector("#ingest-inbox-list li")

    page.check("#ingest-inbox-file-1")
    page.fill("#ingest-inbox-title", "Chosen Title")
    page.check("#ingest-sensitivity-normal")
    page.click("#ingest-submit")
    harness.settled()

    assert harness.jobs.inbox_posts[0] == {"name": "flonk.txt", "title": "Chosen Title",
                                           "tags": [], "sensitivity": "normal",
                                           "enrich": True}


def test_the_list_is_reachable_and_pickable_by_keyboard(harness: Harness) -> None:
    page = _inbox_mode(harness)
    page.wait_for_selector("#ingest-inbox-list li")

    page.focus("#ingest-inbox-file-0")
    page.keyboard.press("Space")
    page.keyboard.press("ArrowDown")

    assert page.is_checked("#ingest-inbox-file-1")


def test_a_hostile_name_is_drawn_as_text(harness: Harness) -> None:
    harness.jobs.inbox = {**harness.jobs.inbox,
                          "files": [{"name": "<img src=x onerror=alert(1)>.md", "size": 3}]}
    page = _inbox_mode(harness)
    page.wait_for_selector("#ingest-inbox-list li")

    assert _labels(page) == ["<img src=x onerror=alert(1)>.md (3 bytes)"]
    assert page.locator("#ingest-inbox-list img").count() == 0


def test_refresh_fetches_the_list_again(harness: Harness) -> None:
    page = _inbox_mode(harness)
    page.wait_for_selector("#ingest-inbox-list li")
    harness.jobs.inbox = {**harness.jobs.inbox, "files": [{"name": "new.md", "size": 1}]}

    page.click("#ingest-inbox-refresh")

    page.wait_for_function(
        "() => document.querySelector('#ingest-inbox-list label')?.textContent"
        " === 'new.md (1 byte)'"
    )
    assert harness.jobs.inbox_lists == 2


@pytest.mark.parametrize(
    ("listing", "says"),
    [
        ({"available": False, "reason": "inbox_unconfigured", "files": []}, "No inbox folder"),
        ({"available": False, "reason": "inbox_misconfigured", "files": []}, "brain doctor"),
        ({"available": True, "reason": None, "files": [], "extensions": [".md"]},
         "The inbox is empty."),
    ],
)
def test_the_listing_reason_is_shown(
    harness: Harness, listing: dict[str, Any], says: str
) -> None:
    harness.jobs.inbox = {**harness.jobs.inbox, **listing}
    page = _inbox_mode(harness)

    page.wait_for_selector("#ingest-inbox-empty:not([hidden])")

    assert says in page.text_content("#ingest-inbox-empty")
    assert page.locator("#ingest-inbox-list li").count() == 0


def test_a_refused_submit_shows_the_servers_message(harness: Harness) -> None:
    harness.jobs.inbox_error = (400, "not_a_regular_file",
                                "not a regular file (a link, folder or pipe)")
    page = _inbox_mode(harness)
    page.wait_for_selector("#ingest-inbox-list li")

    page.check("#ingest-inbox-file-0")
    page.click("#ingest-submit")

    page.wait_for_selector("#ingest-error:not([hidden])")
    assert "not a regular file" in page.text_content("#ingest-error")
    assert page.locator("#ingest-jobs > li").count() == 0


# --- review round 1 -------------------------------------------------------------------


def test_the_listing_is_fetched_only_when_inbox_mode_is_picked(harness: Harness) -> None:
    """L5: a prompted view. Never on page load, on opening the tab, or in Paste/File mode."""
    harness.open_tab()
    page = harness.page
    page.check("#ingest-mode-file")
    page.check("#ingest-mode-paste")
    page.wait_for_timeout(300)  # an ABSENCE: give a stray background fetch time to show

    assert harness.jobs.inbox_lists == 0

    page.check("#ingest-mode-inbox")
    page.wait_for_selector("#ingest-inbox-list li")

    assert harness.jobs.inbox_lists == 1


def test_an_inbox_with_no_ingestible_type_says_so_not_empty(harness: Harness) -> None:
    """F2: a manual folder without `extensions` gets the image defaults."""
    harness.jobs.inbox = {**harness.jobs.inbox, "extensions": [], "files": []}
    page = _inbox_mode(harness)

    page.wait_for_selector("#ingest-inbox-empty:not([hidden])")

    text = page.text_content("#ingest-inbox-empty")
    assert "no file type this tab can ingest" in text and ".md" in text
    assert "empty" not in text


def test_leaving_inbox_mode_restores_the_modes_sensitivity(harness: Harness) -> None:
    """F4: the inbox preselects its folder's level; Paste and File keep theirs."""
    page = _inbox_mode(harness)
    page.wait_for_selector("#ingest-inbox-list li")
    assert page.is_checked("#ingest-sensitivity-confidential")

    page.check("#ingest-mode-paste")

    assert page.is_checked("#ingest-sensitivity-normal")


def test_a_listing_that_answers_after_the_user_left_never_moves_the_radio(
    harness: Harness,
) -> None:
    """F4: the preselect happens only while Inbox mode is still the one shown."""
    held: list[Any] = []
    harness.page.route("**/api/ingest/inbox", lambda route: held.append(route))
    harness.open_tab()
    page = harness.page
    page.check("#ingest-mode-inbox")
    harness.wait_until(lambda: bool(held), "the held listing request")
    page.check("#ingest-mode-paste")

    held[0].fulfill(status=200, content_type="application/json",
                    body=json.dumps(harness.jobs.inbox))
    page.wait_for_selector("#ingest-inbox-list li", state="attached")

    assert page.is_checked("#ingest-sensitivity-normal")


def test_a_refusal_refreshes_the_list(harness: Harness) -> None:
    """F5: a 4xx can mean the list is stale, so it is fetched again."""
    harness.jobs.inbox_error = (404, "inbox_file_unknown", "no such file in the inbox")
    page = _inbox_mode(harness)
    page.wait_for_selector("#ingest-inbox-list li")
    harness.jobs.inbox = {**harness.jobs.inbox, "files": [{"name": "flonk.txt", "size": 7}]}

    page.check("#ingest-inbox-file-0")
    page.click("#ingest-submit")

    page.wait_for_selector("#ingest-error:not([hidden])")
    harness.wait_until(lambda: harness.jobs.inbox_lists == 2, "the refreshed listing")
    page.wait_for_function(
        "() => document.querySelectorAll('#ingest-inbox-list li').length === 1"
    )
