"""The sidebar local graph (``js/graph.js``), EXECUTED in a real browser.

Same construction as ``tests/test_ui_browser.py`` and
``tests/test_ui_browser_reading.py``: the real ``index.html``, the real
``css/`` and ``js/`` off disk, every API call stubbed at the network layer. No
Postgres, no Ollama, no contention for the test-database lock. The fixture is
copied from ``test_ui_browser_reading.py`` rather than imported, which is the
convention every ``test_ui_browser*.py`` module follows.

**The filename is load-bearing.** CI selects ``tests/test_ui_browser*.py`` by
path (a bare ``-m browser`` collects modules that open a database connection at
import time), and ``tests/test_ci_workflow.py`` fails if a ``browser``-marked
module sits outside that glob. Run it by path:

    pytest tests/test_ui_browser_graph.py -m browser --no-cov

**UNLIKE THE MARGINALIA TESTS, THIS FILE DOES NOT MOUNT THE MODULE ITSELF.**
``test_ui_browser_reading.py`` calls ``wireMarginalia()`` from
``page.evaluate``, which is why it cannot see a mis-wired ``boot()``. Here the
graph is reached ONLY through ``js/main.js``'s ``boot()``, so the last-child
test is a real oracle for the wiring order: ``wireGraph()`` must be registered
after ``wireMarginalia()``, or the marginalia aside is appended after the
figure on every dispatch that rebuilds the inspector.

The ``/graph`` payload follows the contract the server route implements: root
is ``nodes[0]``, every node is pre-placed, titles are plain strings. Every id
and title here is synthetic.
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

pytestmark = pytest.mark.browser

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

#: Per-test knobs, reset by the ``page`` fixture so nothing leaks between tests.
_GRAPH: dict[str, Any] = {}
_NOTE_EXTRA: dict[str, Any] = {}
_REQUESTS: list[str] = []
_ERRORS: list[str] = []

#: Chromium logs every non-2xx fetch as a console error of this form. It is the
#: browser reporting the network, not the app throwing, so it is not counted.
_NETWORK_NOISE = "Failed to load resource"


@pytest.fixture(scope="module")
def static_origin() -> Iterator[str]:
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

    if re.fullmatch(r"/api/notes/[^/]+/graph", path) is not None:
        if _GRAPH["status"] != 200:
            # 403 is the route's confidential-note refusal; 500 is any failure.
            code = "graph_withheld" if _GRAPH["status"] == 403 else "database_unavailable"
            route.fulfill(
                status=_GRAPH["status"], content_type="application/json",
                body=json.dumps({"error": {"code": code, "message": "synthetic failure"}}),
            )
            return
        ok(_GRAPH["payload"])
        return
    if re.fullmatch(r"/api/notes/[^/]+/links", path) is not None:
        ok(LINKS_PAYLOAD)
        return
    match = re.fullmatch(r"/api/notes/([^/]+)", path)
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


@pytest.fixture
def page(static_origin: str) -> Iterator[Any]:
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
    _NOTE_EXTRA.clear()
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


def _neighbour_ids(page: Any) -> list[str]:
    return page.eval_on_selector_all(
        ".local-graph svg a.node", "els => els.map(e => e.getAttribute('data-note-id'))"
    )


# ---------------------------------------------------------------- painting --


def test_opening_a_note_paints_the_root_and_every_neighbour_in_order(page: Any) -> None:
    """(1) Guard the guard: every test below is vacuous if nothing painted."""
    _open(page)
    page.wait_for_selector(".local-graph svg")

    figure = page.locator("figure.local-graph")
    assert figure.count() == 1
    assert figure.get_attribute("aria-label") == "Links around this note"
    assert page.locator(".local-graph svg").get_attribute("viewBox") == "0 0 320 320"
    assert page.locator(".local-graph g.node-root").count() == 1
    assert page.locator(".local-graph g.node-root").get_attribute("data-note-id") == ROOT_ID
    assert page.locator(".local-graph g.node-root").get_attribute("data-kind") == "vault", (
        "the root carries no data-kind, so an ingested root would not style as one"
    )
    assert _neighbour_ids(page) == [n[0] for n in NEIGHBOURS], (
        "neighbour anchors are missing or out of the order the server sent"
    )
    hrefs = page.eval_on_selector_all(
        ".local-graph svg a.node", "els => els.map(e => e.getAttribute('href'))"
    )
    assert hrefs == [f"?id={n[0]}" for n in NEIGHBOURS]

    # Edges are drawn FIRST, under every node, and carry their kind.
    first_tag = page.evaluate(
        "() => document.querySelector('.local-graph svg').firstElementChild.tagName"
    )
    assert first_tag == "line", f"the first svg child is <{first_tag}>, not an edge"
    kinds = page.eval_on_selector_all(
        ".local-graph line.edge", "els => els.map(e => e.getAttribute('data-kind'))"
    )
    assert kinds == [e["kind"] for e in EDGES]
    assert page.locator(
        f'.local-graph line.edge[data-src="{ROOT_ID}"][data-dst="{CHARLIE_ID}"]'
    ).get_attribute("data-kind") == "embed", "an embed edge lost its kind"

    dash = page.evaluate(
        """() => getComputedStyle(
            document.querySelector('.local-graph line.edge[data-kind="derived"]')
        ).strokeDasharray"""
    )
    assert dash not in ("", "none"), "a derived edge is drawn solid"


def test_a_long_title_is_cut_on_the_label_and_whole_in_the_tooltip(page: Any) -> None:
    _open(page)
    page.wait_for_selector(".local-graph svg")
    _, long_title, _ = NEIGHBOURS[1]
    anchor = f'.local-graph a.node[data-note-id="{BRAVO_ID}"]'
    label = page.locator(f"{anchor} > text").text_content()
    tip = page.locator(f"{anchor} > title").text_content()
    assert label == long_title[:28] + "…"
    assert tip == long_title


# ------------------------------------------------------------- interaction --


def test_clicking_a_neighbour_opens_that_note_without_reloading(page: Any) -> None:
    """(2) The click goes through openNote IN PLACE — no navigation.

    ``expect_request`` alone cannot tell the two apart: a full navigation to
    ``?id=CHARLIE`` makes boot() request the very same note. So a marker is
    planted on ``window`` first; a reload would wipe it. (``_REQUESTS`` is not
    read here: the route handler appends to it on Playwright's side and can run
    after the context manager has already returned — that race made the
    previous form of this test fail 3 runs in 6.)
    """
    _open(page)
    page.wait_for_selector(".local-graph svg")
    page.evaluate("() => { window.__noReload = 1; }")
    with page.expect_request(re.compile(rf"/api/notes/{CHARLIE_ID}$")):
        page.click(f'.local-graph a.node[data-note-id="{CHARLIE_ID}"] circle')
    charlie_title = NEIGHBOURS[2][1]
    page.wait_for_function(
        "(t) => document.querySelector('#inspector h1.note-title')?.textContent === t",
        arg=charlie_title,
    )
    assert page.evaluate("() => window.__noReload") == 1, (
        "clicking a graph node reloaded the page instead of opening the note in place"
    )


def test_hovering_a_neighbour_highlights_it_and_only_its_edges(page: Any) -> None:
    """(3) Exactly the hovered node and the edges touching it, nothing else."""
    _open(page)
    page.wait_for_selector(".local-graph svg")
    page.hover(f'.local-graph a.node[data-note-id="{ALPHA_ID}"] circle')

    hovered_nodes = page.eval_on_selector_all(
        ".local-graph .node[data-hover]", "els => els.map(e => e.getAttribute('data-note-id'))"
    )
    assert hovered_nodes == [ALPHA_ID]
    hovered_edges = page.eval_on_selector_all(
        ".local-graph line.edge[data-hover]",
        "els => els.map(e => [e.getAttribute('data-src'), e.getAttribute('data-dst')])",
    )
    assert hovered_edges == [[ROOT_ID, ALPHA_ID], [ALPHA_ID, CHARLIE_ID]]

    page.mouse.move(1, 1)
    assert page.locator(".local-graph [data-hover]").count() == 0, (
        "the highlight outlived the hover"
    )


def test_keyboard_focus_highlights_a_neighbour_and_blur_clears_it(page: Any) -> None:
    """(3b) Focus is the keyboard's hover: same marking, removed on blur.

    BRAVO is chosen because its one edge is written dst-first, so the match must
    consider both ends here too. The mouse never moves in this test, so any
    ``data-hover`` can only have come from the focus listener.
    """
    _open(page)
    page.wait_for_selector(".local-graph svg")
    page.focus(f'.local-graph a.node[data-note-id="{BRAVO_ID}"]')

    hovered_nodes = page.eval_on_selector_all(
        ".local-graph .node[data-hover]", "els => els.map(e => e.getAttribute('data-note-id'))"
    )
    assert hovered_nodes == [BRAVO_ID], (
        f"focus did not mark exactly the focused node: {hovered_nodes}"
    )
    hovered_edges = page.eval_on_selector_all(
        ".local-graph line.edge[data-hover]",
        "els => els.map(e => [e.getAttribute('data-src'), e.getAttribute('data-dst')])",
    )
    assert hovered_edges == [[BRAVO_ID, ROOT_ID]]

    page.evaluate("() => document.activeElement.blur()")
    assert page.locator(".local-graph [data-hover]").count() == 0, (
        "the focus highlight outlived the blur"
    )


def _lit(page: Any) -> list[str | None]:
    return page.eval_on_selector_all(
        ".local-graph .node[data-hover]", "els => els.map(e => e.getAttribute('data-note-id'))"
    )


def test_focusing_another_node_while_hovering_lights_only_the_focused_one(
    page: Any,
) -> None:
    """(3c) Pointer and keyboard share ONE highlight, never two.

    Hover ALPHA, then focus BRAVO: only BRAVO (and its one edge) is lit. Then
    the mouse leaves ALPHA: BRAVO, which still has focus, must STAY lit.
    """
    _open(page)
    page.wait_for_selector(".local-graph svg")
    page.hover(f'.local-graph a.node[data-note-id="{ALPHA_ID}"] circle')
    assert _lit(page) == [ALPHA_ID], "precondition: the hover did not light ALPHA"

    page.focus(f'.local-graph a.node[data-note-id="{BRAVO_ID}"]')
    assert _lit(page) == [BRAVO_ID], (
        f"hover and focus are both lit: {_lit(page)}; focus must replace the hover"
    )
    lit_edges = page.eval_on_selector_all(
        ".local-graph line.edge[data-hover]",
        "els => els.map(e => [e.getAttribute('data-src'), e.getAttribute('data-dst')])",
    )
    assert lit_edges == [[BRAVO_ID, ROOT_ID]]

    page.mouse.move(1, 1)
    assert _lit(page) == [BRAVO_ID], (
        "the mouse leaving ALPHA darkened BRAVO, which still has keyboard focus"
    )


# ----------------------------------------------------------- accessibility --


def test_every_neighbour_is_a_link_named_by_its_full_title(page: Any) -> None:
    """(11) The accessibility tree, not the DOM.

    An ``img`` role makes its descendants presentational, so links inside an
    ``role="img"`` svg can drop out of the tree — and an unnamed img is a
    defect of its own. The svg must be a NAMED group, and every neighbour a
    link whose name is its whole title (from the ``<title>`` child, not the
    cut label).
    """
    _open(page)
    page.wait_for_selector(".local-graph svg")
    snapshot = page.locator("figure.local-graph").aria_snapshot()

    for _, title, _ in NEIGHBOURS:
        assert f'link "{title}"' in snapshot, (
            f"neighbour {title!r} is not a link named by its full title:\n{snapshot}"
        )
    assert re.search(r"^\s*- img\s*:?\s*$", snapshot, re.M) is None, (
        f"an unnamed img is in the accessibility tree:\n{snapshot}"
    )
    assert re.search(r"^\s*- img\b", snapshot, re.M) is None, (
        f"the graph is exposed as an img, which hides its links:\n{snapshot}"
    )
    assert 'group "Links around this note"' in snapshot, (
        f"the svg is not a group named for the graph:\n{snapshot}"
    )


def test_an_empty_title_is_called_untitled_on_the_label_and_the_link(page: Any) -> None:
    """(12) A blank title would leave a nameless link and an empty label."""
    _GRAPH["payload"] = graph_payload(
        neighbours=[(ALPHA_ID, "", "vault")],
        edges=[{"src": ROOT_ID, "dst": ALPHA_ID, "kind": "wiki"}],
    )
    _open(page)
    page.wait_for_selector(".local-graph svg")
    anchor = f'.local-graph a.node[data-note-id="{ALPHA_ID}"]'
    assert page.locator(f"{anchor} > text").text_content() == "Untitled"
    assert page.locator(f"{anchor} > title").text_content() == "Untitled"
    assert 'link "Untitled"' in page.locator("figure.local-graph").aria_snapshot()


# ------------------------------------------------------------------ layout --


#: Every neighbour gets a label at the 28-char cut, so the widest possible
#: label sits on each ring position, including the two near the rim.
_LONG_NEIGHBOURS: list[tuple[str, str, str]] = [
    (ALPHA_ID, "Alpha Synthetic Note With A Long Title", "vault"),
    (BRAVO_ID, "Bravo Synthetic Note With A Long Title", "vault"),
    (CHARLIE_ID, "Charlie Synthetic Note With A Long Title", "vault"),
]

#: Below 780px the ledger and inspector become a two-view stack
#: (components.css), so on a 320px phone the inspector is the whole 320px with
#: the narrow padding — the narrowest it ever gets.
_NARROW_VIEWPORT = {"width": 320, "height": 800}


def test_long_labels_do_not_scroll_a_narrow_inspector_sideways(page: Any) -> None:
    """(13) Labels near the rim neither side-scroll nor get cut by the inspector.

    TWO assertions, because the first one ALONE CANNOT FAIL on label spill —
    measured: re-anchoring every label to ``start`` (so the right-hand ones run
    far past the svg) left ``scrollWidth == clientWidth``. Text overflowing an
    ``overflow: visible`` svg is INK overflow in Chromium, not scrollable
    overflow, so it never widens the scroll box. What it does instead is get
    CLIPPED at the inspector's edge (``overflow-y: auto`` clips both axes). So
    the property a reader would notice — every label wholly inside the visible
    inspector — is asserted directly, and the scroll check stays as the cheap
    half for anything that IS scrollable overflow.
    """
    page.set_viewport_size(_NARROW_VIEWPORT)
    _GRAPH["payload"] = graph_payload(neighbours=_LONG_NEIGHBOURS)
    _open(page)
    page.wait_for_selector(".local-graph svg")

    width, scroll, client = page.evaluate(
        """() => {
            const i = document.getElementById('inspector');
            return [i.getBoundingClientRect().width, i.scrollWidth, i.clientWidth];
        }"""
    )
    assert width <= 340, f"precondition: the inspector is {width}px wide, not ~320px"
    assert scroll <= client, (
        f"the inspector scrolls sideways: scrollWidth {scroll} > clientWidth {client}"
    )

    spill = page.evaluate(
        """() => {
            const box = document.getElementById('inspector').getBoundingClientRect();
            return [...document.querySelectorAll('.local-graph text')]
              .map((t) => [t.textContent, t.getBoundingClientRect()])
              .filter(([, r]) => r.left < box.left || r.right > box.right)
              .map(([text, r]) => `${text}: ${Math.round(r.left)}..${Math.round(r.right)}`
                                  + ` outside ${Math.round(box.left)}..${Math.round(box.right)}`);
        }"""
    )
    assert page.locator(".local-graph text").count() == 1 + len(_LONG_NEIGHBOURS), (
        "precondition: not every label was drawn"
    )
    assert spill == [], f"labels run past the inspector and are clipped: {spill}"


# ---------------------------------------------------------- degraded states --


def test_truncated_neighbours_are_counted_in_a_caption(page: Any) -> None:
    """(4)"""
    _GRAPH["payload"] = graph_payload(truncated=2)
    _open(page)
    page.wait_for_selector(".local-graph figcaption")
    assert page.locator(".local-graph figcaption").text_content() == "+2 more not shown"


def test_no_caption_when_nothing_was_truncated(page: Any) -> None:
    _open(page)
    page.wait_for_selector(".local-graph svg")
    assert page.locator(".local-graph figcaption").count() == 0


def test_an_unlinked_vault_names_the_command_that_links_it(page: Any) -> None:
    """(5a) One node in a vault with no links at all: say how to fix it."""
    _GRAPH["payload"] = graph_payload(neighbours=[], edges=[], corpus_linked=False)
    _open(page)
    page.wait_for_selector(".local-graph .graph-empty")
    assert "brain vault sync" in page.locator(".local-graph .graph-empty").text_content()
    assert page.locator(".local-graph svg").count() == 0


def test_an_isolated_note_in_a_linked_vault_draws_nothing(page: Any) -> None:
    """(5b) Not an error, and an empty canvas would look like one."""
    _GRAPH["payload"] = graph_payload(neighbours=[], edges=[], corpus_linked=True)
    _open(page)
    assert page.locator(".note-body").count() == 1
    assert page.locator(".local-graph").count() == 0, (
        "an isolated note in a linked vault rendered graph chrome"
    )


# ----------------------------------------------------------------- wiring --


def test_edit_mode_removes_the_graph_and_leaving_restores_it(page: Any) -> None:
    """(6)"""
    _open(page)
    page.wait_for_selector(".local-graph svg")
    _dispatch(page, '{"editing": true}')
    page.wait_for_selector(".editor")
    assert page.locator(".local-graph").count() == 0
    _dispatch(page, '{"editing": false}')
    page.wait_for_selector(".note-body")
    assert page.locator(".local-graph svg").count() == 1


def test_the_graph_is_the_last_child_of_the_inspector(page: Any) -> None:
    """(7) The wiring-order oracle. Reached through boot() only — see header."""
    _open(page)
    page.wait_for_selector(".local-graph svg")
    # Wait out the backlinks fetch: it re-renders the marginalia asynchronously,
    # and this test is about the synchronous order of a full dispatch.
    page.wait_for_selector(".marginalia .backlinks-rail a")
    _dispatch(page, "{}")

    children = page.evaluate(
        "() => [...document.getElementById('inspector').children].map(c => c.className)"
    )
    assert any("marginalia" in c for c in children), (
        f"precondition: the marginalia is not mounted ({children}), so 'last' "
        "would be vacuously true"
    )
    assert "local-graph" in children[-1], (
        f"#inspector children are {children}; the graph must be LAST. "
        "wireGraph() was registered before wireMarginalia() in boot()."
    )


# --------------------------------------------------------------- hardening --


def test_a_markup_title_renders_as_literal_text(page: Any) -> None:
    """(8) The XSS guard: a title is text, never parsed."""
    _GRAPH["payload"] = graph_payload(
        neighbours=[(ALPHA_ID, "<b>x</b>", "vault")],
        edges=[{"src": ROOT_ID, "dst": ALPHA_ID, "kind": "wiki"}],
    )
    _open(page)
    page.wait_for_selector(".local-graph svg")
    anchor = f'.local-graph a.node[data-note-id="{ALPHA_ID}"]'
    assert page.locator(f"{anchor} > text").text_content() == "<b>x</b>"
    assert page.locator(f"{anchor} > title").text_content() == "<b>x</b>"
    assert page.locator(".local-graph b").count() == 0, "a title was parsed as markup"


@pytest.mark.parametrize("status", [500, 403], ids=["server-error", "graph-withheld"])
def test_a_failed_graph_request_leaves_the_note_readable_and_silent(
    page: Any, status: int,
) -> None:
    """(9) A failed /graph: note intact, no figure, no toast, no uncaught error.

    403 ``graph_withheld`` is the server refusing the graph of a confidential
    note. The client treats it exactly like any other failure — and it is the
    case where drawing anything at all would be a leak, not just noise.
    """
    _GRAPH["status"] = status
    _open(page)
    assert page.locator(".note-body").count() == 1
    assert page.locator(".local-graph").count() == 0
    assert page.locator("#toast").is_hidden(), "a toast fired for a supplementary fetch"
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"
    assert any(r.endswith("/graph") for r in _REQUESTS), (
        "precondition: /graph was never requested, so the failure path never ran"
    )


def test_a_withheld_note_never_requests_its_graph(page: Any) -> None:
    """(10) A withheld note: the client does not even ASK for its graph.

    The server would refuse with 403 (covered above); this pins the client half
    of the same rule, so the graph of a withheld note is never fetched at all.
    """
    _NOTE_EXTRA["withheld"] = "Synthetic withheld notice."
    page.evaluate(
        """async (id) => {
            const inspector = await import("/static/js/inspector.js");
            await inspector.openNote(id);
        }""",
        ROOT_ID,
    )
    page.wait_for_selector("#inspector p.withheld")
    page.evaluate(
        """async () => {
            await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
            const store = await import("/static/js/store.js");
            store.dispatch({});
        }"""
    )
    assert f"/api/notes/{ROOT_ID}" in _REQUESTS, (
        "precondition: the note itself was never requested"
    )
    assert not any(r.endswith("/graph") for r in _REQUESTS), (
        f"/graph was requested for a withheld note: {_REQUESTS}"
    )
    assert page.locator(".local-graph").count() == 0
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"
