"""The pagination control, DRIVEN — real clicks, real requests, real focus.

``tests/test_ui_pager.py`` executes the decision half under node. What it cannot
reach is the half this module exists for: that the decision is WIRED — that the
offset it computes actually leaves the browser on the query string, that
clicking Next moves the ledger, and that changing the search puts the reader
back on page one. Every one of those is a property of ``results.js``,
``store.js`` and ``index.html`` acting together, and none of them can be
asserted about a source string.

THE DEFECT, restated as what this catches: ``/api/search`` had taken an
``offset`` since T6 and no JavaScript ever sent one, so the ledger showed the
first page and nothing else was reachable. :func:`test_next_asks_the_server_for
_the_next_page` is that defect verbatim — it reads the query string the browser
actually sent.

The harness is ``tests/test_ui_browser_api.py``'s: a plain static server for the
real ``static/`` tree, with ``/api/**`` fulfilled from this module. The search
route is a FUNCTION of the requested offset rather than a fixture, so paging
forward and back exercises the same arithmetic the server would.

No PII: one nonsense query and synthetic titles.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from brain.ui.app import static_dir
from brain.ui.schemas import ranking_status

pytestmark = pytest.mark.browser

#: Enough of a boot for the page to evaluate its modules and settle.
_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": {"count": 0, "name": "", "path": "", "empty_hint": "none",
                  "children": [], "notes": []},
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
}

#: A nonsense term, so nothing about these assertions depends on real content.
TERM = "zorblat"

#: The page size the stub serves. Chosen HERE and echoed back in the payload's
#: ``limit``, exactly as the server echoes ``spec.limit`` — no test below reads
#: it from ``schemas``, because what is under test is the control's arithmetic
#: over whatever the server said, not the server's own default.
PAGE = 25

#: How many documents the stub's ranker will produce before it stops. Deliberately
#: NOT a multiple of :data:`PAGE`, so the last page is short and the range label
#: has something to be wrong about.
RANKED = 87


def _search_payload(offset: int, *, ranked: int = RANKED, status: str | None = None,
                    total: int | None = 544) -> dict[str, Any]:
    """One page of a ranking of ``ranked`` documents, sliced at ``offset``.

    ``status`` is computed by ``schemas.ranking_status`` ITSELF rather than by a
    rule written here. A stub with its own copy of that rule can walk a sequence
    of statuses the real server never emits — the first version of this file did
    exactly that and reported ``ceiling`` for a ranking that was genuinely
    exhausted, which made the boundary comparison below compare a thing to
    itself. Tests may still pin a status explicitly; that is what ``status``
    being a parameter is for.
    """
    fetch_limit = offset + PAGE
    rows = max(0, min(ranked, fetch_limit) - offset)
    if status is None:
        status = ranking_status(
            ranked=min(ranked, fetch_limit),
            fetch_limit=fetch_limit,
            total_documents=total,
        )
    return {
        "session_id": "00000000-0000-0000-0000-000000000000",
        "query": TERM,
        "offset": offset,
        "limit": PAGE,
        "returned": rows,
        "total_documents": total,
        "fts_count": rows,
        "timing_ms": {"embed": 10, "sql": 20, "facets": 1, "total": 31},
        "embed_cached": False,
        "fts_only": False,
        "facets": None,
        "ranking": {
            "status": status,
            "ranked_documents": min(ranked, fetch_limit),
            "max_ranked_documents": 100,
        },
        "results": [
            {"id": f"{offset + i:032x}", "title": f"Synthetic note {offset + i + 1}",
             "source_kind": "manual", "date": "2026-03-01",
             "snippet": "a snippet", "score": 0.5,
             "content_type": "note", "tags": []}
            for i in range(rows)
        ],
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


class _Ledger:
    """A loaded page plus the query strings its searches actually carried."""

    def __init__(self, page: Any, searches: list[dict[str, list[str]]]) -> None:
        self.page = page
        self.searches = searches

    def wait_for_search(self, count: int) -> dict[str, list[str]]:
        """Block until the ``count``-th search has been issued, and return it."""
        self.page.wait_for_function(
            "n => window.__searchCount >= n", arg=count, timeout=5000
        )
        return self.searches[count - 1]

    def wait_for_ready(self) -> None:
        """Block until the ledger has RENDERED, not merely asked.

        ``wait_for_search`` returns as soon as the request leaves the browser,
        and the DOM at that instant still shows the previous page — asserting on
        it there reads the state the click was supposed to change. Race-free
        because ``runSearch`` dispatches ``loading`` BEFORE it calls ``api()``,
        so by the time the fetch counter has moved, ``#meta`` already says
        "searching…" and can only be leaving that state.
        """
        self.page.wait_for_function(
            "() => document.getElementById('meta').textContent !== 'searching…'",
            timeout=5000,
        )

    def status(self) -> str:
        return str(self.page.inner_text("#pager-status"))

    def visible(self) -> bool:
        """Is the control ON SCREEN — computed, not the attribute.

        Reading `hidden` would answer "did results.js set the attribute", which
        is not the property. `.pager { display: flex }` and the UA's bare
        `[hidden]` are the same specificity, so an author rule wins on cascade
        order and the attribute hides NOTHING without `.pager[hidden]` in
        components.css. This codebase has hit that trap three times already
        (`.shell[hidden]`, `.field[hidden]`, `.toast[hidden]`), and one of them
        was caught by exactly this distinction: a browser test asserting the
        element was not visible, on an attribute that was set correctly and had
        no effect.
        """
        return bool(self.page.is_visible("#pager"))

    def disabled(self, which: str) -> bool:
        return bool(self.page.get_attribute(f"#pager-{which}", "disabled") is not None)


@pytest.fixture(scope="module")
def browser(static_origin: str) -> Iterator[Any]:
    """ONE chromium for the module, because two are not allowed.

    ``sync_playwright()`` cannot be entered twice on one thread — the second
    raises "you are using Playwright Sync API inside the asyncio loop". The
    boundary test below needs TWO loaded pages at once (a ceiling ending beside
    an exhausted one, so the comparison is controlled), so the browser is owned
    here and each ledger is a page on it.
    """
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


@pytest.fixture
def make_ledger(browser: Any, static_origin: str) -> Iterator[Any]:
    """Open a ledger over a stubbed ranking of the caller's shape."""
    opened: list[Any] = []

    def make(ranked: int = RANKED, status: str | None = None,
             total: int | None = 544) -> _Ledger:
        searches: list[dict[str, list[str]]] = []

        def route_api(route: Any) -> None:
            split = urlsplit(route.request.url)
            if split.path == "/api/search":
                query = parse_qs(split.query)
                searches.append(query)
                offset = int(query.get("offset", ["0"])[0])
                route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps(_search_payload(
                        offset, ranked=ranked, status=status, total=total)),
                )
                return
            body = _STUBS.get(split.path)
            route.fulfill(status=200 if body else 404,
                          body=json.dumps(body) if body else "{}",
                          content_type="application/json")

        pg = browser.new_page()
        opened.append(pg)
        pg.route("**/api/**", route_api)
        # Counted in the PAGE so a test can wait on "the Nth search has been
        # issued" instead of sleeping. Registered before navigation so the very
        # first search is counted too.
        pg.add_init_script(
            "window.__searchCount = 0;"
            "const f = window.fetch;"
            "window.fetch = function (input, init) {"
            "  const u = typeof input === 'string' ? input : input.url;"
            "  if (u && u.indexOf('/api/search') !== -1) window.__searchCount++;"
            "  return f.apply(this, arguments);"
            "};"
        )
        pg.goto(f"{static_origin}/static/index.html?q={TERM}")
        return _Ledger(pg, searches)

    try:
        yield make
    finally:
        for pg in opened:
            pg.close()


@pytest.fixture
def ledger(make_ledger: Any) -> _Ledger:
    """A ledger over a ranking the ranker cut short at :data:`RANKED`."""
    return make_ledger()


@pytest.fixture
def short_ledger(make_ledger: Any) -> _Ledger:
    """A ranking whose last page is a genuine end of the results.

    Same page size and same shape as :func:`ledger`; the ONLY difference is
    the ending, which is what makes the boundary assertions a controlled
    comparison rather than two unrelated screenshots.
    """
    return make_ledger(ranked=30, total=30)


# ------------------------------------------------------- the defect itself --


def test_the_first_page_is_requested_without_an_offset(ledger: _Ledger) -> None:
    """Page one must stay byte-identical to what the ledger sent before paging.

    Anti-vacuity for everything below as well: it establishes that this harness
    really does observe the query string, so a later "offset=25 was sent"
    assertion is reading a real signal rather than a fixture.
    """
    first = ledger.wait_for_search(1)

    assert first["q"] == [TERM]
    assert "offset" not in first, (
        f"the first page carried {first.get('offset')!r} — a request that used "
        "to have no offset now has one, so every log line, cache key and "
        "telemetry row for a non-paging search has changed shape"
    )


def test_next_asks_the_server_for_the_next_page(ledger: _Ledger) -> None:
    """THE DEFECT, VERBATIM. Before this change no JavaScript ever sent one."""
    ledger.wait_for_search(1)
    ledger.page.click("#pager-next")
    second = ledger.wait_for_search(2)

    assert second.get("offset") == [str(PAGE)], (
        f"clicking Next sent offset={second.get('offset')!r}. The rows past the "
        "first page are unreachable from the UI, which is the whole defect"
    )
    assert second["q"] == [TERM], "Next dropped the query it was paging through"


def test_the_reader_can_come_back(ledger: _Ledger) -> None:
    """Forward is only half a control; a reader who cannot return is trapped."""
    ledger.wait_for_search(1)
    ledger.wait_for_ready()
    assert ledger.disabled("prev"), "page one offered a Previous"

    ledger.page.click("#pager-next")
    ledger.wait_for_search(2)
    ledger.wait_for_ready()
    assert not ledger.disabled("prev"), "page two offered no way back"

    ledger.page.click("#pager-prev")
    third = ledger.wait_for_search(3)
    assert "offset" not in third, (
        f"Previous from page two sent offset={third.get('offset')!r} instead of "
        "returning to the unoffset first page"
    )


def test_the_ledger_shows_which_rows_are_on_screen(ledger: _Ledger) -> None:
    """The range is over the rows RETURNED, so a short last page is honest."""
    ledger.wait_for_search(1)
    ledger.wait_for_ready()
    assert "1–25" in ledger.status()

    ledger.page.click("#pager-next")
    ledger.wait_for_search(2)
    ledger.wait_for_ready()
    assert "26–50" in ledger.status()


# --------------------------------------------------- the boundary, legible --


def _page_to_the_end(ledger: _Ledger) -> None:
    """Click Next until it stops being offered, with a bound so a broken control
    cannot spin forever."""
    ledger.wait_for_search(1)
    ledger.wait_for_ready()
    clicks = 0
    while not ledger.disabled("next"):
        ledger.page.click("#pager-next")
        clicks += 1
        ledger.wait_for_search(clicks + 1)
        ledger.wait_for_ready()
        assert clicks < 10, "Next never stopped being offered"
    assert clicks > 0, (
        "Next was dead on the first page, so nothing below is about an ending"
    )


def test_the_ranking_ceiling_does_not_read_as_the_end_of_the_results(
    ledger: _Ledger, short_ledger: _Ledger
) -> None:
    """THE REQUIREMENT. Two endings, two sentences, and neither is the other's.

    At the ceiling the reader has NOT seen everything that matched; at
    exhaustion they have. Both directions are read from a real page, because
    "these two strings differ" is satisfied by two strings that are both wrong.
    """
    _page_to_the_end(ledger)
    at_ceiling = ledger.status()

    _page_to_the_end(short_ledger)
    at_end = short_ledger.status()

    assert at_ceiling != at_end, (
        f"the ranking ceiling and the end of the results both read {at_ceiling!r}, "
        "so a reader cannot tell 'there is more that search will not rank' from "
        "'you have seen everything'"
    )
    assert "rank" in at_ceiling.lower(), (
        f"the ceiling boundary ({at_ceiling!r}) does not say that RANKING is "
        "what ran out"
    )
    assert "rank" not in at_end.lower(), (
        f"an exhausted result set ({at_end!r}) was described as a ranking limit"
    )


def test_reaching_the_end_does_not_drop_keyboard_focus(ledger: _Ledger) -> None:
    """A disabled button is not focusable, so the last page must hand focus on.

    Without this the keyboard user pages to the end and lands at ``<body>`` —
    the top of the document, many tab stops from the control they were using.
    """
    ledger.wait_for_search(1)
    ledger.wait_for_ready()
    ledger.page.focus("#pager-next")
    _page_to_the_end(ledger)

    focused = ledger.page.evaluate("() => document.activeElement.id")
    assert focused == "pager-prev", (
        f"focus ended up on {focused!r} after the last page turn; the reader's "
        "place in the document was thrown away by their own click"
    )


# ------------------------------------------------- paging is not sticky ----


def test_changing_the_query_returns_the_reader_to_page_one(ledger: _Ledger) -> None:
    """An offset belongs to ONE ranking.

    Carried into a different query it lands the reader on page 4 of something
    they have not seen page 1 of — or on an empty page, for a query with three
    matches. Typing is the path most likely to be forgotten, because unlike the
    filters it is debounced and goes through a different handler.
    """
    ledger.wait_for_search(1)
    ledger.wait_for_ready()
    ledger.page.click("#pager-next")
    ledger.wait_for_search(2)
    ledger.wait_for_ready()

    ledger.page.fill("#q", f"{TERM} refined")
    latest = ledger.wait_for_search(3)

    assert "offset" not in latest, (
        f"a new query was sent with offset={latest.get('offset')!r} carried over "
        "from the previous search's page 2"
    )


def test_a_single_page_search_has_no_pager_at_all(make_ledger: Any) -> None:
    """A control whose every button is dead is furniture.

    It says only "this is all of it", which the ledger's own count already says,
    and it would appear under every one-page search in the app.
    """
    ledger = make_ledger(ranked=4, status="exhausted", total=4)
    ledger.wait_for_search(1)
    ledger.wait_for_ready()

    assert not ledger.visible(), (
        "a search that fits on one page still rendered a pagination control"
    )


def test_going_back_to_the_same_search_returns_to_page_one(ledger: _Ledger) -> None:
    """The one case the "did the search change?" rule cannot see.

    ``runSearch`` resets the offset when the query or the filters changed, which
    covers typing and every dropdown. It does NOT cover opening a note and
    pressing Back: an ``id`` is a navigational param, so it pushes a history
    entry, but it is not part of the search — so the search key on the way back
    is identical to the one on the way in and the rule finds nothing to reset.
    Without ``readUrl``'s own reset the reader lands on page 2 under an address
    bar that says page 1, and the ledger and the URL disagree about what is on
    screen.

    ``pushState`` stands in for ``openNote``, which is what writes that entry in
    the app (via ``syncUrl``). The URL transition — and therefore the ``popstate``
    the handler receives — is the same one, without needing a note payload
    stubbed for the inspector.
    """
    ledger.wait_for_search(1)
    ledger.wait_for_ready()
    ledger.page.click("#pager-next")
    assert ledger.wait_for_search(2).get("offset") == [str(PAGE)], (
        "the premise failed: the ledger was not on page two, so going Back "
        "from it proves nothing"
    )
    ledger.wait_for_ready()

    ledger.page.evaluate(
        "() => history.pushState(null, '', location.search + '&id=deadbeef')"
    )
    ledger.page.evaluate("() => history.back()")
    back = ledger.wait_for_search(3)

    assert "offset" not in back, (
        f"Back re-ran the search at offset={back.get('offset')!r} under a URL "
        "that never mentioned one — the ledger shows page 2 while the address "
        "bar describes page 1"
    )
