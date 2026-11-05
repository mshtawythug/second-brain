"""The reading face, loaded by a real browser from the real app under the real CSP (RM-50).

**This is the one browser module that runs ``create_app``.** The others serve the
tree with ``SimpleHTTPRequestHandler``, which sends no ``Content-Security-Policy``, so
none of them can see the failure Q14 warned about: without ``font-src``, ``default-src
'none'`` blocks every ``@font-face`` silently. Here the real middleware sets the header,
and ``/api/**`` alone is stubbed, so no database is reached.

A computed ``font-family`` returns the declared list whether or not the font arrived,
so it proves nothing by itself. These tests also read the ``FontFace`` status, measure
the rendered text, and ask Chromium which font it painted with.

The refusing row strips ``font-src`` from the response and requires the same checks to
report failure, which shows they can.

It is rostered in ``ci.yml``'s ``browser`` job, which ``tests/test_ci_workflow.py``
checks. No Postgres, no Ollama.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import uvicorn

from brain.ui.app import create_app
from tests.test_ui_security import _context

pytestmark = pytest.mark.browser

FACE = "Newsreader"
SERVER_START_TIMEOUT_S = 10.0
SERVER_STOP_TIMEOUT_S = 10.0

NOTE = {
    "id": "n-font", "title": "A Synthetic Note", "tier": "vault", "content_type": "note",
    "draft": False, "tags": [], "source_kind": "manual", "vault_path": "font.md",
    "ingested_at": None, "editable": True, "movable": True, "body": "x\n",
    "body_hash": "sha256:x",
    "html": '<p id="probe">Hamburgefonstiv</p><p><em id="probe-em">Hamburgefonstiv</em></p>',
}
TREE = {
    "count": 1, "name": "", "path": "", "empty_hint": "", "children": [],
    "note_count": 1, "vault_count": 1, "ingested_count": 0,
    "notes": [{"id": "n-font", "title": "A Synthetic Note", "path": "font.md",
               "draft": False, "tier": "vault", "date": "2026-01-06"}],
}
STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": TREE,
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
    "/api/search": {"results": [], "total_documents": 0, "timing_ms": {}, "session_id": "s"},
}


@pytest.fixture(scope="module")
def app_origin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The real app under uvicorn, on a socket bound first so the origin is known."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    context = _context(tmp_path_factory.mktemp("fonts"), allowed_origin=origin)
    server = uvicorn.Server(uvicorn.Config(create_app(context), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while not server.started:
        assert time.monotonic() < deadline, "the app did not start"
        assert thread.is_alive(), "the app's server thread died during startup"
        time.sleep(0.02)
    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=SERVER_STOP_TIMEOUT_S)
        sock.close()
        assert not thread.is_alive(), "the app's server thread did not stop"


def _strip_font_src(sync_route: Any) -> None:
    """Serve the shell with its real header, minus ``font-src``."""
    response = sync_route.fetch()
    headers = dict(response.headers)
    headers["content-security-policy"] = re.sub(
        r"font-src [^;]*; ", "", headers["content-security-policy"]
    )
    sync_route.fulfill(response=response, headers=headers)


def _route_api(route: Any) -> None:
    url = route.request.url
    path = "/" + url.split("127.0.0.1:")[-1].split("/", 1)[-1].split("?")[0]
    if path.startswith("/api/notes/"):
        body: Any = NOTE
    else:
        body = STUBS.get(path)
    if body is None:
        route.fulfill(status=404, body="{}", content_type="application/json")
        return
    route.fulfill(status=200, body=json.dumps(body), content_type="application/json")


class _Opened:
    """A page with a note open, and everything it fetched."""

    def __init__(self, page: Any, requests: list[str], fonts: dict[str, tuple[int, str]]) -> None:
        self.page = page
        self.requests = requests
        self.fonts = fonts


def _open_note(
    origin: str, shell_route: Callable[[Any], None] | None = None
) -> Iterator[_Opened]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            requests: list[str] = []
            fonts: dict[str, tuple[int, str]] = {}
            page.on("request", lambda r: requests.append(r.url))

            def on_response(response: Any) -> None:
                if response.url.endswith(".woff2"):
                    fonts[response.url] = (
                        response.status, response.headers.get("content-type", "")
                    )

            page.on("response", on_response)
            page.add_init_script(
                "window.__cspViolations = [];"
                "document.addEventListener('securitypolicyviolation', e => "
                "window.__cspViolations.push(e.effectiveDirective));"
            )
            page.route("**/api/**", _route_api)
            if shell_route is not None:
                page.route(re.compile(re.escape(origin) + "/$"), shell_route)
            page.goto(f"{origin}/")
            page.wait_for_selector('[role="treeitem"]', timeout=8000)
            page.locator('[role="treeitem"]', has_text="A Synthetic Note").first.click()
            page.wait_for_selector("#probe")
            yield _Opened(page, requests, fonts)
        finally:
            browser.close()


@pytest.fixture
def opened(app_origin: str) -> Iterator[_Opened]:
    yield from _open_note(app_origin)


@pytest.fixture
def blocked(app_origin: str) -> Iterator[_Opened]:
    yield from _open_note(app_origin, _strip_font_src)


def _face_statuses(page: Any) -> dict[str, str]:
    """Each declared style's ``FontFace`` status, once the browser has settled."""
    result: dict[str, str] = page.evaluate(
        """async (face) => {
            for (const spec of [`18px "${face}"`, `italic 18px "${face}"`]) {
                try { await document.fonts.load(spec); } catch (_) { /* reported below */ }
            }
            await document.fonts.ready;
            const out = {};
            for (const f of document.fonts) {
                if (f.family.replaceAll('"', '') === face) out[f.style] = f.status;
            }
            return out;
        }""",
        FACE,
    )
    return result


def _widths(page: Any, selector: str) -> dict[str, float]:
    """The element's text width as rendered, and in its own stack with the face removed.

    The second is what the page falls back to, so the two are equal exactly when the
    face did not paint, on any host's set of system fonts.
    """
    result: dict[str, float] = page.evaluate(
        """(selector) => {
            const el = document.querySelector(selector);
            const measure = () => {
                const range = document.createRange();
                range.selectNodeContents(el);
                return range.getBoundingClientRect().width;
            };
            const rendered = measure();
            const stack = getComputedStyle(el).fontFamily;
            el.style.fontFamily = stack.slice(stack.indexOf(',') + 1);
            const fallback = measure();
            el.style.fontFamily = '';
            return {rendered, fallback};
        }""",
        selector,
    )
    return result


def _platform_fonts(page: Any, selector: str) -> list[dict[str, Any]]:
    """What Chromium actually painted ``selector`` with, not what the CSS asked for."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    cdp.send("CSS.enable")
    root = cdp.send("DOM.getDocument")["root"]["nodeId"]
    node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
    assert node, f"{selector} is not in the page"
    fonts: list[dict[str, Any]] = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]
    return fonts


def _first_family(page: Any, selector: str) -> str:
    family: str = page.evaluate(
        "(s) => getComputedStyle(document.querySelector(s)).fontFamily", selector
    )
    return family.split(",")[0].strip().strip('"')


# ---------------------------------------------------------------- it loads --


def test_the_note_body_and_title_ask_for_the_face(opened: _Opened) -> None:
    assert _first_family(opened.page, ".note-body") == FACE
    assert _first_family(opened.page, ".note-title") == FACE


def test_both_styles_load_under_the_real_csp(opened: _Opened) -> None:
    assert _face_statuses(opened.page) == {"normal": "loaded", "italic": "loaded"}
    assert opened.page.evaluate("window.__cspViolations") == []


def test_the_fonts_arrive_with_the_right_type(opened: _Opened) -> None:
    _face_statuses(opened.page)

    assert len(opened.fonts) == 2
    assert all(seen == (200, "font/woff2") for seen in opened.fonts.values()), opened.fonts


def test_the_text_is_actually_painted_in_the_face(opened: _Opened) -> None:
    _face_statuses(opened.page)

    widths = _widths(opened.page, "#probe")
    painted = _platform_fonts(opened.page, "#probe")

    assert abs(widths["rendered"] - widths["fallback"]) > 0.5, widths
    assert len(painted) == 1, painted
    assert painted[0]["isCustomFont"]
    assert painted[0]["familyName"].startswith(FACE)
    assert "italic" not in painted[0]["postScriptName"].lower()


def test_the_italic_is_the_real_italic_not_a_synthesised_slant(opened: _Opened) -> None:
    _face_statuses(opened.page)

    painted = _platform_fonts(opened.page, "#probe-em")

    assert len(painted) == 1, painted
    assert painted[0]["isCustomFont"]
    assert "italic" in painted[0]["postScriptName"].lower()


def test_every_request_stays_on_the_apps_origin(opened: _Opened, app_origin: str) -> None:
    """No external origin at runtime: the static scan's claim, observed."""
    _face_statuses(opened.page)

    elsewhere = [
        url for url in opened.requests
        if not url.startswith(f"{app_origin}/") and not url.startswith("data:")
    ]
    assert elsewhere == []


# ------------------------------------------------------------- it refuses --


def test_without_font_src_the_face_does_not_load_and_the_checks_say_so(blocked: _Opened) -> None:
    """Q14's silent failure. Every positive check above must be able to see it."""
    statuses = _face_statuses(blocked.page)
    widths = _widths(blocked.page, "#probe")
    painted = _platform_fonts(blocked.page, "#probe")

    assert statuses == {"normal": "error", "italic": "error"}
    assert "font-src" in blocked.page.evaluate("window.__cspViolations")
    assert blocked.fonts == {}
    # The computed family is unchanged, which is why it cannot be the only check.
    assert _first_family(blocked.page, ".note-body") == FACE
    assert not any(f["isCustomFont"] for f in painted)
    assert widths["rendered"] == widths["fallback"]


# ------------------------------------------------- the chrome keeps its stacks --


def test_the_page_default_is_the_system_stack(opened: _Opened) -> None:
    assert _first_family(opened.page, "body") == "ui-sans-serif"


@pytest.mark.parametrize("selector", [".tab", '[role="treeitem"]'])
def test_the_instrument_chrome_keeps_its_system_stack(opened: _Opened, selector: str) -> None:
    _face_statuses(opened.page)

    assert _first_family(opened.page, selector) == "ui-sans-serif"
    painted = _platform_fonts(opened.page, selector)
    assert painted, f"{selector} painted nothing"
    assert not any(f["isCustomFont"] for f in painted), painted
