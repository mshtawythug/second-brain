"""`js/api.js`'s two parse-failure paths, EXECUTED against a real `fetch`.

L4 from the phase-2 review. `api()` parsed the response body inside a
`try`/`catch` that returned `null` on failure — for BOTH the error path and the
success path. On an error response that is correct and deliberate: a proxy or a
gateway can answer with HTML, and the error branch falls back to
``response.statusText``. On a SUCCESSFUL response it is not: every route under
``ui/`` returns ``JSONResponse`` (verified — no 204, no empty-body 200), so a
2xx whose body will not parse is a broken server contract, and ``null`` made the
app pretend it had data.

WHAT THE OLD BEHAVIOUR COST. The caller destructures off ``null`` and throws a
raw ``TypeError`` from wherever it first touched the payload — "Cannot read
properties of null (reading 'body')" — which names a symptom several frames away
from the cause. ``loadFacets`` swallows its exception entirely, so the filter
dropdowns simply stay empty with nothing logged anywhere.

BOTH DIRECTIONS ARE TESTED HERE, and that is the point rather than thoroughness:
the fix is only correct if it distinguishes the two cases. A change that threw
on every unparseable body would break the deliberate error-path fallback, and a
test for the success path alone would not notice.

This module needs a browser because ``api()`` closes over the real ``fetch`` and
``Response``; it is not reachable from node without reimplementing both.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from brain.ui.app import static_dir

pytestmark = pytest.mark.browser

#: Enough of a boot for the page to evaluate its modules. The assertions never
#: read the app's state — they call ``api()`` directly — so these only exist to
#: keep boot() from throwing noise into the console.
_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": {"count": 0, "name": "", "path": "", "empty_hint": "none",
                  "children": [], "notes": []},
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
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


@pytest.fixture
def page(static_origin: str) -> Iterator[Any]:
    """A loaded page whose ``/api/broken`` answers with a body that is not JSON.

    The status is chosen by the query string so one route handler covers both
    directions: ``?status=200`` is the success path, ``?status=500`` the error
    path. Serving the SAME malformed body for both is what makes the pair a
    controlled comparison — the only variable is the status code.
    """
    sync_api = pytest.importorskip(
        "playwright.sync_api",
        reason='Playwright not installed — `pip install -e ".[browser]"`',
    )

    def route_api(route: Any) -> None:
        url = route.request.url
        path = "/" + url.split("127.0.0.1:")[-1].split("/", 1)[-1]
        if path.split("?")[0] == "/api/broken":
            status = 500 if "status=500" in url else 200
            route.fulfill(status=status, content_type="application/json",
                          body="<!doctype html><p>not json at all")
            return
        body = _STUBS.get(path.split("?")[0])
        route.fulfill(status=200 if body else 404,
                      body=json.dumps(body) if body else "{}",
                      content_type="application/json")

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page()
            pg.route("**/api/**", route_api)
            pg.goto(f"{static_origin}/static/index.html")
            yield pg
        finally:
            browser.close()


def _call(page: Any, path: str) -> dict[str, Any]:
    """Call the REAL ``api()`` and report what it did, without throwing."""
    result: dict[str, Any] = page.evaluate(
        """async (p) => {
            const mod = await import("/static/js/api.js");
            try {
                const value = await mod.api(p);
                return { threw: false, value };
            } catch (error) {
                return {
                    threw: true, message: String(error.message),
                    code: error.code || null, status: error.status || null,
                };
            }
        }""",
        arg=path,
    )
    return result


def test_a_successful_response_that_is_not_json_throws_instead_of_returning_null(
    page: Any,
) -> None:
    """The defect, as a property.

    Asserts the THROW and the code, not just "not null". A version that threw a
    bare ``TypeError`` from somewhere inside would also stop returning null and
    would be no better than the defect — the whole complaint was that the error
    did not name its cause.
    """
    result = _call(page, "/api/broken")

    assert result["threw"] is True, (
        f"api() returned {result.get('value')!r} for a 200 whose body is not "
        f"JSON. Returning null here makes every caller destructure off null and "
        f"throw a TypeError far from the cause."
    )
    assert result["code"] == "malformed_response", (
        f"the failure is not typed (code={result['code']!r}); callers cannot "
        f"tell a broken server contract from any other error"
    )
    assert result["status"] == 200
    assert "not JSON" in result["message"]


def test_an_error_response_that_is_not_json_still_falls_back_to_the_status_text(
    page: Any,
) -> None:
    """The half that must NOT change — the deliberate swallow.

    Same malformed body, only the status differs. If this reddened, the fix
    would have replaced one defect with another: an error response carrying HTML
    from a proxy is ordinary, and reporting "the body is not JSON" instead of the
    actual HTTP failure would hide the real problem behind a parsing detail.
    """
    result = _call(page, "/api/broken?status=500")

    assert result["threw"] is True
    assert result["code"] == "http_error", (
        f"an unparseable ERROR body was reported as {result['code']!r}; the "
        f"error path must keep falling back to the status, not the parse"
    )
    assert result["status"] == 500
    assert "not JSON" not in result["message"]
