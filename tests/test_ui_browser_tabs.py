"""Phase 8 T6 (E10): every remaining tab switches, in a real browser, without an error.

A pane check alone is not enough, and that is the reason for this module.
``wireTabs`` loops over a name list. If the Publish section were deleted but
``"publish"`` left in that list, each click would switch the panes it reaches
FIRST and then throw on ``$("tab-publish")`` being null. "publish" was the last
name, so every pane assertion would still pass. Only the ``pageerror`` /
console-error assertion sees it, so this module collects both, the way
``tests/ui_graph_harness.py`` does.
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

#: Just enough of a boot for the page to load cleanly (``test_ui_browser_api``'s set).
_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": {"count": 0, "name": "", "path": "", "empty_hint": "none",
                  "children": [], "notes": []},
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
}

#: Chromium logs every non-2xx fetch as a console error; that is the network, not the app.
_NETWORK_NOISE = "Failed to load resource"

_TABS = ("notes", "ingest", "agent")


@pytest.fixture(scope="module", name="static_origin")
def _static_origin_fixture() -> Iterator[str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(static_dir().parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _route_api(route: Any) -> None:
    path = "/" + route.request.url.split("127.0.0.1:")[-1].split("/", 1)[-1]
    body = _STUBS.get(path.split("?")[0])
    route.fulfill(
        status=200 if body is not None else 404,
        body=json.dumps(body) if body is not None else "{}",
        content_type="application/json",
    )


def test_every_tab_switches_and_the_page_raises_nothing(static_origin: str) -> None:
    sync_api = pytest.importorskip(
        "playwright.sync_api",
        reason='Playwright not installed — `pip install -e ".[browser]"`',
    )
    errors: list[str] = []
    shown: dict[str, list[str]] = {}

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page()
            pg.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            pg.on("console", lambda msg: (
                errors.append(f"console: {msg.text}")
                if msg.type == "error" and _NETWORK_NOISE not in msg.text else None
            ))
            pg.route("**/api/**", _route_api)
            pg.goto(f"{static_origin}/static/index.html")
            pg.wait_for_load_state("networkidle")

            buttons = pg.eval_on_selector_all(".tab", "els => els.map(e => e.dataset.tab)")
            for name in [*_TABS[1:], _TABS[0]]:  # away from Notes, then back to it
                pg.click(f'.tab[data-tab="{name}"]')
                shown[name] = pg.eval_on_selector_all(
                    '[id^="tab-"]', "els => els.filter(e => !e.hidden).map(e => e.id)"
                )
        finally:
            browser.close()

    assert buttons == list(_TABS), buttons
    for name in _TABS:
        assert shown[name] == [f"tab-{name}"], (name, shown[name])
    assert errors == [], f"the page raised while switching tabs: {errors}"
