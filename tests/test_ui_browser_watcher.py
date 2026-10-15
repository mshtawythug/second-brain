"""A9's UI half, DRIVEN: the watcher status line in a real browser.

``tests/test_ui_watcher_status.py`` runs the words under node. What it cannot
reach is the wiring: that ``main.js`` actually calls ``wireWatcherStatus()``,
that the line lands in a live region a screen reader will announce, that a
375 px top bar still fits, and that coming back to the tab asks again. Each of
those is ``index.html``, ``main.js``, ``watcher.js`` and ``components.css``
acting together, so this module boots the REAL shell — never a hand-mounted
fragment, which is how ``palette.js`` once shipped tested and unreachable.

The harness is ``tests/test_ui_browser_pager.py``'s: a static server for the
real ``static/`` tree, ``/api/**`` fulfilled from here. ``/api/watcher``
answers from a mutable holder, so a test can change the server's mind and then
bring the tab back.

No PII: enum strings and empty stubs.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest

from brain.ui.app import static_dir

pytestmark = pytest.mark.browser

NOT_RUNNING = "Watcher not running — external edits will not appear in search"
UNKNOWN_PREFIX = "Watcher status unknown"

_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": {"count": 0, "name": "", "path": "", "empty_hint": "none",
                  "children": [], "notes": []},
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
}

#: Counts ANSWERED /api/watcher fetches in the page, so a test waits on "the
#: Nth answer arrived" rather than sleeping — the pager harness's pattern.
_COUNT_ANSWERS = (
    "window.__watcherAnswered = 0;"
    "const f = window.fetch;"
    "window.fetch = function (input, init) {"
    "  const u = typeof input === 'string' ? input : input.url;"
    "  const sent = f.apply(this, arguments);"
    "  if (!u || u.indexOf('/api/watcher') === -1) return sent;"
    "  const count = () => { window.__watcherAnswered++; };"
    "  sent.then(count, count);"
    "  return sent;"
    "};"
)


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


class _Shell:
    """A booted shell plus the holder ``/api/watcher`` answers from."""

    def __init__(self, page: Any, answer: dict[str, Any]) -> None:
        self.page = page
        self.answer = answer

    def answered(self) -> int:
        return int(self.page.evaluate("window.__watcherAnswered"))

    def wait_for_answers(self, count: int) -> None:
        """Block until ``count`` answers arrived, then let the render settle.

        The counter moves when ``fetch`` resolves; ``api()`` still has to parse
        the body and ``watcher.js`` to render it, all in the next few tasks. One
        macrotask hop plus a short pause covers that without polling for a
        change that a "nothing changed" assertion could never observe.
        """
        self.page.wait_for_function(
            "n => window.__watcherAnswered >= n", arg=count, timeout=5000
        )
        self.page.wait_for_timeout(150)

    def wait_for_text(self, text: str) -> None:
        self.page.wait_for_function(
            "t => document.querySelector('#watcher-status .watcher-full')"
            ".textContent === t",
            arg=text,
            timeout=5000,
        )

    def come_back(self) -> None:
        """The reader returns to the tab: ``visibilitychange`` while visible."""
        self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


@pytest.fixture
def make_shell(browser: Any, static_origin: str) -> Iterator[Any]:
    opened: list[Any] = []

    def make(state: str, *, width: int = 1280, fake_clock: bool = False,
             read_only: bool = False) -> _Shell:
        answer: dict[str, Any] = {"status": 200, "body": {"state": state}}
        stubs = {**_STUBS, "/api/health": {**_STUBS["/api/health"], "read_only": read_only}}

        def route_api(route: Any) -> None:
            path = urlsplit(route.request.url).path
            if path == "/api/watcher":
                route.fulfill(status=answer["status"], content_type="application/json",
                              body=json.dumps(answer["body"]))
                return
            body = stubs.get(path)
            route.fulfill(status=200 if body else 404,
                          body=json.dumps(body) if body else "{}",
                          content_type="application/json")

        page = browser.new_page(viewport={"width": width, "height": 800})
        opened.append(page)
        page.route("**/api/**", route_api)
        page.add_init_script(_COUNT_ANSWERS)
        if fake_clock:
            page.clock.install()
        page.goto(f"{static_origin}/static/index.html")
        return _Shell(page, answer)

    try:
        yield make
    finally:
        for page in opened:
            page.close()


def test_a_stopped_watcher_is_reported_in_words(make_shell: Any) -> None:
    """A9 verbatim: the booted shell — no hand-mounting — shows the sentence."""
    shell = make_shell("not_running")

    shell.wait_for_text(NOT_RUNNING)

    assert shell.page.is_visible("#watcher-status .watcher-full")
    assert shell.page.inner_text("#watcher-status .watcher-full") == NOT_RUNNING
    assert shell.page.get_attribute("#watcher-status", "data-tone") == "warn"


def test_the_line_is_a_polite_live_region_that_is_never_hidden(make_shell: Any) -> None:
    """Present and un-hidden from first paint, so a CHANGE is what is announced.

    A live region that is inserted or un-hidden in the same moment its text
    changes is announced unreliably, so a running watcher leaves the region in
    place and merely empty.
    """
    shell = make_shell("running")
    shell.wait_for_answers(1)

    region = shell.page.locator("#watcher-status")
    assert region.get_attribute("role") == "status"
    assert region.get_attribute("aria-live") == "polite"
    assert region.get_attribute("hidden") is None
    assert region.text_content() == ""


def test_a_running_watcher_shows_nothing(make_shell: Any) -> None:
    shell = make_shell("running")
    shell.wait_for_answers(1)

    assert shell.page.inner_text("#watcher-status").strip() == ""


def test_an_unknown_state_is_said_quietly(make_shell: Any) -> None:
    shell = make_shell("unavailable")
    shell.page.wait_for_function(
        "p => document.querySelector('#watcher-status .watcher-full')"
        ".textContent.startsWith(p)",
        arg=UNKNOWN_PREFIX, timeout=5000,
    )

    assert shell.page.get_attribute("#watcher-status", "data-tone") == "muted"


#: The status line must be READABLE, not merely present. The first version
#: let it shrink with an ellipsis: at 375px it rendered "Wa…" and squeezed the
#: read-only badge onto two lines, and every assertion here still passed.
_NOT_TRUNCATED = (
    "() => { const e = document.getElementById('watcher-status');"
    " return e.scrollWidth <= e.clientWidth && e.clientWidth > 0; }"
)


@pytest.mark.parametrize(
    ("width", "read_only"),
    [(375, False), (375, True), (900, True)],
    ids=["phone", "phone-read-only", "tablet-read-only"],
)
def test_a_narrow_top_bar_gets_the_short_line_and_a_screen_reader_the_whole_one(
    make_shell: Any, width: int, read_only: bool
) -> None:
    shell = make_shell("not_running", width=width, read_only=read_only)
    shell.wait_for_text(NOT_RUNNING)
    page = shell.page

    assert page.is_visible("#watcher-status .watcher-short")
    assert page.inner_text("#watcher-status .watcher-short") == "Watcher off"
    assert page.evaluate(_NOT_TRUNCATED), "the short line is cut off"
    full_box = page.locator("#watcher-status .watcher-full").bounding_box()
    assert full_box is not None and full_box["width"] <= 1, (
        "the full sentence is still laid out on a narrow top bar"
    )
    # The full sentence is visually hidden, not removed: it is what the live
    # region's accessible text consists of, at every width.
    snapshot = page.locator("#watcher-status").aria_snapshot()
    assert snapshot.count(NOT_RUNNING) == 1
    assert "Watcher off" not in snapshot
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    ), f"the top bar pushed the page into a horizontal scroll at {width}px"


def test_a_wide_top_bar_shows_the_whole_sentence_uncut(make_shell: Any) -> None:
    shell = make_shell("not_running", read_only=True)
    shell.wait_for_text(NOT_RUNNING)

    assert shell.page.evaluate(_NOT_TRUNCATED), "the full sentence is cut off"
    assert shell.page.get_attribute("#mode-badge", "hidden") is None


def test_a_wide_screen_does_not_show_the_short_line(make_shell: Any) -> None:
    shell = make_shell("not_running")
    shell.wait_for_text(NOT_RUNNING)

    assert not shell.page.is_visible("#watcher-status .watcher-short")


def test_coming_back_to_the_tab_asks_again(make_shell: Any) -> None:
    """The realistic case: started the watcher in a terminal, back to the browser."""
    shell = make_shell("not_running")
    shell.wait_for_text(NOT_RUNNING)
    before = shell.answered()
    shell.answer["body"] = {"state": "running"}

    shell.come_back()

    shell.wait_for_text("")
    assert shell.answered() > before


def test_a_failed_check_keeps_the_last_answer(make_shell: Any) -> None:
    """Unreachable is boot's toast, not a claim about the watcher either way."""
    shell = make_shell("not_running")
    shell.wait_for_text(NOT_RUNNING)
    before = shell.answered()
    shell.answer.update(status=500, body={"error": {"code": "internal_error",
                                                    "message": "synthetic"}})

    shell.come_back()
    shell.wait_for_answers(before + 1)

    assert shell.page.inner_text("#watcher-status .watcher-full") == NOT_RUNNING


def test_refocusing_the_window_asks_again(make_shell: Any) -> None:
    shell = make_shell("not_running")
    shell.wait_for_text(NOT_RUNNING)
    before = shell.answered()
    shell.answer["body"] = {"state": "running"}

    shell.page.evaluate("window.dispatchEvent(new Event('focus'))")

    shell.wait_for_text("")
    assert shell.answered() > before


def test_a_hidden_tab_does_not_ask(make_shell: Any) -> None:
    """Polling a tab nobody can see forks ``ps`` for no reader."""
    shell = make_shell("not_running")
    shell.wait_for_text(NOT_RUNNING)
    before = shell.answered()
    shell.page.evaluate(
        "Object.defineProperty(document, 'hidden', {value: true, configurable: true})"
    )

    shell.come_back()
    shell.page.evaluate("window.dispatchEvent(new Event('focus'))")
    shell.page.wait_for_timeout(300)

    assert shell.answered() == before


def test_a_visible_tab_asks_again_every_minute(make_shell: Any) -> None:
    shell = make_shell("not_running", fake_clock=True)
    shell.wait_for_text(NOT_RUNNING)
    before = shell.answered()
    shell.answer["body"] = {"state": "running"}

    shell.page.clock.run_for(59_000)
    shell.page.wait_for_timeout(300)
    assert shell.answered() == before, "asked before the minute was up"

    shell.page.clock.run_for(1_000)
    shell.wait_for_text("")
    assert shell.answered() == before + 1
