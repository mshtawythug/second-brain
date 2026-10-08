"""Shared harness for the local-graph browser suites (``js/graph.js``).

Imported by ``tests/test_ui_browser_graph.py`` (painting, interaction,
accessibility, degraded states, hardening),
``tests/test_ui_browser_graph_layout.py`` (label geometry on the server's
ring) and ``tests/test_ui_browser_related.py`` (the related-notes rail, the
canonical block order, the refresh after a save). ONE copy of the stub
routing, the per-test knobs and the fixtures, so the modules cannot drift
apart.

**Deliberately not a ``test_*`` module and deliberately unmarked.** It holds no
tests, so pytest never collects it, and it carries no ``browser`` marker, so
``tests/test_ci_workflow.py`` (which discovers browser modules by the marker
in their source) does not ask the CI glob to cover it. The two modules that
import it each apply ``pytestmark = pytest.mark.browser`` themselves. Same
shape as ``tests/backup_fakes.py``: a plain helper module whose fixtures a test
module imports (``# noqa: F401``), which registers them with pytest.

Everything runs against the real ``index.html`` and the real ``css/`` and
``js/`` off disk with every API call stubbed at the network layer — no
Postgres, no Ollama, no contention for the test-database lock. The ``/graph``
payload follows the contract the server route implements: root is
``nodes[0]``, every node is pre-placed, titles are plain strings. Every id and
title here is synthetic.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from brain.ui.app import static_dir

ROOT_ID = "aaaaaaaa-0000-4000-8000-00000000000a"
ALPHA_ID = "aaaaaaaa-0000-4000-8000-00000000000b"
BRAVO_ID = "aaaaaaaa-0000-4000-8000-00000000000c"
CHARLIE_ID = "aaaaaaaa-0000-4000-8000-00000000000d"
LINKER_ID = "aaaaaaaa-0000-4000-8000-00000000000e"

ROOT_TITLE = "Synthetic Root Note"
VAULT_PATH = "projects/synthetic-root-note.md"

#: Neighbours in the order the server sends them — the anchors must follow it.
NEIGHBOURS: list[tuple[str, str, str]] = [
    (ALPHA_ID, "Alpha Synthetic Note", "vault"),
    (BRAVO_ID, "Bravo Synthetic Note With A Deliberately Long Title", "ingested"),
    (CHARLIE_ID, "Charlie Synthetic Note", "vault"),
]

#: Edges chosen so hovering ALPHA touches exactly two lines (root-alpha and
#: alpha-charlie) and leaves two untouched (bravo-root, root-charlie). The
#: bravo edge is written dst-first so the hover match must consider BOTH ends.
#: All three server kinds appear (``links.link_kind`` allows ``embed``), so the
#: client is proven to pass the kind through rather than collapse it.
EDGES: list[dict[str, str]] = [
    {"src": ROOT_ID, "dst": ALPHA_ID, "kind": "wiki"},
    {"src": BRAVO_ID, "dst": ROOT_ID, "kind": "derived"},
    {"src": ROOT_ID, "dst": CHARLIE_ID, "kind": "embed"},
    {"src": ALPHA_ID, "dst": CHARLIE_ID, "kind": "derived"},
]

_RING = [(160.0, 60.0), (247.0, 210.0), (73.0, 210.0)]


def graph_payload(
    *,
    neighbours: list[tuple[str, str, str]] | None = None,
    edges: list[dict[str, str]] | None = None,
    truncated: int = 0,
    corpus_linked: bool = True,
) -> dict[str, Any]:
    """A ``/api/notes/{id}/graph`` body in the contract's exact shape."""
    chosen = NEIGHBOURS if neighbours is None else neighbours
    nodes: list[dict[str, Any]] = [{
        "id": ROOT_ID, "title": ROOT_TITLE, "kind": "vault",
        "x": 160.0, "y": 160.0, "r": 9, "root": True,
    }]
    for (node_id, title, kind), (x, y) in zip(chosen, _RING, strict=False):
        nodes.append({"id": node_id, "title": title, "kind": kind,
                      "x": x, "y": y, "r": 6, "root": False})
    return {
        "id": ROOT_ID, "width": 320, "height": 320, "nodes": nodes,
        "edges": EDGES if edges is None else edges,
        "truncated": truncated, "corpus_linked": corpus_linked,
    }


#: Headings so the marginalia renders: the last-child test needs a SECOND
#: appender present, or "the figure is last" is vacuously true.
NOTE_PAYLOAD: dict[str, Any] = {
    "id": ROOT_ID,
    "title": ROOT_TITLE,
    "tier": "vault",
    "content_type": "note",
    "draft": False,
    "tags": [],
    "source_kind": "manual",
    "vault_path": VAULT_PATH,
    "ingested_at": None,
    "editable": True,
    "movable": True,
    "body": f"# {ROOT_TITLE}\n\n## Scope\n",
    "body_hash": "sha256:x",
    "html": '<h2 id="scope">Scope</h2><p>Synthetic prose.</p>',
    "headings": [{"level": 2, "text": "Scope", "id": "scope"}],
}

LINKS_PAYLOAD: dict[str, Any] = {
    "id": ROOT_ID,
    "backlinks": [{"id": LINKER_ID, "title": "Synthetic Linker", "kind": "vault",
                   "link_text": "linker", "link_kind": "wiki",
                   "rule": None, "weight": None}],
    "outgoing": [],
    "counts": {"backlinks": 1, "outgoing": 0},
}

TREE: dict[str, Any] = {
    "count": 1, "name": "", "path": "", "empty_hint": "Nothing here yet.",
    "children": [],
    "notes": [{"id": ROOT_ID, "title": ROOT_TITLE, "path": VAULT_PATH,
               "draft": False, "tier": "vault", "date": "2026-01-04"}],
}

#: Each note answers with its OWN title, so a test can tell which note the
#: inspector is showing.
_TITLES: dict[str, str] = {node_id: title for node_id, title, _ in NEIGHBOURS}

_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": TREE,
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
    "/api/search": {"results": [], "total_documents": 0,
                    "timing_ms": {"total": 1, "embed": 0, "sql": 1},
                    "session_id": "s-synthetic"},
}

#: Neighbours for ``/api/notes/{id}/related`` — ``(id, title, snippet,
#: snippet_truncated)``. Synthetic, like everything here.
RELATED: list[tuple[str, str, str, bool]] = [
    (ALPHA_ID, "Alpha Synthetic Note", "A synthetic snippet about alpha.", False),
    (CHARLIE_ID, "Charlie Synthetic Note", "A synthetic snippet that was cut", True),
]


def related_payload(
    rows: list[tuple[str, str, str, bool]] | None = None, *, note_id: str = ROOT_ID,
) -> dict[str, Any]:
    """A ``/api/notes/{id}/related`` body in the contract's exact shape."""
    chosen = RELATED if rows is None else rows
    return {
        "id": note_id,
        "related": [
            {"id": rid, "title": title, "vault_path": f"synthetic/{rid[-2:]}.md",
             "source": "vault", "score": 0.5, "snippet": snippet,
             "snippet_truncated": truncated}
            for rid, title, snippet, truncated in chosen
        ],
        "count": len(chosen),
        "vector_sim_floor": 0.25,
    }


#: Per-test knobs, reset by the ``page`` fixture so nothing leaks between tests.
#:
#: ``_RELATED`` DEFAULTS TO 404, i.e. "not stubbed", and that is deliberate: the
#: graph suites predate the related rail, and a default rail would change what
#: their structural assertions (e.g. which block is last) are measuring. A test
#: that wants the rail sets ``_RELATED["status"] = 200``.
#:
#: ``_HOLD`` names the supplementary routes (``"links"``, ``"graph"``,
#: ``"related"``) whose responses are NOT fulfilled on arrival but parked in
#: ``_HELD`` as ``(kind, release)`` pairs, so a test can deliver them in an order
#: of its choosing — see :func:`release_held`.
_GRAPH: dict[str, Any] = {}
_LINKS: dict[str, Any] = {}
_RELATED: dict[str, Any] = {}
_NOTE_EXTRA: dict[str, Any] = {}
_HOLD: set[str] = set()
_HELD: list[tuple[str, Any]] = []
_PUTS: list[dict[str, Any]] = []
_REQUESTS: list[str] = []
_ERRORS: list[str] = []

#: Chromium logs every non-2xx fetch as a console error of this form. It is the
#: browser reporting the network, not the app throwing, so it is not counted.
_NETWORK_NOISE = "Failed to load resource"


#: Fixture FUNCTIONS are named ``_..._fixture`` and registered under their
#: public names with ``name=``: a test module imports the function, which
#: registers the fixture, and its tests take ``page`` as a parameter without
#: that parameter shadowing an imported name (ruff F811).
@pytest.fixture(scope="module", name="static_origin")
def _static_origin_fixture() -> Iterator[str]:
    """Serve the PACKAGE dir over HTTP (index.html uses absolute /static/ paths)."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(static_dir().parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _route_api(route: Any) -> None:
    path = "/" + route.request.url.split("127.0.0.1:")[-1].split("/", 1)[-1]
    path = path.split("?")[0]
    _REQUESTS.append(path)

    def ok(body: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    supplementary = re.fullmatch(r"/api/notes/[^/]+/(graph|links|related)", path)
    if supplementary is not None:
        kind = supplementary.group(1)
        # The body is decided NOW, at request time, from the knobs as they are
        # when the request arrives — a held response must not pick up a knob a
        # test changes while it is parked.
        respond = partial(_fulfil_supplementary, route, kind, _supplementary_reply(kind))
        if kind in _HOLD:
            _HELD.append((kind, respond))
        else:
            respond()
        return
    match = re.fullmatch(r"/api/notes/([^/]+)", path)
    if match is not None and route.request.method == "PUT":
        # The save path (inspector.js saveNote): PUT {body_hash, body} ->
        # {body_hash, html}. Recorded so a test can prove the save happened.
        _PUTS.append(json.loads(route.request.post_data or "{}"))
        ok({"body_hash": f"sha256:saved-{len(_PUTS)}",
            "html": "<p>Synthetic saved prose.</p>"})
        return
    if match is not None:
        note_id = match.group(1)
        ok({**NOTE_PAYLOAD, **_NOTE_EXTRA, "id": note_id,
            "title": _TITLES.get(note_id, ROOT_TITLE)})
        return
    body = _STUBS.get(path)
    if body is None:
        route.fulfill(status=404, body="{}", content_type="application/json")
        return
    ok(body)


def _supplementary_reply(kind: str) -> tuple[int, Any]:
    """``(status, body)`` for a ``/graph``, ``/links`` or ``/related`` request."""
    knob = {"graph": _GRAPH, "links": _LINKS, "related": _RELATED}[kind]
    status = knob["status"]
    if status == 200:
        return 200, knob["payload"]
    # 403 is each route's confidential-note refusal; anything else is a failure.
    code = f"{kind}_withheld" if status == 403 else "database_unavailable"
    if status == 404:
        code = "not_found"
    return status, {"error": {"code": code, "message": "synthetic failure"}}


def _fulfil_supplementary(route: Any, kind: str, reply: tuple[int, Any]) -> None:
    status, body = reply
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def wait_for_held(page: Any, count: int, timeout_ms: int = 8000) -> None:
    """Pump the page until ``count`` responses are parked in ``_HELD``.

    The route handler runs on Playwright's dispatcher, which only turns while
    the test thread is inside a Playwright call — so this waits with
    ``page.wait_for_timeout`` (a Playwright call) rather than ``time.sleep``.
    """
    waited = 0
    while len(_HELD) < count:
        assert waited < timeout_ms, (
            f"only {len(_HELD)} of {count} held responses arrived: "
            f"{[kind for kind, _ in _HELD]}"
        )
        page.wait_for_timeout(20)
        waited += 20


def settle(page: Any) -> None:
    """Two animation frames: a delivered tiny response's `.then` chain and any
    re-render it triggers have run by the second frame."""
    page.evaluate(
        "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


def release_held(page: Any, order: list[str]) -> None:
    """Deliver the parked responses in ``order`` (by kind), settling after each,
    so each response is fully handled before the next one is delivered."""
    by_kind = {kind: respond for kind, respond in _HELD}
    assert sorted(by_kind) == sorted(order), (
        f"held {sorted(by_kind)} but asked to release {order}"
    )
    for kind in order:
        with page.expect_response(re.compile(rf"/api/notes/[^/]+/{kind}$")):
            by_kind[kind]()
        settle(page)
    _HELD.clear()


@pytest.fixture(name="page")
def _page_fixture(static_origin: str) -> Iterator[Any]:
    """A booted app page with every API call stubbed and errors recorded."""
    sync_api = pytest.importorskip(
        "playwright.sync_api",
        reason=(
            "Playwright not installed — `pip install -e \".[browser]\"` "
            "(browsers cache outside the venv, so this is a small install)"
        ),
    )
    _GRAPH.clear()
    _GRAPH.update({"status": 200, "payload": graph_payload()})
    _LINKS.clear()
    _LINKS.update({"status": 200, "payload": LINKS_PAYLOAD})
    _RELATED.clear()
    _RELATED.update({"status": 404, "payload": related_payload()})
    _NOTE_EXTRA.clear()
    _HOLD.clear()
    _HELD.clear()
    _PUTS.clear()
    _REQUESTS.clear()
    _ERRORS.clear()

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": 1280, "height": 900})
            pg.on("pageerror", lambda exc: _ERRORS.append(f"pageerror: {exc}"))
            pg.on("console", lambda msg: (
                _ERRORS.append(f"console: {msg.text}")
                if msg.type == "error" and _NETWORK_NOISE not in msg.text else None
            ))
            pg.route("**/api/**", _route_api)
            pg.goto(f"{static_origin}/static/index.html")
            pg.wait_for_selector('[role="treeitem"]', timeout=8000)
            yield pg
        finally:
            browser.close()


def _open(page: Any, note_id: str = ROOT_ID) -> None:
    """Open a note through the REAL inspector, and wait until the /graph
    response has been delivered AND handled.

    "Handled" is settled by two animation frames after the response: the
    response body is tiny and its `.then` is a microtask chain, so by the
    second frame the cache is populated and any re-render has run. Then a bare
    dispatch re-renders synchronously from that cache, so every assertion that
    follows reads a state the module has fully decided — including the
    "draws nothing" cases, where there is no element to wait for.
    """
    with page.expect_response(re.compile(r"/api/notes/[^/]+/graph$")):
        page.evaluate(
            """async (id) => {
                const inspector = await import("/static/js/inspector.js");
                await inspector.openNote(id);
            }""",
            note_id,
        )
    page.wait_for_selector(".note-body")
    page.evaluate(
        """async () => {
            await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
            const store = await import("/static/js/store.js");
            store.dispatch({});
        }"""
    )


def _dispatch(page: Any, patch: str) -> None:
    page.evaluate(
        """async (patch) => {
            const store = await import("/static/js/store.js");
            store.dispatch(JSON.parse(patch));
        }""",
        patch,
    )

