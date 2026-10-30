"""The Ingest tab's file mode in a real browser (Ingest spec §8, §9.4 upload rows; RM-13).

Hermetic, on the RM-11 harness (``tests/ui_ingest_harness.py``): every ``/api/**``
call is answered by ``page.route``, and the upload routes record what reached them.
Files are built here from named bytes, handed to the page with
``set_input_files``, a file chooser, or a synthetic ``drop`` event.

No PII: nonsense file names and synthetic bytes.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote

import pytest

from tests.ui_ingest_harness import (
    Harness,
    _browser_fixture,  # noqa: F401 — registers `browser`, which `harness` uses
    _harness_fixture,  # noqa: F401 — registers `harness`
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`
)

pytestmark = pytest.mark.browser

_MD = {"name": "quarkle-notes.md", "mimeType": "text/markdown",
       "buffer": b"# Quarkle notes\n\nZibbit flonk.\n"}


def _file_mode(harness: Harness) -> Any:
    harness.open_tab()
    page = harness.page
    page.check("#ingest-mode-file")
    page.wait_for_selector("#ingest-file-fields:not([hidden])")
    return page


def _pick(page: Any, payload: dict[str, Any] = _MD) -> None:
    page.set_input_files("#ingest-file", files=[payload])


def _ready(page: Any) -> None:
    page.wait_for_selector("#ingest-staged:not([hidden])")
    page.wait_for_function(
        "() => document.getElementById('ingest-staged-state').textContent.startsWith('Ready')"
    )


def _drop(page: Any, names: list[str]) -> None:
    page.evaluate(
        """(names) => {
          const transfer = new DataTransfer();
          for (const name of names) transfer.items.add(new File(["synthetic"], name));
          const zone = document.getElementById("ingest-drop");
          zone.dispatchEvent(new DragEvent("dragover", {dataTransfer: transfer, bubbles: true,
                                                        cancelable: true}));
          zone.dispatchEvent(new DragEvent("drop", {dataTransfer: transfer, bubbles: true,
                                                    cancelable: true}));
        }""",
        names,
    )


def test_a_picked_file_is_staged_raw_then_ingested_as_a_file_job(harness: Harness) -> None:
    page = _file_mode(harness)

    _pick(page)
    _ready(page)

    (upload,) = harness.jobs.uploads
    assert upload["content_type"] == "application/octet-stream"
    assert unquote(upload["filename"]) == "quarkle-notes.md"
    assert upload["size"] == len(_MD["buffer"])
    assert page.text_content("#ingest-staged-name") == "quarkle-notes.md"

    page.fill("#ingest-tags", "alpha, beta")
    page.click("#ingest-submit")
    row = harness.settled()

    (submitted,) = harness.jobs.files
    assert submitted == {"upload_id": f"{1:032x}", "tags": ["alpha", "beta"],
                         "sensitivity": "normal", "enrich": True}
    assert row.locator(".job-kind").text_content() == "File"
    assert row.locator(".job-outcome").text_content() == "Added"
    assert row.locator(".job-file").text_content() == "From quarkle-notes.md"
    assert page.is_hidden("#ingest-staged")
    assert harness.jobs.discards == []  # a submitted upload is the server's to delete


def test_the_optional_title_is_sent_only_when_given(harness: Harness) -> None:
    page = _file_mode(harness)
    _pick(page)
    _ready(page)

    page.fill("#ingest-file-title", "Chosen Title")
    page.click("#ingest-submit")
    harness.settled()

    assert harness.jobs.files[0]["title"] == "Chosen Title"


def test_the_switch_shows_one_mode_at_a_time(harness: Harness) -> None:
    harness.open_tab()
    page = harness.page
    assert page.is_visible("#ingest-body") and page.is_hidden("#ingest-drop")

    page.check("#ingest-mode-file")
    assert page.is_hidden("#ingest-body") and page.is_visible("#ingest-drop")
    assert page.is_hidden("#ingest-content-type")

    page.check("#ingest-mode-paste")
    assert page.is_visible("#ingest-body") and page.is_hidden("#ingest-drop")


def _intercept_file_choosers(page: Any) -> None:
    """Turn on file-chooser interception now, not inside ``expect_file_chooser``.

    The FIRST ``filechooser`` listener on a page makes the client send
    ``updateSubscription``, which enables CDP's ``setInterceptFileChooserDialog``,
    without waiting for a reply; the driver handles messages concurrently.
    Registered inside ``expect_file_chooser`` around a bare ``keyboard.press``,
    the driver sent the interception and the Enter ``Input.dispatchKeyEvent`` as
    consecutive CDP messages, the key before the interception was acknowledged
    (20 of 20 traced runs). Headless Chromium silently cancels an unintercepted
    chooser, so the run that lost the race timed out after 30 s: 5 of about 70
    CI runs, 2 of 30 locally. Registered here, the interception goes out before
    the awaited calls below, and ``expect_file_chooser`` then adds a SECOND
    listener, which sends nothing.
    """
    page.on("filechooser", lambda _chooser: None)


def test_the_whole_upload_works_from_the_keyboard(harness: Harness) -> None:
    harness.open_tab()
    page = harness.page
    _intercept_file_choosers(page)
    page.focus("#ingest-mode-paste")
    page.keyboard.press("ArrowDown")      # a radio group: arrows move the choice
    assert page.is_checked("#ingest-mode-file")
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement.id") == "ingest-drop"

    with page.expect_file_chooser() as chooser:
        page.keyboard.press("Enter")
    chooser.value.set_files([_MD])
    _ready(page)

    order = []
    for _ in range(10):
        page.keyboard.press("Tab")
        order.append(page.evaluate("() => document.activeElement.id"))
        if order[-1] == "ingest-submit":
            break
    assert order == ["ingest-staged-remove", "ingest-file-title", "ingest-tags",
                     "ingest-sensitivity-normal", "ingest-no-enrich", "ingest-submit"]
    page.keyboard.press("Enter")
    harness.settled()
    assert len(harness.jobs.files) == 1


def test_dropping_one_file_stages_it(harness: Harness) -> None:
    page = _file_mode(harness)

    _drop(page, ["dropped-zibbit.txt"])
    _ready(page)

    assert unquote(harness.jobs.uploads[0]["filename"]) == "dropped-zibbit.txt"
    assert page.get_attribute("#ingest-drop", "data-dragging") is None


def _drop_on(page: Any, selector: str, names: list[str]) -> bool:
    """Drop files on ``selector``; True when the page cancelled the default (open the file)."""
    return bool(page.evaluate(
        """([selector, names]) => {
          const transfer = new DataTransfer();
          for (const name of names) transfer.items.add(new File(["synthetic"], name));
          const target = document.querySelector(selector);
          const over = new DragEvent("dragover", {dataTransfer: transfer, bubbles: true,
                                                  cancelable: true});
          target.dispatchEvent(over);
          const drop = new DragEvent("drop", {dataTransfer: transfer, bubbles: true,
                                              cancelable: true});
          target.dispatchEvent(drop);
          return over.defaultPrevented && drop.defaultPrevented;
        }""",
        [selector, names],
    ))


def test_a_file_dropped_beside_the_zone_is_staged_not_opened(harness: Harness) -> None:
    """The browser would otherwise open the file in place of the app (rev-77 R4)."""
    page = _file_mode(harness)

    assert _drop_on(page, "#ingest-heading", ["beside-the-zone.md"])

    _ready(page)
    assert unquote(harness.jobs.uploads[0]["filename"]) == "beside-the-zone.md"


def test_in_paste_mode_a_page_drop_is_left_to_the_browser(harness: Harness) -> None:
    harness.open_tab()

    assert not _drop_on(harness.page, "#ingest-heading", ["pasted.md"])
    assert harness.jobs.uploads == []


def test_dropping_two_files_is_refused_without_a_request(harness: Harness) -> None:
    page = _file_mode(harness)

    _drop(page, ["a.txt", "b.txt"])

    assert page.text_content("#ingest-error") == "Drop one file at a time."
    assert harness.jobs.uploads == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"name": "tool.exe", "mimeType": "application/octet-stream", "buffer": b"MZ"},
         "Only .txt, .md, .markdown, .pdf, .docx files can be uploaded."),
        ({"name": "big.txt", "mimeType": "text/plain", "buffer": b"x" * 11},
         "The file is 11 bytes; the limit for .txt is 10 bytes."),
    ],
)
def test_the_client_refuses_a_wrong_type_or_size_before_sending(
    harness: Harness, payload: dict[str, Any], message: str
) -> None:
    harness.jobs.config = {**harness.jobs.config, "limits": {
        **harness.jobs.config["limits"],
        "upload_bytes": {**harness.jobs.config["limits"]["upload_bytes"], ".txt": 10}}}
    page = _file_mode(harness)

    _pick(page, payload)

    page.wait_for_selector("#ingest-error:not([hidden])")
    assert page.text_content("#ingest-error") == message
    assert harness.jobs.uploads == []
    assert page.is_hidden("#ingest-staged")


def test_a_server_refusal_is_shown_and_nothing_stays_staged(harness: Harness) -> None:
    harness.jobs.upload_error = (415, "unsupported_file", "the file is not a PDF")
    page = _file_mode(harness)

    _pick(page, {"name": "fake.pdf", "mimeType": "application/pdf", "buffer": b"nope"})

    page.wait_for_selector("#ingest-error:not([hidden])")
    assert page.text_content("#ingest-error") == "the file is not a PDF"
    assert page.is_hidden("#ingest-staged")
    page.click("#ingest-submit")
    assert page.text_content("#ingest-error") == "Choose a file to ingest."
    assert harness.jobs.files == []


def test_remove_discards_the_staged_file_with_a_json_body(harness: Harness) -> None:
    page = _file_mode(harness)
    _pick(page)
    _ready(page)

    page.click("#ingest-staged-remove")

    page.wait_for_selector("#ingest-staged", state="hidden")
    harness.wait_until(lambda: harness.jobs.discards, "the DELETE")
    assert harness.jobs.discards == [(f"{1:032x}", {})]
    assert page.evaluate("() => document.activeElement.id") == "ingest-drop"


def test_choosing_another_file_discards_the_first(harness: Harness) -> None:
    page = _file_mode(harness)
    _pick(page)
    _ready(page)

    _pick(page, {"name": "second.txt", "mimeType": "text/plain", "buffer": b"second"})
    page.wait_for_function(
        "() => document.getElementById('ingest-staged-name').textContent === 'second.txt'"
    )
    _ready(page)

    assert [d[0] for d in harness.jobs.discards] == [f"{1:032x}"]


def test_an_upload_removed_while_in_flight_is_deleted_when_it_lands(harness: Harness) -> None:
    page = _file_mode(harness)
    harness.jobs.holding = True
    _pick(page)
    harness.wait_until(lambda: harness.jobs.hold_uploads, "the held upload")
    assert page.text_content("#ingest-staged-state") == "Uploading…"

    page.click("#ingest-staged-remove")
    harness.jobs.holding = False
    harness.jobs.answer_upload(harness.jobs.hold_uploads.pop())

    harness.wait_until(lambda: harness.jobs.discards, "the late DELETE")
    assert harness.jobs.discards == [(f"{1:032x}", {})]
    assert page.is_hidden("#ingest-staged")


def test_submit_waits_for_the_upload_to_finish(harness: Harness) -> None:
    page = _file_mode(harness)
    harness.jobs.holding = True
    _pick(page)
    harness.wait_until(lambda: harness.jobs.hold_uploads, "the held upload")

    page.click("#ingest-submit")

    assert page.text_content("#ingest-error") == "Wait for the upload to finish."
    harness.jobs.holding = False
    harness.jobs.answer_upload(harness.jobs.hold_uploads.pop())
    _ready(page)
    assert harness.jobs.files == []


def test_an_expired_upload_asks_for_the_file_again(harness: Harness) -> None:
    harness.jobs.file_error = (404, "upload_unknown", "no such upload")
    page = _file_mode(harness)
    _pick(page)
    _ready(page)

    page.click("#ingest-submit")

    page.wait_for_selector("#ingest-error:not([hidden])")
    assert page.text_content("#ingest-error") == (
        "The uploaded file expired before it was ingested. Choose it again."
    )
    assert page.is_hidden("#ingest-staged")


def test_a_full_queue_keeps_the_staged_file_for_a_retry(harness: Harness) -> None:
    harness.jobs.file_error = (429, "queue_full", "at most 4 jobs may wait")
    page = _file_mode(harness)
    _pick(page)
    _ready(page)

    page.click("#ingest-submit")

    page.wait_for_selector("#ingest-error:not([hidden])")
    assert page.text_content("#ingest-error") == "at most 4 jobs may wait"
    assert page.is_visible("#ingest-staged")
    harness.jobs.file_error = None
    page.click("#ingest-submit")
    harness.settled()
    assert len(harness.jobs.files) == 2


def test_an_extension_without_a_cap_is_not_offered(harness: Harness) -> None:
    """The client never guesses a limit: a config naming a type without its cap
    (an older server) does not offer it, and nothing throws."""
    harness.jobs.config = {**harness.jobs.config, "limits": {"paste_body_bytes": 10,
                                                             "max_queued_jobs": 4}}
    harness.open_tab()

    assert harness.page.is_disabled("#ingest-mode-file")


def test_a_server_with_no_upload_types_offers_no_file_mode(harness: Harness) -> None:
    harness.jobs.config = {**harness.jobs.config, "extensions": []}
    harness.open_tab()

    assert harness.page.is_disabled("#ingest-mode-file")
    assert harness.page.text_content("#ingest-file-hint") == "This server accepts no file uploads."


def test_the_hint_names_the_types_and_caps(harness: Harness) -> None:
    _file_mode(harness)

    assert harness.page.text_content("#ingest-file-hint") == (
        "One file at a time: .txt up to 5.0 MiB, .md up to 5.0 MiB, .markdown up to 5.0 MiB, "
        ".pdf up to 25.0 MiB, .docx up to 25.0 MiB."
    )


def test_each_upload_state_is_announced_once(harness: Harness) -> None:
    page = _file_mode(harness)

    _pick(page)
    _ready(page)
    page.wait_for_timeout(300)  # a bounded wait for the announcer's next frame

    texts = harness.announced()
    assert texts.count("Uploading quarkle-notes.md.") == 1
    assert texts.count("quarkle-notes.md is ready to ingest.") == 1


def test_the_upload_carries_the_token_from_the_fragment(harness: Harness) -> None:
    page = harness.page
    # A real load (the query makes it a new document, not a fragment change):
    # api.js reads the token when its module is evaluated.
    page.goto(f"{harness.origin}/static/index.html?reload=1#t=tok-synthetic")
    page.wait_for_selector("#tab-notes")
    page = _file_mode(harness)

    _pick(page)
    _ready(page)

    assert harness.jobs.uploads[0]["token"] == "tok-synthetic"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_file_mode_renders_in_both_themes(harness: Harness, theme: str) -> None:
    page = harness.page
    page.evaluate(f"() => document.documentElement.setAttribute('data-theme', '{theme}')")
    page = _file_mode(harness)
    _pick(page)
    _ready(page)

    drop = page.locator("#ingest-drop")
    assert drop.bounding_box() is not None
    color = page.evaluate(
        "() => getComputedStyle(document.getElementById('ingest-staged-name')).color"
    )
    background = page.evaluate(
        "() => getComputedStyle(document.getElementById('ingest-staged')).backgroundColor"
    )
    assert color != background
