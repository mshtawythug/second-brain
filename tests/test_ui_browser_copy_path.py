"""RM-23 — the inspector's "Copy path" button, in a real browser.

Built like ``tests/test_ui_browser_lede.py``: the real ``index.html``, ``css/``
and ``js/`` off disk, every API call stubbed with ``page.route``. No Postgres, no
Ollama. The browser context is granted ``clipboard-read`` and ``clipboard-write``
for the loopback origin, so a test can read back what the button wrote.

The insecure-context case does not spoof ``window.isSecureContext``. It loads
the same files from a non-loopback ``http://`` origin (``LAN_ORIGIN``), served
entirely by ``page.route``, so nothing resolves or leaves the machine. That is
what a ``--lan`` page is to the browser: not a secure context, and no
``navigator.clipboard``.

**The filename is load-bearing**: ``tests/test_ci_workflow.py`` requires every
module carrying the ``browser`` marker to be listed in ``ci.yml``'s browser job.
Every path and title here is synthetic.
"""
from __future__ import annotations

import json
import mimetypes
import re
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from brain.ui.app import static_dir

pytestmark = pytest.mark.browser

ABS_PATH = "/synthetic/vault/projects/copy-path-note.md"
VAULT_PATH = "projects/copy-path-note.md"
#: A non-loopback http origin. ``.test`` is reserved (RFC 2606), and every
#: request to it is answered by ``page.route``, so it is never resolved.
LAN_ORIGIN = "http://brain-lan.test:8765"
BUTTON = '.note-bar button:has-text("Copy path")'
STATUS = ".note-bar .copy-path-state"


def _note(note_id: str, **extra: Any) -> dict[str, Any]:
    """A minimal read-mode note payload. ``extra`` is merged last."""
    payload: dict[str, Any] = {
        "id": note_id,
        "title": "Copy Path Note",
        "tier": "vault",
        "content_type": "note",
        "draft": False,
        "tags": [],
        "source_kind": "manual",
        "vault_path": VAULT_PATH,
        "ingested_at": None,
        "editable": True,
        "movable": True,
        "body": "Synthetic body.\n",
        "body_hash": "sha256:x",
        "html": "<p>Synthetic body.</p>",
        "headings": [],
    }
    payload.update(extra)
    return payload


NOTES: dict[str, dict[str, Any]] = {
    # What a loopback server sends (RM-23): abs_path beside vault_path.
    "n-abs": _note("n-abs", abs_path=ABS_PATH),
    # What a LAN server sends: no abs_path, so the button copies vault_path.
    "n-rel": _note("n-rel"),
    # An ingested row with no vault mirror has nothing to copy.
    "n-none": _note("n-none", tier="ingested", vault_path=None, editable=False,
                    movable=False),
}

TREE: dict[str, Any] = {
    # count 1, not 0: the fixture waits on a treeitem to know the app booted.
    "count": 1, "name": "", "path": "", "empty_hint": "Nothing here yet.",
    "children": [],
    "notes": [{"id": "n-abs", "title": "Copy Path Note", "path": VAULT_PATH,
               "draft": False, "tier": "vault", "date": "2026-01-04"}],
}

_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": TREE,
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
}

#: Replaces ``navigator.clipboard.writeText`` with one that rejects the way a
#: denied permission does. Installed before any page script runs.
_DENY_WRITE = """
    if (navigator.clipboard) {
      navigator.clipboard.writeText = () =>
        Promise.reject(new DOMException("Write permission denied.", "NotAllowedError"));
    }
"""


@pytest.fixture(scope="module")
def static_origin() -> Iterator[str]:
    """Serve the REAL static directory over loopback http (a secure context)."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(static_dir().parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _fulfill_api(route: Any, path: str) -> None:
    match = re.fullmatch(r"/api/notes/([^/]+)", path)
    body = NOTES.get(match.group(1)) if match is not None else _STUBS.get(path)
    if body is None:
        route.fulfill(status=404, body="{}", content_type="application/json")
        return
    route.fulfill(status=200, body=json.dumps(body), content_type="application/json")


def _route_api(route: Any) -> None:
    path = "/" + route.request.url.split("://", 1)[1].split("/", 1)[1]
    _fulfill_api(route, path.split("?")[0])


def _route_lan(route: Any) -> None:
    """Serve every LAN_ORIGIN request: API stubs, else the static file off disk."""
    path = "/" + route.request.url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
    if path.startswith("/api/"):
        _fulfill_api(route, path)
        return
    root = static_dir().parent.resolve()
    target = (root / path.lstrip("/")).resolve()
    if root not in target.parents or not target.is_file():
        route.fulfill(status=404, body="")
        return
    kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix == ".js":
        kind = "text/javascript"
    route.fulfill(status=200, body=target.read_bytes(), content_type=kind)


@pytest.fixture
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip(
        "playwright.sync_api",
        reason="Playwright not installed — `pip install -e \".[browser]\"`",
    )
    with sync_api.sync_playwright() as p:
        launched = p.chromium.launch()
        try:
            yield launched
        finally:
            launched.close()


def _boot(browser: Any, origin: str, *, init_script: str | None = None) -> Any:
    """A booted app page at ``origin``, clipboard permissions granted there."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    if init_script is not None:
        context.add_init_script(init_script)
    page = context.new_page()
    if origin == LAN_ORIGIN:
        page.route(f"{LAN_ORIGIN}/**", _route_lan)
    else:
        page.route("**/api/**", _route_api)
    page.goto(f"{origin}/static/index.html")
    page.wait_for_selector('[role="treeitem"]', timeout=8000)
    return page


def _open(page: Any, note_id: str) -> None:
    page.evaluate(
        """async (id) => {
            const inspector = await import("/static/js/inspector.js");
            await inspector.openNote(id);
        }""",
        note_id,
    )
    page.wait_for_selector(".note-bar .save-state", timeout=8000)


def _clipboard(page: Any) -> str:
    text: str = page.evaluate("() => navigator.clipboard.readText()")
    return text


# ------------------------------------------------------------------- AC 1 --


def test_loopback_click_copies_the_absolute_path(browser: Any, static_origin: str) -> None:
    page = _boot(browser, static_origin)
    assert page.evaluate("() => window.isSecureContext") is True
    _open(page, "n-abs")

    page.click(BUTTON, timeout=5000)

    page.wait_for_function(
        "(sel) => document.querySelector(sel)?.textContent === 'Copied'", arg=STATUS,
        timeout=5000,
    )
    assert _clipboard(page) == ABS_PATH
    assert page.get_attribute(STATUS, "role") == "status"


def test_without_abs_path_the_button_copies_vault_path(
    browser: Any, static_origin: str
) -> None:
    """What a LAN-bound server's payload gets: the vault-relative path."""
    page = _boot(browser, static_origin)
    _open(page, "n-rel")

    page.click(BUTTON, timeout=5000)

    page.wait_for_function(
        "(sel) => document.querySelector(sel)?.textContent === 'Copied'", arg=STATUS,
        timeout=5000,
    )
    assert _clipboard(page) == VAULT_PATH


def test_a_note_with_no_path_has_no_button(browser: Any, static_origin: str) -> None:
    page = _boot(browser, static_origin)
    _open(page, "n-none")

    assert page.locator(BUTTON).count() == 0


# ------------------------------------------------------------------- AC 2 --


def test_an_insecure_lan_origin_shows_no_button(browser: Any) -> None:
    """A ``--lan`` page is plain http on a non-loopback host: no button at all."""
    page = _boot(browser, LAN_ORIGIN)
    assert page.evaluate("() => window.isSecureContext") is False
    _open(page, "n-rel")

    assert page.locator(".note-bar .save-state").count() == 1
    assert page.locator(BUTTON).count() == 0
    assert page.locator(STATUS).count() == 0


# ------------------------------------------------------------------- AC 3 --


def test_a_clipboard_failure_is_reported_in_the_status(
    browser: Any, static_origin: str
) -> None:
    page = _boot(browser, static_origin, init_script=_DENY_WRITE)
    _open(page, "n-abs")

    page.click(BUTTON, timeout=5000)

    page.wait_for_function(
        "(sel) => (document.querySelector(sel)?.textContent || '').startsWith('Copy failed')",
        arg=STATUS, timeout=5000,
    )
    assert page.text_content(STATUS) == "Copy failed (NotAllowedError)"
