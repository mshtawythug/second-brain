"""The text-only fallback notice above the ledger, EXECUTED in a real browser.

Roadmap ticket RM-03. When the query embed fails, ``GET /api/search`` now
answers 200 with text-only results and four ``fallback*`` keys instead of
``503 embedding_unavailable``. What only a browser can show is the reader's
half: that the class-only ``fallback_note`` appears ABOVE the results, that the
next normal search takes it away, and that it is drawn as text, never parsed as
markup (``results.js`` sets ``textContent``; the single-``innerHTML`` rule in
``tests/test_ui_static_behaviour.py`` covers the source side).

Hermetic, like ``tests/test_ui_browser.py`` (see its "WHY IT IS HERMETIC"): a
plain static server for the real ``static/`` tree, with ``/api/**`` fulfilled
here by ``page.route``. No Postgres, no Ollama, no app.

No PII: nonsense queries and synthetic titles.
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

pytestmark = pytest.mark.browser

#: Enough of a boot for the page to evaluate its modules and settle.
_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": {"count": 0, "name": "", "path": "", "empty_hint": "none",
                  "children": [], "notes": []},
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
}

#: The query the stub answers with a fallback.
FALLEN = "zorblat"
#: The query the stub answers normally.
HEALTHY = "quindle"
#: The query the stub answers with a server error.
BROKEN = "snarfle"

#: Markup-looking on purpose: drawn as text it reads exactly like this; parsed
#: as HTML it would become a <b> element and lose its angle brackets.
NOTE = (
    "embedding backend unavailable (<b>OllamaEmbedError</b>); showing "
    "text-only (FTS) matches."
)


def _payload(query: str, *, fallback: bool) -> dict[str, Any]:
    """A two-result page, shaped like the route's response."""
    return {
        "session_id": "00000000-0000-0000-0000-000000000000",
        "query": query,
        "offset": 0,
        "limit": 25,
        "returned": 2,
        "total_documents": 2,
        "fts_count": 2,
        "timing_ms": {"embed": None if fallback else 10, "sql": 20,
                      "facets": 1, "total": 31},
        "embed_cached": False,
        "fts_only": fallback,
        "facets": None,
        "ranking": {"status": "exhausted", "ranked_documents": 2,
                    "max_ranked_documents": 100},
        "fallback": "fts_only" if fallback else None,
        "fallback_reason": "OllamaEmbedError" if fallback else None,
        "fallback_transient": True if fallback else None,
        "fallback_note": NOTE if fallback else None,
        "results": [
            {"id": f"{i:032x}", "title": f"Synthetic note {i + 1}",
             "source_kind": "manual", "date": "2026-03-01",
             "snippet": "a snippet", "score": 0.5,
             "content_type": "note", "tags": []}
            for i in range(2)
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


def _route_api(route: Any) -> None:
    split = urlsplit(route.request.url)
    if split.path == "/api/search":
        query = parse_qs(split.query).get("q", [""])[0]
        if query == BROKEN:
            route.fulfill(
                status=500, content_type="application/json",
                body=json.dumps({"error": {"code": "internal", "message": "boom"}}),
            )
            return
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(_payload(query, fallback=query == FALLEN)),
        )
        return
    body = _STUBS.get(split.path)
    route.fulfill(status=200 if body else 404,
                  body=json.dumps(body) if body else "{}",
                  content_type="application/json")


@pytest.fixture
def page(browser: Any, static_origin: str) -> Iterator[Any]:
    """A ledger that has searched :data:`FALLEN` and rendered the answer."""
    pg = browser.new_page()
    pg.route("**/api/**", _route_api)
    # Counted when the search's fetch RESOLVES, so a test waits on "the Nth
    # search has been answered" instead of sleeping (test_ui_browser_pager.py).
    pg.add_init_script(
        "window.__searchAnswered = 0;"
        "const f = window.fetch;"
        "window.fetch = function (input, init) {"
        "  const u = typeof input === 'string' ? input : input.url;"
        "  const sent = f.apply(this, arguments);"
        "  if (!u || u.indexOf('/api/search') === -1) return sent;"
        "  return sent.then((r) => { window.__searchAnswered++; return r; });"
        "};"
    )
    pg.goto(f"{static_origin}/static/index.html?q={FALLEN}")
    _wait_for_answer(pg, 1)
    try:
        yield pg
    finally:
        pg.close()


def _wait_for_answer(pg: Any, count: int) -> None:
    """Block until the ``count``-th search is answered AND rendered."""
    pg.wait_for_function("n => window.__searchAnswered >= n", arg=count, timeout=5000)
    pg.wait_for_function(
        "() => document.getElementById('meta').textContent !== 'searching…'",
        timeout=5000,
    )


def _search(pg: Any, query: str, count: int) -> None:
    pg.fill("#q", query)
    _wait_for_answer(pg, count)


def test_a_fallback_shows_the_note_above_the_results(page: Any) -> None:
    """AC 2: the note and the results, with the meta line still ``fts-only``."""
    assert page.is_visible("#search-fallback"), "the fallback notice is not on screen"
    assert page.text_content("#search-fallback") == NOTE
    assert page.get_attribute("#search-fallback", "role") == "status"
    assert page.locator("#results .result").count() == 2
    assert "fts-only" in page.inner_text("#meta-sub")

    notice = page.locator("#search-fallback").bounding_box()
    results = page.locator("#results").bounding_box()
    assert notice is not None and results is not None
    assert notice["y"] + notice["height"] <= results["y"], (
        "the notice must sit above the ledger, where it is read before the rows"
    )


def test_the_note_is_text_not_markup(page: Any) -> None:
    """Server-built and class-only, and still never parsed: ``textContent``."""
    assert page.locator("#search-fallback b").count() == 0
    assert "<b>OllamaEmbedError</b>" in page.inner_text("#search-fallback")


def test_a_following_normal_search_hides_the_note(page: Any) -> None:
    """AC 3."""
    _search(page, HEALTHY, 2)

    assert not page.is_visible("#search-fallback"), (
        "a search that did not fall back still shows the previous search's notice"
    )
    assert page.text_content("#search-fallback") == ""
    assert page.locator("#results .result").count() == 2
    assert "fts-only" not in page.inner_text("#meta-sub")


def test_a_failed_search_hides_the_note(page: Any) -> None:
    """An error answers nothing, so no notice can describe its results."""
    _search(page, BROKEN, 2)

    assert not page.is_visible("#search-fallback")
    assert page.locator("#results .error-state").count() == 1


def test_clearing_the_query_hides_the_note(page: Any) -> None:
    """The idle ledger has no results for a notice to describe."""
    page.fill("#q", "")
    page.wait_for_function(
        "() => document.getElementById('results').children.length === 0",
        timeout=5000,
    )

    assert not page.is_visible("#search-fallback")
