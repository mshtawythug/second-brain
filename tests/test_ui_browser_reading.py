"""The reading surfaces — TOC, breadcrumbs — EXECUTED in a real browser.

Third harness alongside ``tests/test_ui_browser.py`` and
``tests/test_ui_browser_nav.py``, built the same way: the real ``index.html``,
the real ``css/`` and ``js/`` off disk, every API call stubbed at the network
layer. No Postgres, no Ollama, and no contention for the machine-wide
test-database lock.

**The filename is load-bearing.** ``tests/test_ci_workflow.py`` discovers every
module carrying the ``browser`` marker and fails if one sits outside the CI
selection, which names the path glob ``tests/test_ui_browser*.py`` rather than a
bare ``-m browser`` (marker deselection happens after collection, and two
modules in this suite open a database connection at import time). This file is
inside the glob by construction.

WHAT IS COVERED HERE (T13): the table of contents and the breadcrumb trail in
the marginalia block.

**WHY THE TEST MOUNTS THE MARGINALIA ITSELF.** ``wireMarginalia()`` is called
from ``page.evaluate`` rather than reached through ``js/main.js``'s ``boot()``,
because ``index.html`` and ``js/main.js`` are owned by a dedicated integrator
for the whole of phase 2 and this task must not edit them. What executes is the
REAL module off disk, against the REAL booted app; only the *call site* is
supplied by the harness. ``wireMarginalia()`` is idempotent for exactly that
reason — ``boot()`` now ALSO calls it (main.js), so this mount is a second call
on every run and must be a no-op rather than a second subscriber rendering a
second TOC. Same contract, and the same rationale, as ``wirePalette()`` in
``tests/test_ui_browser_nav.py``.

**THAT WIRING IS NOT PROVEN BY THIS FILE, AND THE DISTINCTION MATTERS.** Because
these tests mount the module themselves, they pass whether or not ``boot()``
knows the module exists — the same costume as a route that is tested and
unreachable. What pins the boot call is a separate guard,
``check_the_marginalia_is_wired_after_the_inspector`` in
``tests/test_ui_static_behaviour.py``, which also pins its POSITION: it must
follow ``subscribe(renderInspector)``, because ``renderInspector`` opens with
``host.textContent = ""`` and a subscriber registered earlier is wiped on the
same dispatch that drew it. Nothing in this file would notice that.

**THE SCROLL ASSERTION MEASURES GEOMETRY, NEVER THE HASH.** ``location.hash``
changes whether or not anything moved, and an ``href="#id"`` that points at
nothing still writes the hash. Every assertion here reads
``getBoundingClientRect()`` and compares it against the scroll container's own
rect, so it fails for a TOC that links to an id absent from the DOM — which is
precisely defect S4.

S4 RESTATED, because it is what test_every_toc_entry_resolves_to_a_heading
exists for: ``notes_service.read_note`` renders
``strip_redundant_title_heading(body, title)``, so the document's leading H1 is
NOT in the HTML the TOC links into. A TOC built from the unstripped source opens
with an entry pointing at an anchor the page does not contain. The server
already handles this — ``notes_service`` passes the *rendered* body to
``extract_headings`` — and the front end must not reintroduce it by synthesising
a title entry of its own.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, cast

import pytest

from brain.ui.app import static_dir

pytestmark = pytest.mark.browser

#: The seeded note. Synthetic throughout — no real titles, paths or ids.
#:
#: The body is deliberately LONG. The scroll oracle is only meaningful if the
#: target heading starts out below the fold: in a pane tall enough to show the
#: whole note, "scroll it into view" is satisfied by doing nothing at all, and
#: the test would pass against an implementation whose click handler is a no-op.
#: ``test_the_third_heading_starts_out_below_the_fold`` pins that precondition
#: rather than trusting it.
NOTE_ID = "n-vendor"
NOTE_TITLE = "Vendor Evaluation"
VAULT_PATH = "projects/q3/vendor-evaluation.md"

#: (id, text) in document order, matching the ids ``brain.ui.render`` stamps.
#: The TOC target is the THIRD entry, per the T13 oracle.
HEADINGS: list[tuple[str, str]] = [
    ("scope", "Scope"),
    ("signals", "Signals"),
    ("risks", "Risks"),
    ("next-steps", "Next Steps"),
]
TARGET_INDEX = 2
TARGET_ID, TARGET_TEXT = HEADINGS[TARGET_INDEX]

#: Filler that makes the note taller than any plausible pane.
_FILLER = "".join(
    f"<p>Paragraph {n} of synthetic evaluation prose held here purely to give "
    "the note enough height that a heading below it is genuinely out of "
    "view.</p>" for n in range(12)
)

#: The server-rendered HTML, with the leading H1 ALREADY STRIPPED — exactly what
#: ``notes_service.read_note`` produces. There is no ``<h1>`` here, so a TOC
#: entry pointing at one is unresolvable, which is the S4 defect made visible.
NOTE_HTML = "".join(
    f'<h2 id="{hid}">{text}</h2>{_FILLER}' for hid, text in HEADINGS
)

#: What ``extract_headings`` returns for the stripped body, in payload form.
NOTE_HEADINGS = [
    {"level": 2, "text": text, "id": hid} for hid, text in HEADINGS
]

NOTE_PAYLOAD: dict[str, Any] = {
    "id": NOTE_ID,
    "title": NOTE_TITLE,
    "tier": "vault",
    "content_type": "note",
    "draft": False,
    "tags": [],
    "source_kind": "manual",
    "vault_path": VAULT_PATH,
    "ingested_at": None,
    "editable": True,
    "movable": True,
    "body": f"# {NOTE_TITLE}\n\n## Scope\n",
    "body_hash": "sha256:x",
    "html": NOTE_HTML,
    "headings": NOTE_HEADINGS,
}

TREE: dict[str, Any] = {
    "count": 1,
    "name": "",
    "path": "",
    "empty_hint": "Nothing here yet.",
    "children": [],
    "notes": [{"id": NOTE_ID, "title": NOTE_TITLE, "path": VAULT_PATH,
               "draft": False, "tier": "vault", "date": "2026-01-04"}],
}

_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": TREE,
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
    "/api/search": {"results": [], "total_documents": 0,
                    "timing_ms": {"total": 1, "embed": 0, "sql": 1},
                    "session_id": "s-synthetic"},
}

# ------------------------------------------------------------------ T14 seed --

#: The document that links INTO the open note. Backlinks of the seeded note are
#: exactly ``[SOURCE]``, so the oracle can name a title rather than count rows.
SOURCE_ID = "n-planning"
SOURCE_TITLE = "Planning Sync"

#: ``link_text`` is deliberately DIFFERENT from the title. It is what the author
#: typed inside the brackets, and the rail must show the document's own name
#: instead — a rail rendering link_text would label the same document
#: differently on every page that links to it. Pinned by
#: ``test_the_rail_shows_the_document_title_not_the_authored_link_text``.
SOURCE_LINK_TEXT = "the planning call"

LINKS_PAYLOAD: dict[str, Any] = {
    "id": NOTE_ID,
    "backlinks": [{
        "id": SOURCE_ID,
        "title": SOURCE_TITLE,
        "kind": "vault",
        "link_text": SOURCE_LINK_TEXT,
        "link_kind": "wiki",
        "rule": None,
        "weight": None,
    }],
    "outgoing": [],
    "counts": {"backlinks": 1, "outgoing": 0},
}

#: How the ``/links`` stub should behave for the test currently running. Reset
#: by the ``page`` fixture, so a test that changes it cannot leak into the next.
_LINKS_MODE: dict[str, str] = {"value": "ok"}


@pytest.fixture(scope="module")
def static_origin() -> Iterator[str]:
    """Serve the REAL static directory over HTTP.

    Over http:// rather than file:// because ES module imports are subject to
    CORS and a file:// origin cannot satisfy them. The PACKAGE dir is served,
    not ``static/``, because index.html references its assets as absolute
    ``/static/...`` paths.
    """
    handler = partial(SimpleHTTPRequestHandler, directory=str(static_dir().parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def page(static_origin: str) -> Iterator[Any]:
    """A booted app page with every API call stubbed.

    The viewport is deliberately SHORT (600px). The scroll oracle needs the
    third heading to start out below the fold; a tall default viewport would
    show the whole note and make "scrolled into view" vacuously true.
    """
    sync_api = pytest.importorskip(
        "playwright.sync_api",
        reason=(
            "Playwright not installed — `pip install -e \".[browser]\"` "
            "(browsers cache outside the venv, so this is a small install)"
        ),
    )

    _LINKS_MODE["value"] = "ok"

    def route_api(route: Any) -> None:
        path = "/" + route.request.url.split("127.0.0.1:")[-1].split("/", 1)[-1]
        path = path.split("?")[0]
        # The /links form is matched FIRST: `/api/notes/{id}` would otherwise
        # never see it (the two patterns are disjoint), but ordering the checks
        # this way keeps the intent obvious rather than implicit.
        if re.fullmatch(r"/api/notes/[^/]+/links", path) is not None:
            mode = _LINKS_MODE["value"]
            if mode == "error":
                route.fulfill(
                    status=500, content_type="application/json",
                    body=json.dumps({"error": {"code": "database_unavailable",
                                               "message": "the database is unavailable"}}),
                )
                return
            payload = (
                {**LINKS_PAYLOAD, "backlinks": [], "counts": {"backlinks": 0, "outgoing": 0}}
                if mode == "empty" else LINKS_PAYLOAD
            )
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload))
            return
        if re.fullmatch(r"/api/notes/[^/]+", path) is not None:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(NOTE_PAYLOAD))
            return
        body = _STUBS.get(path)
        if body is None:
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        route.fulfill(status=200, body=json.dumps(body),
                      content_type="application/json")

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": 1280, "height": 600})
            pg.route("**/api/**", route_api)
            pg.goto(f"{static_origin}/static/index.html")
            # Short timeout on purpose: a page that did not boot is a blank
            # page, and waiting 30s to learn that wastes a run.
            pg.wait_for_selector('[role="treeitem"]', timeout=8000)
            yield pg
        finally:
            browser.close()


def _open_note(page: Any) -> None:
    """Mount the REAL marginalia module, then open the seeded note.

    Mount happens BEFORE the note opens so the subscriber is registered when
    the note-loaded dispatch fires — the same ordering ``boot()`` uses.
    """
    # Placeholder substitution rather than %-formatting or an f-string: the
    # script body is JavaScript and is full of braces, so both of those would
    # need every one of them escaped for no gain in clarity.
    page.evaluate(
        """async () => {
            const marginalia = await import("/static/js/marginalia.js");
            marginalia.wireMarginalia();
            const inspector = await import("/static/js/inspector.js");
            await inspector.openNote("__NOTE_ID__");
        }""".replace("__NOTE_ID__", NOTE_ID)
    )
    page.wait_for_selector(".note-body")


def _toc_entries(page: Any) -> list[dict[str, str]]:
    """The rendered TOC as ``[{"href": …, "text": …}]``, in document order."""
    return page.eval_on_selector_all(
        ".marginalia .toc a",
        "els => els.map(e => ({href: e.getAttribute('href'), text: e.textContent}))",
    )


def _is_within(page: Any, selector: str) -> bool:
    """Is ``selector``'s box inside the inspector's visible box?

    Geometry only. Compares the element's own rect against the SCROLL
    CONTAINER's rect (``.inspector`` is the ``overflow-y: auto`` element), not
    against the viewport, so the answer does not depend on where the pane sits
    on the page.
    """
    return bool(page.evaluate(
        """(sel) => {
            const target = document.querySelector(sel);
            const pane = document.getElementById("inspector");
            if (!target || !pane) return false;
            const t = target.getBoundingClientRect();
            const p = pane.getBoundingClientRect();
            return t.top >= p.top - 2 && t.top < p.bottom;
        }""",
        selector,
    ))


# ------------------------------------------------------------------- the TOC --


def test_the_toc_lists_every_server_provided_heading(page: Any) -> None:
    """Guard the guard: every test below is vacuous if nothing rendered.

    Pins the TEXT and the ORDER, because "the third entry" is only a meaningful
    target if which heading is third is decided by the payload rather than by
    luck.
    """
    _open_note(page)
    assert [e["text"] for e in _toc_entries(page)] == [t for _, t in HEADINGS], (
        "the TOC did not render the seeded headings in document order; the "
        "third-entry target below is derived from this list"
    )


def test_every_toc_entry_resolves_to_a_heading_in_the_rendered_body(
    page: Any,
) -> None:
    """DEFECT S4. Asserts the TARGET RESOLVES, not that an ``<a href="#…">`` exists.

    The stripped-title trap produces a perfectly well-formed anchor pointing at
    an id the page does not contain, so "a link exists" survives the bug
    untouched. This resolves each href against the DOM instead.

    MUTATION THAT MUST GO RED (measured, not assumed): in ``js/marginalia.js``,
    have ``tocEntries()`` prepend ``{id: slug(note.title), text: note.title}`` —
    the entry the *unstripped* body would have produced. Entry 1 then points at
    an id absent from the rendered HTML and this test fails on it by name.
    """
    _open_note(page)
    entries = _toc_entries(page)
    assert entries, "no TOC entries rendered — nothing to resolve"

    unresolved = page.evaluate(
        """(hrefs) => hrefs.filter(
            (h) => !document.querySelector(".note-body " + h)
        )""",
        [e["href"] for e in entries],
    )
    assert unresolved == [], (
        f"TOC entries {unresolved} point at ids absent from the rendered body. "
        "That is defect S4: a TOC built from the UNSTRIPPED source opens with "
        "an entry for the H1 that strip_redundant_title_heading removed."
    )


def test_the_third_heading_starts_out_below_the_fold(page: Any) -> None:
    """The precondition the scroll oracle depends on.

    If the seeded note ever became short enough to fit the pane, the click test
    below would pass against a no-op handler. This fails first, and says why.
    """
    _open_note(page)
    assert not _is_within(page, f"#{TARGET_ID}"), (
        f"#{TARGET_ID} is already visible before any click, so "
        "test_clicking_the_third_toc_entry_scrolls_that_heading_into_view "
        "cannot discriminate a working handler from a dead one. Lengthen the "
        "seeded body or shorten the viewport."
    )


def test_clicking_the_third_toc_entry_scrolls_that_heading_into_view(
    page: Any,
) -> None:
    """**THE** T13 test: click TOC entry 3, the matching ``<h2>`` comes into view.

    The assertion is GEOMETRY — the heading's ``getBoundingClientRect()``
    measured against the scroll container's — never ``location.hash``. The hash
    is written by the browser whether or not the target exists and whether or
    not anything moved, so a hash assertion passes against a TOC linking to
    nothing at all.

    MUTATION THAT MUST GO RED (measured, not assumed): in ``js/marginalia.js``,
    delete the ``target.scrollIntoView(...)`` call from the click handler,
    leaving the ``preventDefault()``. The entry still highlights and the DOM is
    unchanged, but the heading never comes into view and this fails.
    """
    _open_note(page)
    entries = _toc_entries(page)
    assert entries[TARGET_INDEX]["text"] == TARGET_TEXT, (
        f"entry {TARGET_INDEX} is {entries[TARGET_INDEX]['text']!r}, expected "
        f"{TARGET_TEXT!r} — the seed no longer places the target third"
    )

    page.locator(".marginalia .toc a").nth(TARGET_INDEX).click()
    page.wait_for_function(
        """(id) => {
            const t = document.getElementById(id);
            const p = document.getElementById("inspector");
            if (!t || !p) return false;
            const tr = t.getBoundingClientRect();
            const pr = p.getBoundingClientRect();
            return tr.top >= pr.top - 2 && tr.top < pr.bottom;
        }""",
        arg=TARGET_ID,
        timeout=4000,
    )
    assert _is_within(page, f"#{TARGET_ID}"), (
        f"clicking TOC entry {TARGET_INDEX + 1} ({TARGET_TEXT!r}) left "
        f"#{TARGET_ID} outside the inspector's visible box"
    )


def test_the_toc_marks_which_entry_was_activated(page: Any) -> None:
    """The clicked entry is marked current, so the rail is not write-only.

    ``aria-current="location"`` rather than a class alone: the TOC is a
    navigation landmark and the active entry has to be announced, not merely
    tinted.
    """
    _open_note(page)
    page.locator(".marginalia .toc a").nth(TARGET_INDEX).click()
    current = page.eval_on_selector_all(
        '.marginalia .toc a[aria-current="location"]', "els => els.map(e => e.textContent)"
    )
    assert current == [TARGET_TEXT], (
        f"expected exactly {TARGET_TEXT!r} marked current, got {current}"
    )


# ----------------------------------------------------------- the breadcrumbs --


def test_the_breadcrumbs_name_every_vault_path_segment(page: Any) -> None:
    """The trail is the note's location, segment by segment.

    Asserts the SEGMENTS, not that a ``.breadcrumbs`` element exists — an empty
    container satisfies the latter while telling the reader nothing about where
    the note lives.
    """
    _open_note(page)
    crumbs = page.eval_on_selector_all(
        ".marginalia .breadcrumbs li", "els => els.map(e => e.textContent)"
    )
    assert crumbs == VAULT_PATH.split("/"), (
        f"breadcrumbs {crumbs} do not spell out {VAULT_PATH}"
    )


# ------------------------------------------------------------- absent cases --


def test_a_note_with_no_headings_renders_no_toc_chrome(page: Any) -> None:
    """No headings is not an empty TOC — it is no TOC.

    An empty ``<nav>`` with a heading label is chrome that promises a table of
    contents and delivers a blank box.
    """
    flat = json.dumps({**NOTE_PAYLOAD, "id": "n-flat", "headings": [],
                       "html": "<p>No headings here.</p>"})
    page.evaluate(
        """async () => {
            const marginalia = await import("/static/js/marginalia.js");
            marginalia.wireMarginalia();
            const store = await import("/static/js/store.js");
            store.dispatch({
                note: __NOTE_JSON__,
                selectedId: "n-flat",
                editing: false,
            });
        }""".replace("__NOTE_JSON__", flat)
    )
    page.wait_for_selector(".note-body")
    assert page.locator(".marginalia .toc").count() == 0, (
        "a note with no headings still rendered TOC chrome"
    )


def test_the_marginalia_is_absent_while_editing(page: Any) -> None:
    """The editor owns the pane.

    ``.inspector`` is a two-row grid whose ``1fr`` track the editor must land
    on; a marginalia block rendered alongside it would take a track and is also
    simply wrong — a table of contents beside a textarea of raw markdown
    describes a rendering the user is not looking at.
    """
    _open_note(page)
    assert page.locator(".marginalia").count() == 1, "precondition: TOC rendered"

    page.evaluate(
        """async () => {
            const store = await import("/static/js/store.js");
            store.dispatch({ editing: true });
        }"""
    )
    page.wait_for_selector(".editor")
    assert page.locator(".marginalia").count() == 0, (
        "the marginalia survived into edit mode and is competing with the "
        "editor for the inspector's 1fr track"
    )


# ------------------------------------------------------- T14: the backlinks --
#
# BACKLINKS ONLY. The related-docs half of the T14 plan row is NOT built here:
# there is no HTTP endpoint for related documents anywhere in the tree
# (`compute_related` exists in src/brain/related.py but no ui/ module references
# it and app.py registers no such route), and adding one is blocked on the
# unresolved `vector_sim_floor` decision. Recorded as defect S18; the
# coordinator ruled backlinks-only. The plan's own T14 oracle names backlinks
# and nothing else, which is what made the omission visible.


def _gate_links(page: Any) -> None:
    """Hold the ``/links`` response inside the PAGE until the test releases it.

    The delay is installed in the browser rather than in the route handler on
    purpose. A ``time.sleep`` in a Playwright route handler blocks the driver's
    message pump, not just the one request, so it would stall the very
    ``wait_for_selector`` calls the assertion depends on and the test would be
    measuring the harness instead of the code.

    It also gives the mutation a sharp edge. A BLOCKING implementation would
    reach for a synchronous ``XMLHttpRequest``, which never touches
    ``window.fetch`` and so sails past this gate — arriving *before* the release
    and tripping the "the rail is not here yet" assertion. Gated-and-absent
    versus ungated-and-present is exactly the distinction under test.
    """
    page.evaluate(
        """() => {
            window.__releaseLinks = null;
            const gate = new Promise((resolve) => { window.__releaseLinks = resolve; });
            const realFetch = window.fetch.bind(window);
            window.fetch = (input, init) => {
                if (String(input).includes("/links")) {
                    return gate.then(() => realFetch(input, init));
                }
                return realFetch(input, init);
            };
        }"""
    )


def test_a_backlink_names_the_document_that_links_in(page: Any) -> None:
    """**THE** T14 test: open the note, the rail names the document linking in.

    Asserts the TITLE of the linking document, not that a rail element exists
    and not a row count. "One backlink is shown" survives a rail that renders
    the wrong document, the open note itself, or a blank row.
    """
    _open_note(page)
    page.wait_for_selector(".marginalia .backlinks-rail a", timeout=4000)
    titles = page.eval_on_selector_all(
        ".marginalia .backlinks-rail a", "els => els.map(e => e.textContent)"
    )
    assert titles == [SOURCE_TITLE], (
        f"backlinks rail shows {titles}, expected exactly [{SOURCE_TITLE!r}] — "
        "the one document that links into the open note"
    )


def test_the_rail_shows_the_document_title_not_the_authored_link_text(
    page: Any,
) -> None:
    """``link_text`` is the author's phrasing; the title is the document's name.

    The payload carries both, and they differ in the seed precisely so this can
    discriminate. A rail rendering link_text labels one document differently on
    every page that links to it.
    """
    _open_note(page)
    page.wait_for_selector(".marginalia .backlinks-rail a", timeout=4000)
    shown = page.locator(".marginalia .backlinks-rail a").first.inner_text()
    assert shown == SOURCE_TITLE, f"rail shows {shown!r}, expected {SOURCE_TITLE!r}"
    assert shown != SOURCE_LINK_TEXT, (
        "the rail rendered the authored link text instead of the title"
    )


def test_the_note_is_readable_before_the_backlinks_arrive(page: Any) -> None:
    """The rail is LAZY: the note never waits on a second request.

    With the links response held, the note body and the TOC must already be
    rendered and the rail must be absent. Releasing the gate then produces it —
    which is what stops "absent" from being vacuously true.

    MUTATION THAT MUST GO RED (measured, not assumed): in
    ``js/marginalia.js``, replace the ``api(...).then(...)`` call in
    ``attachBacklinks`` with a synchronous ``XMLHttpRequest`` (``open(..., false)``
    then ``send()``) and append the rail inline. The request bypasses the page's
    patched ``window.fetch``, so the rail is present on the very first paint and
    the "not yet" assertion below fails.
    """
    _gate_links(page)
    _open_note(page)

    page.wait_for_selector(".marginalia .toc a", timeout=4000)
    assert page.locator(".note-body").count() == 1, (
        "the note body is not rendered while the links request is outstanding — "
        "the rail's fetch is standing between the reader and the note"
    )
    assert page.locator(".marginalia .backlinks-rail").count() == 0, (
        "the backlinks rail rendered before its response was released, so the "
        "fetch is not lazy: a blocking request would produce exactly this"
    )

    page.evaluate("() => window.__releaseLinks()")
    page.wait_for_selector(".marginalia .backlinks-rail a", timeout=4000)
    titles = page.eval_on_selector_all(
        ".marginalia .backlinks-rail a", "els => els.map(e => e.textContent)"
    )
    assert titles == [SOURCE_TITLE], (
        "the rail never arrived after the response was released, so the "
        "assertion above proved nothing"
    )


def test_a_failing_links_route_leaves_the_note_readable_and_silent(
    page: Any,
) -> None:
    """500 on the rail: the note still renders, and NOTHING is announced.

    The toast assertion is the point. ``/api/notes/…`` already reports anything
    that stopped the NOTE from loading; a second error for a supplementary rail
    tells the reader about a request they never made. ``api()`` throws on a
    non-2xx and does not toast — toasting is the caller's decision, and this
    caller declines.
    """
    _LINKS_MODE["value"] = "error"
    _open_note(page)

    page.wait_for_selector(".marginalia .toc a", timeout=4000)
    assert page.locator(".note-body").count() == 1, (
        "a failing links route took the note down with it"
    )
    assert page.locator(".marginalia .backlinks-rail").count() == 0, (
        "a failed rail rendered chrome anyway"
    )
    # The toast element is shipped hidden and unhidden by dom.js's toast().
    # Asserting on `hidden` rather than existence is what makes this mean
    # "nothing was announced" rather than "the element is absent from the page".
    assert page.locator("#toast").is_hidden(), (
        "a toast fired for a failed backlinks fetch — the reader is being told "
        "about a supplementary request they never made"
    )


def test_a_note_with_no_backlinks_renders_no_rail(page: Any) -> None:
    """Nothing links here is not an empty rail — it is no rail.

    An empty "Linked from" heading is chrome that asks the reader to interpret a
    blank.
    """
    _LINKS_MODE["value"] = "empty"
    _open_note(page)

    page.wait_for_selector(".marginalia .toc a", timeout=4000)
    assert page.locator(".marginalia .backlinks-rail").count() == 0, (
        "a note with zero backlinks still rendered the rail"
    )


# ------------------------------------------------------ T18: email threads --
#
# The server half (structural <details> re-emission) is covered by
# tests/test_ui_render_email_thread.py. What is left for a browser is the part
# that only exists in the DOM: the synthetic open section for the newest
# message, and the "only my replies" filter.
#
# THE HEALTH STUB IS PRODUCED BY THE REAL HANDLER, and that is the point rather
# than a convenience. `routes_meta.health` is invoked below with a minimal
# context and its actual JSON becomes the stub. So deleting `user_email` from
# that handler does not merely fail a payload-shape assertion somewhere else —
# it removes the field from this stub and turns the FILTER test red, which is
# the behaviour the field exists for. A hand-written stub would have kept
# passing and left the field unproven.


def _real_health_payload(*, user_email: str | None) -> dict[str, Any]:
    """Call the REAL ``routes_meta.health`` and return its JSON.

    THE CONTEXT IS A REAL ``UiContext``, not a hand-built stand-in, and that is
    the load-bearing part rather than a style preference. This helper used to
    pass a ``SimpleNamespace`` carrying exactly the four attributes ``health``
    read at the time. When ``serve_confidential_titles`` was added to
    ``UiContext`` and read by the handler, the stand-in had no such attribute
    and all four thread tests died at setup with ``AttributeError`` — a break
    caused by a field that ``UiContext`` itself declares with a default, so a
    real context would have absorbed it silently.

    A real context cannot fail that way: every field added to the dataclass so
    far carries a default, deliberately (see the ``serve_confidential_titles``
    docstring — the default is the fail-closed one precisely so a fixture that
    does not care need not name it). The 13 other UI test modules already
    construct the real dataclass; this was the only module that did not.

    ``cfg`` stays duck-typed, matching those modules: ``Config`` is a large
    object built from the environment, and ``health`` reads exactly two fields
    off it. ``conn_factory``/``embedder``/``search_fn`` are required by the
    dataclass but unreachable here — ``health`` touches no database, which is
    the property that lets it answer when Postgres is down.
    """
    import asyncio

    from brain.ui import routes_meta
    from brain.ui.context import UiContext

    class _Cfg:
        vault_path = "/vault"
        user_email: str | None = None

    cfg = _Cfg()
    cfg.user_email = user_email

    def _unused_conn_factory() -> Any:  # pragma: no cover — health opens no connection
        raise AssertionError("routes_meta.health must not open a database connection")

    ctx = UiContext(
        cfg=cast(Any, cfg),
        conn_factory=_unused_conn_factory,
        embedder=cast(Any, object()),
        search_fn=lambda *a, **k: [],
        read_only=False,
        logging_enabled=False,
        serve_confidential_bodies=False,
        notices=(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ui=ctx)))
    response = asyncio.run(routes_meta.health(request))  # type: ignore[arg-type]
    payload: dict[str, Any] = json.loads(response.body)
    return payload


OWNER_EMAIL = "owner@example.test"
OTHER_EMAIL = "sam@example.test"

#: A thread rendered by the REAL renderer, so the browser sees exactly what the
#: server would send. Older messages collapsed; newest a trailing plain H2 —
#: the order gmail.py actually emits (verified by running the producer).
THREAD_SOURCE = (
    "<details>\n"
    f"<summary>2026-03-01 09:15 — Sam Buyer &lt;{OTHER_EMAIL}&gt;</summary>\n"
    "\n"
    "Their message.\n"
    "\n"
    "</details>\n"
    "\n"
    "<details>\n"
    f"<summary>2026-03-02 10:00 — Me &lt;{OWNER_EMAIL}&gt;</summary>\n"
    "\n"
    "My earlier reply.\n"
    "\n"
    "</details>\n"
    "\n"
    f"## 2026-03-03 08:05 — Me &lt;{OWNER_EMAIL}&gt;\n\nThe newest message.\n"
)


@pytest.fixture
def thread_page(static_origin: str, request: Any) -> Iterator[Any]:
    """A booted page showing one email thread.

    ``request.param`` is the configured owner address, or ``None`` for "not
    configured" — which is a genuinely different state from "configured but
    matching nothing".
    """
    from brain.ui.render import EMAIL_THREAD_CONTENT_TYPE, render_markdown

    sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright not installed")
    owner = getattr(request, "param", OWNER_EMAIL)
    health = _real_health_payload(user_email=owner)
    # `content_type` is REQUIRED for the thread rules to fire: `render_markdown`
    # defaults to no thread recognition, so omitting it here yields a body with
    # zero server-emitted `details.thread-message` and only the one section
    # `js/thread.js` synthesizes for the newest message — which is what these
    # three tests were asserting against when they read `1 == 3`.
    note = {**NOTE_PAYLOAD, "id": "n-thread", "title": "Q3 numbers",
            "headings": [],
            "html": render_markdown(THREAD_SOURCE, content_type=EMAIL_THREAD_CONTENT_TYPE)}

    def route_api(route: Any) -> None:
        path = "/" + route.request.url.split("127.0.0.1:")[-1].split("/", 1)[-1]
        path = path.split("?")[0]
        if path == "/api/health":
            body: Any = health
        elif re.fullmatch(r"/api/notes/[^/]+/links", path):
            body = {"id": "n-thread", "backlinks": [], "outgoing": [],
                    "counts": {"backlinks": 0, "outgoing": 0}}
        elif re.fullmatch(r"/api/notes/[^/]+", path):
            body = note
        else:
            body = _STUBS.get(path)
        if body is None:
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        route.fulfill(status=200, body=json.dumps(body),
                      content_type="application/json")

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": 1280, "height": 700})
            pg.route("**/api/**", route_api)
            pg.goto(f"{static_origin}/static/index.html")
            pg.wait_for_selector('[role="treeitem"]', timeout=8000)
            pg.evaluate(
                """async () => {
                    const thread = await import("/static/js/thread.js");
                    thread.wireThread();
                    const inspector = await import("/static/js/inspector.js");
                    await inspector.openNote("n-thread");
                }"""
            )
            pg.wait_for_selector("details.thread-message")
            yield pg
        finally:
            browser.close()


def test_the_newest_message_becomes_an_open_section_like_the_others(
    thread_page: Any,
) -> None:
    """All three messages share one shape; only the newest is open.

    The assembler leaves the newest message as a bare H2 — no summary bar, no
    twisty, not collapsible — while every older one is a `<details>`. This is
    the asymmetry thread.js exists to remove.
    """
    sections = thread_page.locator("details.thread-message")
    assert sections.count() == 3, (
        f"expected 3 uniform sections, got {sections.count()} — the newest "
        "message was not wrapped, so it is still the odd one out"
    )
    assert thread_page.locator("details.thread-message[open]").count() == 1, (
        "exactly one section should start open"
    )
    assert thread_page.locator("details.thread-newest[open]").count() == 1, (
        "the open one is not the newest message"
    )


def test_the_newest_messages_anchor_survives_the_wrap(thread_page: Any) -> None:
    """T5 NON-DISTURBANCE, pinned rather than asserted in a comment.

    The wrap MOVES the real `<h2>` into the summary instead of copying its text,
    because that element carries the anchor id `extract_headings` points the TOC
    at. Copying the text and dropping the element would leave every TOC link
    into the newest message pointing at an id the page no longer contains — and
    the link would still be there, so nothing would look broken.
    """
    anchored = thread_page.eval_on_selector_all(
        "details.thread-newest summary h2", "els => els.map((e) => e.id)"
    )
    assert len(anchored) == 1 and anchored[0], (
        f"the newest message's <h2> did not survive the wrap with its id: "
        f"{anchored}"
    )
    assert thread_page.evaluate(
        "(id) => Boolean(document.getElementById(id))", anchored[0]
    ), "the anchor id no longer resolves in the document"


def test_only_my_replies_hides_the_other_senders_messages(
    thread_page: Any,
) -> None:
    """The filter, reading the address the SERVER reported for this request.

    MUTATION THAT MUST GO RED (measured, not assumed): delete the `user_email`
    line from `routes_meta.health`. The stub in this module is produced by
    calling that real handler, so the field vanishes from it, `ownerEmail()`
    returns "", the control is never mounted, and this test fails at the
    checkbox — not at some payload-shape assertion elsewhere.
    """
    assert thread_page.locator(".thread-filter input").count() == 1, (
        "the filter control was not mounted, so the owner address never "
        "reached the client"
    )
    visible_before = thread_page.locator("details.thread-message:not([hidden])")
    assert visible_before.count() == 3, "precondition: all messages visible"

    thread_page.locator(".thread-filter input").check()

    shown = thread_page.eval_on_selector_all(
        "details.thread-message:not([hidden]) summary",
        "els => els.map((e) => e.textContent)",
    )
    assert len(shown) == 2, (
        f"expected the 2 messages from {OWNER_EMAIL}, got {len(shown)}: {shown}"
    )
    assert all(OWNER_EMAIL in text for text in shown), (
        f"a message from another sender survived the filter: {shown}"
    )
    assert not any(OTHER_EMAIL in text for text in shown), (
        f"{OTHER_EMAIL}'s message is still visible with the filter on"
    )


@pytest.mark.parametrize("thread_page", [None], indirect=True)
def test_no_configured_address_offers_no_filter(thread_page: Any) -> None:
    """Unconfigured is not the same as "you wrote none of these".

    With no BRAIN_USER_EMAIL the question the filter answers has no answer, so
    the control is not offered at all. Mounting it would filter to nothing and
    read as a statement about the thread rather than about the configuration.
    """
    assert thread_page.locator("details.thread-message").count() == 3, (
        "precondition: the thread still renders without an owner address"
    )
    assert thread_page.locator(".thread-filter").count() == 0, (
        "the filter was offered with no owner address configured"
    )
