"""T15 — the summary lede, the source glyphs, and link-kind styling.

Fourth browser harness, alongside ``tests/test_ui_browser.py``,
``tests/test_ui_browser_nav.py`` and ``tests/test_ui_browser_reading.py``, and
built exactly the same way: the real ``index.html``, the real ``css/`` and
``js/`` off disk, every API call stubbed at the network layer. No Postgres, no
Ollama, no contention for the machine-wide test-database lock.

**The filename is load-bearing.** ``tests/test_ci_workflow.py`` discovers every
module carrying the ``browser`` marker and fails if one sits outside the CI
selection, which names the path glob ``tests/test_ui_browser*.py`` rather than a
bare ``-m browser`` (marker deselection happens after collection, and two
modules in this suite open a database connection at import time). This file is
inside the glob by construction.

**WHY THIS IS NOT IN test_ui_browser_reading.py**, which the phase-2 plan's
"where the new tests go" table nominates for T15. That file is 670 lines and the
three surfaces below need roughly 280 more, which lands it at ~950 — past the
800-line ceiling CLAUDE.md sets and past the ceiling the same table cites as its
reason for not growing ``test_ui_routes.py``. The naming rule the table actually
enforces (``test_ui_browser*.py``) is satisfied either way. Deviation recorded
rather than quietly taken.

WHAT REACHABILITY THESE TESTS DO AND DO NOT PROVE
-------------------------------------------------
The JavaScript half is genuinely reachable. ``renderResults`` and
``renderInspector`` are both subscribed by ``js/main.js``'s ``boot()``, and
these tests reach them through the app's own wiring — typing into ``#q`` for
the ledger, and ``inspector.openNote`` for the note pane — not by mounting a
module the way the marginalia and palette suites must.

The STYLESHEET half **now is too, and this paragraph used to say otherwise.**
``css/reading.css`` was written before ``index.html`` linked it, so these tests
attach it at runtime — the same device
``test_the_palette_stylesheet_parses_and_marks_the_highlighted_row`` used while
``palette.css`` was unlinked. **The pair has since landed atomically**: the
``<link>`` is in index.html and ``reading.css`` is in :data:`CSS_ORDER`, so the
``page`` fixture already carries the sheet before any test attaches it.

The runtime attach is KEPT rather than removed, for the reason the palette
suite gives: appending a second ``<link>`` to the same href is idempotent as
far as these assertions go — the rule count is read from the matching sheet
either way — and it keeps this file self-sufficient if the ``<link>`` is ever
moved or dropped, which is exactly the regression the exact-match roster exists
to catch elsewhere.

What that changes about the claims below: the link-kind tests prove the file
parses and that its rules paint four distinguishable treatments, and the
``<link>`` now also puts it in front of a real user. What it does NOT change is
:func:`test_reading_css_never_uses_the_failing_ink_token`, which still earns
its place — see its own docstring.
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

READING_CSS = static_dir() / "css" / "reading.css"

# --------------------------------------------------------------- the ledger --

#: Four synthetic result rows, one per source vocabulary bucket plus one whose
#: source is outside it. Four and not two on purpose: with two rows a "collapse
#: the map to a single default" mutation could be papered over by a two-branch
#: special case, and the unrecognised source is the only row that pins what the
#: FALLBACK is rather than what a known key maps to.
#:
#: Titles are distinct and synthetic — the oracle finds a row BY TITLE and then
#: reads the glyph inside that row. "some glyph exists somewhere in the list"
#: survives the mutation completely and is deliberately not asserted anywhere.
SEARCH_QUERY = "quarterly"
ROWS: list[dict[str, Any]] = [
    # U+FE0F is part of the glyph, not decoration on this line. U+1F399 alone
    # is the TEXT presentation of the studio microphone — a monochrome pictograph
    # — and the overlay's `sourceIcons.ts` spells it with the emoji variation
    # selector, so the two strings are unequal and the app would look different
    # from the wiki it is consolidating. Written as an escape rather than a
    # pasted literal precisely so the selector is visible; a copy-paste of the
    # overlay's character carries it invisibly and the next reader cannot tell
    # whether it is there.
    {"id": "r-krisp", "title": "Quarterly Planning Recording",
     "source_kind": "krisp",
     "glyph": "\N{STUDIO MICROPHONE}\N{VARIATION SELECTOR-16}"},
    {"id": "r-gmail", "title": "Quarterly Contract Follow-Up",
     "source_kind": "gmail", "glyph": "\N{E-MAIL SYMBOL}"},
    {"id": "r-slack", "title": "Quarterly Channel Digest",
     "source_kind": "slack", "glyph": "\N{SPEECH BALLOON}"},
    {"id": "r-other", "title": "Quarterly Vendor Notebook",
     "source_kind": "notion", "glyph": "\N{SEEDLING}"},
]

SEARCH_PAYLOAD: dict[str, Any] = {
    "results": [
        {"id": row["id"], "title": row["title"], "snippet": "synthetic snippet",
         "tags": [], "source_kind": row["source_kind"], "date": "2026-01-04",
         "withheld": None}
        for row in ROWS
    ],
    "total_documents": len(ROWS),
    "timing_ms": {"total": 4, "embed": 1, "sql": 3},
    "session_id": "s-synthetic",
}

# ------------------------------------------------------------ the note pane --

SUMMARY_TEXT = (
    "A synthetic precis of the evaluation, written to stand in for the "
    "LLM-generated documents.summary column."
)

#: The four kinds ``brain.ui.render.classify_link_kind`` actually emits. There
#: are FOUR, not five: the overlay's ``derived`` kind is stripped out of
#: ``documents.content`` by ``vault.sync`` before anything reaches the renderer,
#: so no derived link exists on this path and a rule for one could never be
#: exercised. :func:`test_reading_css_styles_exactly_the_four_emitted_kinds`
#: keeps the stylesheet honest about that.
LINK_KINDS = ("wiki", "ingested", "tag", "external")

_LINKS_HTML = "".join(
    f'<p><a href="#l-{kind}" data-brain-link-kind="{kind}" '
    f'id="a-{kind}">link {kind}</a></p>'
    for kind in LINK_KINDS
)


def _note(note_id: str, **extra: Any) -> dict[str, Any]:
    """A minimal read-mode note payload. ``extra`` is merged last."""
    payload: dict[str, Any] = {
        "id": note_id,
        "title": "Vendor Evaluation",
        "tier": "vault",
        "content_type": "note",
        "draft": False,
        "tags": [],
        "source_kind": "manual",
        "vault_path": "projects/q3/vendor-evaluation.md",
        "ingested_at": None,
        "editable": True,
        "movable": True,
        "body": "# Vendor Evaluation\n",
        "body_hash": "sha256:x",
        "html": "<p>Synthetic body.</p>",
        "headings": [],
    }
    payload.update(extra)
    return payload


#: Keyed by id so one route stub serves every note case.
#:
#: ``n-nosummary`` OMITS the key — that is what the server does, since
#: ``notes_service.read_note`` only sets ``summary`` when ``row.summary is not
#: None``. ``n-blank`` carries the empty string, which is the shape the plan's
#: mutation names and the one a key-presence guard renders an empty aside for.
NOTES: dict[str, dict[str, Any]] = {
    "n-summary": _note("n-summary", summary=SUMMARY_TEXT),
    "n-nosummary": _note("n-nosummary"),
    "n-blank": _note("n-blank", summary=""),
    "n-links": _note("n-links", html=_LINKS_HTML),
}

TREE: dict[str, Any] = {
    # ``count`` is 1, not 0, and it is load-bearing for the FIXTURE rather than
    # for any assertion: ``renderTree`` draws the empty hint instead of the
    # tree when the root count is zero, and the ``page`` fixture waits on a
    # ``[role="treeitem"]`` to know the app booted. A zero here times the
    # fixture out on every test in the file — a broken experiment wearing the
    # costume of a red suite. Measured, not guessed.
    "count": 1, "name": "", "path": "", "empty_hint": "Nothing here yet.",
    "children": [],
    "notes": [{"id": "n-summary", "title": "Vendor Evaluation",
               "path": "projects/q3/vendor-evaluation.md", "draft": False,
               "tier": "vault", "date": "2026-01-04"}],
}

_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": TREE,
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
    "/api/search": SEARCH_PAYLOAD,
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
    """A booted app page with every API call stubbed."""
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
        match = re.fullmatch(r"/api/notes/([^/]+)", path)
        if match is not None:
            note = NOTES.get(match.group(1))
            if note is None:
                route.fulfill(status=404, body="{}",
                              content_type="application/json")
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(note))
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
            pg = browser.new_page(viewport={"width": 1280, "height": 800})
            pg.route("**/api/**", route_api)
            pg.goto(f"{static_origin}/static/index.html")
            # Short timeout on purpose: a page that did not boot is a blank
            # page, and waiting 30s to learn that wastes a run.
            pg.wait_for_selector('[role="treeitem"]', timeout=8000)
            yield pg
        finally:
            browser.close()


def _run_search(page: Any) -> None:
    """Drive the REAL ledger path — type, debounce, stubbed fetch, render.

    Not a ``dispatch({results: …})`` shortcut: ``renderResults`` is subscribed
    by ``boot()`` and typing is how a user reaches it, so this exercises the
    wiring as well as the renderer.
    """
    page.fill("#q", SEARCH_QUERY)
    page.wait_for_selector("#results .result", timeout=8000)


def _glyph_by_title(page: Any) -> dict[str, str]:
    """``{result title: the glyph rendered INSIDE that row}``.

    The join is on the row element, so an assertion can name which row it means.
    A flat list of every glyph on the page would be satisfied by a renderer that
    stamped all four onto one row.
    """
    return page.evaluate(
        """() => Object.fromEntries(
            [...document.querySelectorAll("#results .result")].map((row) => [
              row.querySelector(".result-title").textContent,
              (row.querySelector(".source-icon") || {}).textContent || "",
            ])
        )"""
    )


def _open(page: Any, note_id: str) -> None:
    page.evaluate(
        """async (id) => {
            const inspector = await import("/static/js/inspector.js");
            await inspector.openNote(id);
        }""",
        note_id,
    )
    page.wait_for_selector("#inspector .note-body", timeout=8000)


def _attach_reading_css(page: Any) -> int:
    """Attach ``css/reading.css`` at runtime; return its parsed rule count.

    Written when ``index.html`` did not link the sheet; **it does now**, so the
    fixture's page already has it and this attach is a second, idempotent
    ``<link>`` to the same href — kept deliberately, see the module docstring.
    CSS fails SILENTLY, rule by rule, so the count is asserted by callers
    rather than assumed.
    """
    return page.evaluate(
        """async () => {
            const link = document.createElement("link");
            link.rel = "stylesheet";
            link.href = "/static/css/reading.css";
            const done = new Promise((ok, no) => {
                link.onload = ok; link.onerror = () => no(new Error("404"));
            });
            document.head.appendChild(link);
            await done;
            const sheet = [...document.styleSheets].find(
                (s) => (s.href || "").endsWith("/css/reading.css"));
            return sheet ? sheet.cssRules.length : 0;
        }"""
    )


# ============================================================ source glyphs ==


def test_each_known_source_row_shows_its_own_glyph(page: Any) -> None:
    """The named minority the icon mutation must redden.

    Asserts the glyph BESIDE THE RIGHT ROW, three times. "A glyph exists"
    survives a map collapsed to one default; three per-row equalities do not,
    because krisp, gmail and slack must each differ from the fallback and from
    each other.

    The vocabulary is ported from ``quartz/util/sourceIcons.ts``, which four
    overlay components already share, rather than invented here — the wiki and
    the app must not disagree about what a krisp row looks like.
    """
    _run_search(page)
    glyphs = _glyph_by_title(page)
    for row in ROWS[:3]:
        assert glyphs.get(row["title"]) == row["glyph"], (
            f"the {row['source_kind']} row {row['title']!r} renders "
            f"{glyphs.get(row['title'])!r}, expected {row['glyph']!r}"
        )


def test_an_unrecognised_source_falls_back_to_the_vault_glyph(page: Any) -> None:
    """The fallback is a stated value, not "whatever the map does".

    Separate from the test above so a collapsed map reddens a NAMED minority:
    this one keeps passing (the fallback is what a collapsed map returns),
    which is what makes the other test's failure diagnostic rather than a
    blanket red.
    """
    _run_search(page)
    row = ROWS[3]
    assert _glyph_by_title(page).get(row["title"]) == row["glyph"]


def test_the_glyph_is_hidden_from_assistive_tech(page: Any) -> None:
    """The source is already spelled out in words one line below the glyph.

    An emoji announced by a screen reader as "studio microphone" beside the
    word "krisp" is duplication, not information, so the decorative half is
    marked as such.
    """
    _run_search(page)
    hidden = page.eval_on_selector_all(
        "#results .result .source-icon",
        "els => els.map(e => e.getAttribute('aria-hidden'))",
    )
    assert hidden == ["true"] * len(ROWS), hidden


def test_the_source_kind_is_still_spelled_out_beside_the_glyph(page: Any) -> None:
    """The glyph is added TO the gutter, not instead of its text.

    Pins that this change is additive: an icon-only gutter would be a
    regression for anyone who does not read the emoji, and it is exactly what a
    careless implementation of "put an icon in the row" produces.
    """
    _run_search(page)
    texts = page.eval_on_selector_all(
        "#results .result .gutter",
        "els => els.map(e => e.textContent)",
    )
    for row, text in zip(ROWS, texts, strict=True):
        assert row["source_kind"] in text, (
            f"the gutter for {row['title']!r} lost the word "
            f"{row['source_kind']!r}: {text!r}"
        )


# ============================================================ summary lede ===


def test_a_note_with_a_summary_renders_the_lede(page: Any) -> None:
    """THE PREMISE. Absence is only evidence once presence is established.

    This test and the two below run against the SAME renderer and differ only
    in the payload. Under the "drop the emptiness guard" mutation this one
    stays GREEN while both absence tests redden — one run then shows both that
    there was something to render and that the guard is what suppressed it.
    A lone absence assertion cannot distinguish "correctly suppressed" from
    "the feature does not exist".
    """
    _open(page, "n-summary")
    lede = page.query_selector("#inspector .lede")
    assert lede is not None, "a note carrying documents.summary rendered no lede"
    assert lede.text_content().strip() == SUMMARY_TEXT


def test_the_lede_sits_above_the_body(page: Any) -> None:
    """A lede below the text it introduces is not a lede.

    Document order, not geometry: both elements are in the normal flow, so
    ``compareDocumentPosition`` is the exact question and needs no scrolling.
    """
    _open(page, "n-summary")
    following = page.evaluate(
        """() => {
            const lede = document.querySelector("#inspector .lede");
            const body = document.querySelector("#inspector .note-body");
            return Boolean(lede.compareDocumentPosition(body)
                           & Node.DOCUMENT_POSITION_FOLLOWING);
        }"""
    )
    assert following, "the lede renders after the note body"


def test_the_body_is_the_second_inspector_child_even_with_a_lede(page: Any) -> None:
    """POSITION, at the shared host — the claim counting cannot make.

    ``.inspector`` is ``grid-template-rows: auto 1fr``, so the host takes
    exactly two children and the body/editor must be the second one. The lede
    shipped for one iteration as a THIRD host child sitting between them, which
    puts the lede on the fill track and the body on an implicit auto row.

    WHY THIS IS NOT A DUPLICATE OF ``check_resize_is_not_inert``. That guard
    reads ``js/inspector.js`` as TEXT and pins ``host.appendChild(`` at five.
    It is a good guard and it is the one that caught the defect — but it is
    satisfiable by editing the expected number from 5 to 6, which is the
    tempting fix and the wrong one: the count goes green while the body is
    still off the fill track. This test asserts the RENDERED tree, so it stays
    red through that edit. The mutation entry for it is deliberately the
    combined one — put the lede back on ``host`` AND raise the count guard to 6
    — because a mutation that reddens both proves nothing about which guard is
    load-bearing.

    ``test_the_lede_sits_above_the_body`` is also not this test. It compares
    document position, which is satisfied by a lede inside ``.note-head`` and
    equally by a lede wrongly parented to the host — both precede the body.
    Ordering and parentage are two claims and they need two assertions.
    """
    _open(page, "n-summary")
    shape = page.evaluate(
        """() => {
            const host = document.getElementById("inspector");
            const kids = [...host.children];
            const lede = host.querySelector(".lede");
            return {
                classes: kids.map((n) => n.className),
                ledeParent: lede ? lede.parentElement.className : null,
            };
        }"""
    )
    assert shape["ledeParent"] == "note-head", (
        "the lede is parented to "
        f"{shape['ledeParent']!r}, not to the .note-head wrapper — new "
        "furniture above the body goes INSIDE head, never beside it"
    )
    # POSITION, NOT A ROSTER — and the difference was measured, not reasoned.
    # This assertion was first written as `classes == ["note-head",
    # "note-body"]` and it FAILED on the shipped source, because `.marginalia`
    # is already a third host child: T14 appends its rail LAST, from
    # `js/marginalia.js`, and documents that it must. So "exactly two children"
    # is simply false, and an exact-list assertion would have been a correct
    # test of an invariant nobody holds.
    #
    # That failure is also the sharpest available evidence that this test is
    # not a duplicate of `check_resize_is_not_inert`. THAT GUARD CANNOT SEE
    # MARGINALIA AT ALL — it counts `host.appendChild(` inside
    # `renderInspector`'s text, in one file, so a third child contributed by
    # any other module is invisible to it. What it really pins is "this
    # function adds no third child", which is narrower than the layout rule it
    # cites. The layout rule is that the body/editor lands on the `1fr` track,
    # i.e. is the SECOND child; anything after that is on an implicit auto row
    # and is safe because `.inspector > * { align-self: start }`. That is the
    # property asserted here, and only a rendered tree can answer it.
    assert shape["classes"][:2] == ["note-head", "note-body"], (
        f"#inspector's first two children are {shape['classes'][:2]}, expected "
        "['note-head', 'note-body'] (full list: "
        f"{shape['classes']}). `.inspector` is `grid-template-rows: auto 1fr`; "
        "a child inserted between them takes the fill track and pushes the "
        "body/editor onto an implicit auto row, where `align-self: stretch` on "
        ".editor has nothing to stretch into."
    )


def test_a_note_without_a_summary_renders_no_empty_aside(page: Any) -> None:
    """ABSENCE, asserted as absence.

    ``query_selector_all(".lede")`` must be EMPTY. Asserting that some
    container exists and is blank is the assertion that survives the defect:
    an empty aside still has a container, and on a corpus where most documents
    predate enrichment it would draw a rule and a block of padding above every
    one of them.
    """
    _open(page, "n-nosummary")
    assert page.query_selector_all("#inspector .lede") == []


def test_a_blank_summary_renders_no_empty_aside(page: Any) -> None:
    """The empty STRING, not the missing key — the plan's named mutation shape.

    A guard written as ``if ("summary" in note)`` passes the missing-key test
    above and fails here, which is why both exist. Whitespace is included in
    the payload-shaped cases the implementation must treat as empty.
    """
    _open(page, "n-blank")
    assert page.query_selector_all("#inspector .lede") == []


# ======================================================== link-kind styling ===


def test_reading_css_parses(page: Any) -> None:
    """CSS fails silently, rule by rule. A typo'd selector ships green.

    This is also the one test that would notice ``reading.css`` going missing
    from the wheel — ``static_dir()`` is the installed path, and the
    ``package-data`` globs do not cross ``/``.
    """
    assert _attach_reading_css(page) > 0, (
        "reading.css loaded but produced no CSS rules"
    )


def test_the_four_link_kinds_are_painted_distinguishably(page: Any) -> None:
    """Four kinds, four treatments — asserted pairwise, not "some rule exists".

    The fingerprint spans colour, the underline's style, the chip ground and
    the ``::after`` glyph, because that is the axis each kind actually differs
    on: ``wiki`` and ``external`` share a colour and are told apart only by the
    trailing mark, so a fingerprint reading ``color`` alone would call them
    identical and this test would fail for the wrong reason.
    """
    _attach_reading_css(page)
    _open(page, "n-links")
    prints = page.evaluate(
        """(kinds) => Object.fromEntries(kinds.map((kind) => {
            const node = document.getElementById(`a-${kind}`);
            const own = getComputedStyle(node);
            const after = getComputedStyle(node, "::after");
            return [kind, [own.color, own.textDecorationLine,
                           own.textDecorationStyle, own.backgroundColor,
                           after.content].join("|")];
        }))""",
        list(LINK_KINDS),
    )
    for left in LINK_KINDS:
        for right in LINK_KINDS:
            if left >= right:
                continue
            assert prints[left] != prints[right], (
                f"{left} and {right} links are painted identically "
                f"({prints[left]!r}), so the reader cannot tell a vault link "
                "from one that leaves the app"
            )


def test_reading_css_styles_exactly_the_four_emitted_kinds() -> None:
    """No rule for a kind nothing emits — and no kind left unstyled.

    COMMENTS ARE STRIPPED FIRST, and that is the point rather than tidiness: a
    file-wide substring search is satisfied by prose, and this file's own
    header explains at length why ``derived`` is absent. Searching the raw text
    would find the word in that explanation and report the rule as present.
    Scoped to the declaration region, the check means what it says.
    """
    source = re.sub(r"/\*.*?\*/", "", READING_CSS.read_text(encoding="utf-8"),
                    flags=re.DOTALL)
    found = set(re.findall(r'data-brain-link-kind="([^"]+)"', source))
    assert found == set(LINK_KINDS), (
        f"reading.css styles {sorted(found)}, expected {sorted(LINK_KINDS)}. "
        "`derived` has no producer on this path — vault.sync strips the "
        "evidence fence before the renderer sees it — so a rule for it could "
        "never be exercised."
    )


def test_reading_css_never_uses_the_failing_ink_token() -> None:
    """Kept AFTER the roster gap closed — and the reason changed, so it is
    restated rather than left to read as though nothing moved.

    ``--ink-faint`` fails AA as text on every ground in both themes.
    ``check_ink_faint_used_once`` only sees stylesheets listed in
    :data:`CSS_ORDER`, and when this test was written ``reading.css`` was not
    listed, so that guard did not look at this file even though it shipped —
    the same blind spot in which ``marginalia.css`` carried a real contrast
    defect. **``reading.css`` is now in the roster**, so the original
    justification has expired.

    WHAT IT IS FOR NOW. Two things the roster guard does not give:
    (1) it is scoped to THIS file, so its failure names reading.css instead of
    reporting a whole-front-end count that a reader must then bisect; and
    (2) it survives removal from the roster. Roster membership is
    hand-maintained — that is a documented weakness of :data:`CSS_ORDER`, not a
    hypothetical — and a sheet dropped from it silently loses every ``css/*``
    guard. This assertion does not depend on membership at all: it reads the
    file off disk.

    Asserting ZERO rather than "at most one" is correct and not merely
    stricter: the single permitted use in the whole front end is
    ``components.css``'s decorative ``.search-glyph``, so any use here is a
    second one, which is exactly what the roster guard refuses.
    """
    source = re.sub(r"/\*.*?\*/", "", READING_CSS.read_text(encoding="utf-8"),
                    flags=re.DOTALL)
    assert "var(--ink-faint)" not in source, (
        "reading.css uses --ink-faint, which fails AA as text on every ground "
        "in both themes; use --ink-muted (5.66-7.27:1 over the same pairings)"
    )
