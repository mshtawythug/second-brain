"""Fast navigation surfaces, EXECUTED in a real browser.

Companion harness to ``tests/test_ui_browser.py``. Same construction — the real
``index.html``, the real ``css/`` and ``js/`` off disk, every API call stubbed at
the network layer, so no Postgres, no Ollama and no contention for the
machine-wide test-database lock. See that module's header for why the session
fixtures in ``tests/conftest.py`` skip for a ``browser``-marked selection, and
why the CI job names a **path glob** (``tests/test_ui_browser*.py``) rather than
a bare ``-m browser``: marker deselection happens after collection, and two
modules in this suite open a database connection at import time.

**The filename is load-bearing.** ``tests/test_ci_workflow.py`` discovers every
module carrying the ``browser`` marker and fails if one sits outside the CI
selection, so a browser module named anything other than ``test_ui_browser*.py``
turns that gate red. This file is inside the glob by construction.

WHAT IS COVERED HERE (T12): the command palette — ⌘P, fuzzy match over the
already-loaded ``/api/tree`` titles, arrow keys, Enter.

**⌘P AND NOT ⌘K.** ⌘K is already bound, in ``js/keys.js``, to "focus the search
box". Rebinding it would delete a shipped behaviour to add a new one, so the
palette takes ⌘P. The same reasoning is recorded at the binding site in
``js/palette.js``.

**WHY THE TEST MOUNTS THE PALETTE ITSELF.** ``wirePalette()`` is called from
``page.evaluate`` rather than reached through ``js/main.js``'s ``boot()``,
because ``index.html`` and ``js/main.js`` are owned by a dedicated integrator
for the whole of phase 2 and this task must not edit them. What executes is the
REAL module off disk, against the REAL booted app; only the *call site* is
supplied by the harness. ``wirePalette()`` is idempotent for exactly this
reason — once ``boot()`` also calls it, this mount is a no-op rather than a
second dialog with a second keydown listener, and these tests keep passing
unchanged.

**THE ASSERTION IS THE IDENTITY OF THE NOTE THAT OPENS**, never "the dialog is
visible". A visibility assertion survives every defect this feature can have:
a broken fuzzy match, a dead Enter key, an arrow-key index that never moves.
The mutation that proves it — pinning the highlight to 0 so ArrowDown does
nothing — is recorded on the test it must turn red.
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

#: The seeded vault. Synthetic throughout — no real titles, paths or ids.
#:
#: The five titles are chosen so the query ``"not"`` (three characters, per the
#: T12 oracle) matches EVERY one of them, which is what makes the ranking — and
#: therefore the arrow-key index — the only thing that decides which note opens.
TREE: dict[str, Any] = {
    "count": 5,
    "name": "",
    "path": "",
    "empty_hint": "Nothing here yet.",
    "children": [
        {
            "name": "archive", "path": "archive", "children": [],
            "notes": [{"id": "n-buried", "title": "Buried Note",
                       "path": "archive/buried.md", "draft": False,
                       "tier": "vault", "date": "2026-01-02"}],
        },
        {
            "name": "projects", "path": "projects",
            "children": [
                {"name": "q3", "path": "projects/q3", "children": [],
                 "notes": [{"id": "n-deep", "title": "Deep Note",
                            "path": "projects/q3/deep.md", "draft": False,
                            "tier": "vault", "date": "2026-01-03"}]},
            ],
            "notes": [
                {"id": "n-alpha", "title": "Alpha Note",
                 "path": "projects/alpha.md", "draft": False,
                 "tier": "vault", "date": "2026-01-04"},
                {"id": "n-beta", "title": "Beta Note",
                 "path": "projects/beta.md", "draft": True,
                 "tier": "vault", "date": "2026-01-05"},
            ],
        },
    ],
    "notes": [{"id": "n-root", "title": "Root Note", "path": "root.md",
               "draft": False, "tier": "vault", "date": "2026-01-06"}],
}

#: id -> title, derived from TREE rather than restated, so a seed edit cannot
#: leave the expectations pointing at a note that no longer exists.
TITLES: dict[str, str] = {}


def _collect(node: dict[str, Any]) -> None:
    for child in node["children"]:
        _collect(child)
    for note in node["notes"]:
        TITLES[note["id"]] = note["title"]


_collect(TREE)

#: The ranking this seed produces for the query ``"not"``, stated here so the
#: test asserts a CONCRETE note rather than whatever the implementation happens
#: to put third.
#:
#: ``scoreTitle`` in js/palette.js scores a substring hit as
#: ``1000 + (word-boundary ? 100 : 0) - index``, ties broken by vault order
#: (depth-first: folders before notes). Lower-cased:
#:
#:   "deep note"    "not" at 5, word start -> 1095   (vault order 1)
#:   "beta note"    "not" at 5, word start -> 1095   (vault order 3)
#:   "root note"    "not" at 5, word start -> 1095   (vault order 4)
#:   "alpha note"   "not" at 6, word start -> 1094   (vault order 2)
#:   "buried note"  "not" at 7, word start -> 1093   (vault order 0)
QUERY = "not"
EXPECTED_ORDER = ["n-deep", "n-beta", "n-root", "n-alpha", "n-buried"]
#: Two ArrowDowns from the top. This is the note that must end up open, and it
#: is deliberately NOT ``EXPECTED_ORDER[0]`` — an implementation that ignores
#: the arrow keys opens that one instead, which is the mutation below.
TARGET = EXPECTED_ORDER[2]

_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": TREE,
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
    "/api/search": {"results": [], "total_documents": 0,
                    "timing_ms": {"total": 1, "embed": 0, "sql": 1},
                    "session_id": "s-synthetic"},
}


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

    ``/api/notes/<id>`` is answered REFLECTIVELY — the payload is built from the
    id in the URL. That is the whole oracle for "which note opened": a fixed
    payload would render the same title whichever id the app asked for, so the
    inspector would look correct even when the palette opened the wrong note.
    """
    sync_api = pytest.importorskip(
        "playwright.sync_api",
        reason=(
            "Playwright not installed — `pip install -e \".[browser]\"` "
            "(browsers cache outside the venv, so this is a small install)"
        ),
    )

    def route_api(route: Any) -> None:
        path = "/" + route.request.url.split("127.0.0.1:")[-1].split("/", 1)[-1]
        path = path.split("?")[0]
        note = re.fullmatch(r"/api/notes/(?P<id>[^/]+)", path)
        if note is not None:
            note_id = note.group("id")
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "id": note_id,
                    "title": TITLES.get(note_id, f"UNKNOWN {note_id}"),
                    "tier": "vault", "content_type": "note", "draft": False,
                    "tags": [], "source_kind": "manual",
                    "vault_path": f"{note_id}.md", "ingested_at": None,
                    "editable": True, "movable": True,
                    "body": "line\n", "body_hash": "sha256:x",
                    "html": "<p>line</p>",
                }),
            )
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
            pg = browser.new_page()
            pg.route("**/api/**", route_api)
            pg.goto(f"{static_origin}/static/index.html")
            # Short timeout on purpose: a page that did not boot is a blank
            # page, and waiting 30s to learn that wastes a run.
            pg.wait_for_selector('[role="treeitem"]', timeout=8000)
            yield pg
        finally:
            browser.close()


def _mount_palette(page: Any) -> None:
    """Call the REAL ``wirePalette()``; see the module docstring for why."""
    page.evaluate(
        """async () => {
            const mod = await import("/static/js/palette.js");
            mod.wirePalette();
        }"""
    )


def _open_palette(page: Any, query: str) -> list[str]:
    """⌘P, type ``query``, and return the ranked note ids as RENDERED."""
    page.keyboard.press("Meta+p")
    page.wait_for_selector("#palette[open]")
    page.keyboard.type(query)
    page.wait_for_selector('#palette-list [role="option"]')
    return page.eval_on_selector_all(
        '#palette-list [role="option"]', "els => els.map(e => e.dataset.noteId)"
    )


# --------------------------------------------------------------- the palette --


def test_the_palette_mounts_and_ranks_the_loaded_tree_titles(page: Any) -> None:
    """Guard the guard: the test below is vacuous if nothing rendered.

    Also pins the RANKING, because "the third row" is only a meaningful target
    if which note is third is decided by the scorer rather than by luck.
    """
    _mount_palette(page)
    assert _open_palette(page, QUERY) == EXPECTED_ORDER, (
        "the palette ranked the seeded titles differently than js/palette.js "
        "documents; the arrow-key target below is derived from this order"
    )


def test_arrow_keys_then_enter_open_the_note_the_user_landed_on(page: Any) -> None:
    """**THE** T12 test: ⌘P, three characters, ArrowDown x2, Enter.

    The assertion is the IDENTITY of the note that ends up in the inspector,
    not the visibility of the dialog. The stub answers ``/api/notes/<id>``
    reflectively, so the rendered title is evidence of which id was actually
    requested.

    MUTATION THAT MUST GO RED (measured, not assumed): in ``js/palette.js``,
    change ``setHighlight(highlight + delta)`` to ``setHighlight(0)`` — the
    arrow keys stop moving the index and Enter opens ``EXPECTED_ORDER[0]``
    (``Deep Note``) instead of ``EXPECTED_ORDER[2]`` (``Root Note``). Both the
    negative assertion and the positive one below fail, and a "the dialog is
    visible" test would still pass.
    """
    _mount_palette(page)
    ranked = _open_palette(page, QUERY)
    assert ranked[0] != TARGET, (
        "the seed no longer discriminates: the arrow-key target is also the "
        "top-ranked row, so an implementation that ignores ArrowDown passes"
    )

    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")

    page.wait_for_selector(".note-title")
    opened = page.locator(".note-title").inner_text()
    assert opened == TITLES[TARGET], (
        f"Enter opened {opened!r}; two ArrowDowns from the top of "
        f"{ranked} must land on {TITLES[TARGET]!r}. Opening "
        f"{TITLES[ranked[0]]!r} is the signature of a highlight index that "
        "never moves."
    )
    assert page.locator("#palette[open]").count() == 0, (
        "the palette stayed open over the note it just opened"
    )


def test_the_highlight_is_announced_via_aria_activedescendant(page: Any) -> None:
    """The a11y contract: ``role=combobox`` + ``aria-activedescendant``.

    Focus never leaves the input — that is the point of the pattern — so a
    screen reader learns which row is highlighted ONLY from this attribute. A
    palette that moves a CSS class and nothing else is silent to assistive
    tech, and the test above passes either way.
    """
    _mount_palette(page)
    _open_palette(page, QUERY)
    box = page.locator("#palette-input")
    assert box.get_attribute("role") == "combobox"
    assert box.get_attribute("aria-controls") == "palette-list"

    first = box.get_attribute("aria-activedescendant")
    assert page.locator(f"#{first}").get_attribute("aria-selected") == "true"

    page.keyboard.press("ArrowDown")
    moved = box.get_attribute("aria-activedescendant")
    assert moved != first, "aria-activedescendant did not follow ArrowDown"
    assert page.locator(f"#{moved}").get_attribute("aria-selected") == "true"
    assert page.locator('#palette-list [aria-selected="true"]').count() == 1, (
        "more than one option claims to be selected"
    )
    assert page.evaluate("document.activeElement.id") == "palette-input", (
        "focus left the combobox — aria-activedescendant exists precisely so "
        "it does not have to"
    )


def test_a_subsequence_query_narrows_to_the_one_matching_note(page: Any) -> None:
    """Fuzzy, not merely substring: ``dpn`` must find ``Deep Note``.

    ``d…p…n`` appears in order in "deep note" and in none of the other four
    seeded titles, so a substring-only matcher renders an EMPTY list here and a
    matcher that accepts everything renders five rows. Both are caught.
    """
    _mount_palette(page)
    assert _open_palette(page, "dpn") == ["n-deep"]


def test_the_palette_stylesheet_parses_and_marks_the_highlighted_row(
    page: Any,
) -> None:
    """``css/palette.css`` parses, and the highlighted row is really painted.

    WHY THE SHEET IS ATTACHED AT RUNTIME. When this test was written
    ``palette.css`` was not linked from ``index.html`` at all — that one-line
    ``<link>`` belonged to the phase integrator, and extending
    ``check_every_stylesheet_is_linked_in_order``'s exact-match roster was
    pending — so nothing in this suite loaded the file and a typo'd selector or
    an unclosed brace would have shipped green. **Both of those are now done:**
    index.html links it and :data:`CSS_ORDER` lists it, so the fixture's page
    already has the sheet before this runs.

    The runtime attach is therefore no longer the only thing loading the file,
    and it is kept deliberately rather than left behind: appending a second
    ``<link>`` to the same href is idempotent as far as the assertions go — the
    ``cssRules`` count is read from the matching sheet either way — and it keeps
    this test self-sufficient if the ``<link>`` is ever moved or dropped, which
    is exactly the regression the exact-match roster exists to catch elsewhere.

    What it asserts is unchanged, and is two things the browser can only answer
    by having parsed the file: that rules exist, and that the highlighted row is
    actually painted differently from its neighbour. The second is the
    user-visible half — ``aria-activedescendant`` is what a screen reader
    follows, and this is what everyone else follows.

    CSS fails SILENTLY, rule by rule, which is why parsing is asserted at all.
    """
    _mount_palette(page)
    rules = page.evaluate(
        """async () => {
            const link = document.createElement("link");
            link.rel = "stylesheet";
            link.href = "/static/css/palette.css";
            const done = new Promise((ok, no) => {
                link.onload = ok; link.onerror = () => no(new Error("404"));
            });
            document.head.appendChild(link);
            await done;
            const sheet = [...document.styleSheets].find(
                (s) => (s.href || "").endsWith("/css/palette.css"));
            return sheet ? sheet.cssRules.length : 0;
        }"""
    )
    assert rules > 0, "palette.css loaded but produced no CSS rules"

    _open_palette(page, QUERY)
    painted = page.evaluate(
        """() => {
            const rows = [...document.querySelectorAll('#palette-list [role="option"]')];
            const on = rows.find((r) => r.classList.contains("is-active"));
            const off = rows.find((r) => !r.classList.contains("is-active"));
            const read = (n) => [getComputedStyle(n).backgroundColor,
                                 getComputedStyle(n).boxShadow].join("|");
            return { on: read(on), off: read(off) };
        }"""
    )
    assert painted["on"] != painted["off"], (
        "the highlighted row is painted identically to an unhighlighted one — "
        f"both resolve to {painted['on']!r}, so a sighted keyboard user cannot "
        "see where they are in the list"
    )


def test_escape_closes_the_palette_without_opening_anything(page: Any) -> None:
    """Dismissal must not be an accidental navigation.

    ``<dialog>`` handles Escape natively; this asserts the palette does not
    also treat the close as an activation — which is what a naive
    "open whatever is highlighted on close" handler would do, silently
    navigating a user who explicitly backed out.
    """
    _mount_palette(page)
    _open_palette(page, QUERY)
    page.keyboard.press("Escape")
    page.wait_for_selector("#palette[open]", state="detached")
    page.wait_for_timeout(200)
    assert page.locator(".note-title").count() == 0, (
        "Escape opened a note; dismissing the palette navigated the user"
    )
