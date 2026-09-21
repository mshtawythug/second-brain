"""T11 — Back and Forward, EXECUTED in a real browser.

Fourth harness alongside ``test_ui_browser.py``, ``test_ui_browser_nav.py`` and
``test_ui_browser_reading.py``, built the same way: the real ``index.html``, the
real ``css/`` and ``js/`` off disk, every API call stubbed at the network layer.
No Postgres, no Ollama, no contention for the machine-wide test-database lock.

**The filename is load-bearing.** ``tests/test_ci_workflow.py`` discovers every
module carrying the ``browser`` marker and fails if one sits outside the CI
selection, which names the path glob ``tests/test_ui_browser*.py``. This file is
inside it by construction.

WHY THESE TESTS EXIST IN A BROWSER AND NOT AS SOURCE GUARDS. Everything here is
executable: a history stack, a Back button, and what is on screen afterwards.
``tests/test_ui_static_behaviour.py`` takes source assertions only for
invariants with no rendering that can express them, and this has the opposite
problem — it has a rendering and nothing else would be honest. No guard was
added for T11 for that reason.

**THE ORACLE IS THE RENDERED TITLE, NEVER ``history.length``.** A length
assertion is satisfied whether or not the right note is on screen — the stack
grows identically when Back restores the wrong document — and, worse, it is
*also* satisfied when typing pushes an entry per keystroke, which is the exact
defect the debounce exception exists to prevent. It would pass in both
directions of the thing it is supposed to measure. So every assertion below
reads the ``<h1>`` the inspector actually rendered, and the URL's ``id``
alongside it.

``/api/notes/<id>`` is answered REFLECTIVELY, from the id in the request URL. A
fixed payload would render the same title whichever note the app asked for, so
the inspector would look correct even when Back restored the wrong one — the
oracle would be measuring the stub instead of the app.

INDEPENDENT CLAIMS, one test each, one mutation each. Neither of the first two
implies the other: an implementation that pushes everything passes (1) and fails
(2); one that pushes nothing fails (1) and passes (2).

MUTATION RESULTS — RE-MEASURED 2026-08-20. Every number below comes from that
run, not from the authoring one, because two of them did not survive it.

INVOCATION, stated because a count without one is not reproducible:

    .venv/bin/python -m pytest tests/test_ui_browser_history.py -m browser --no-cov

Baseline on that invocation: 4 passed. Every mutant was syntax-checked first —
`node --check` on a `.mjs` copy, because `.js` parses as CommonJS and would let a
broken ES module through — so "the run exited 1" can never stand in for "the
assertion I aimed at failed". Every file restored byte-exact, sha256 verified
both ways.

  A. store.js:128 — the navigation branch, `pushState` -> `replaceState`.
     -> 3 failed, 1 passed.
        * `test_back_...` at "Back left None in the inspector": with every write
          a replace the page holds only its boot entry, so Back leaves the
          document for about:blank.
        * `test_forward_...` at the window-probe assertion.
        * `test_back_to_a_query_less_state_...` — and NOT at its own Back
          assertion. On about:blank the two assertions above it hold vacuously
          (no `q=` in the URL, no result rows in a document that has none), and
          the test dies on the third, `document.getElementById("q").value`,
          with "Cannot read properties of null". Red for a reason unrelated to
          the stale ledger it exists to catch — which is why the failing
          ASSERTION is recorded here and not merely the failing test.
        The typing test was untouched, as it must be.
  B. store.js:135 — the debounced branch, `replaceState` -> `pushState`.
     -> 1 failed, 3 passed. `test_typing_...` at "one Back left id='n-first' in
        the URL". Both navigation tests stayed green.
  C. main.js:161 — the `popstate` listener registration deleted.
     -> 2 failed, 2 passed.
        * `test_back_...` at the same assertion as A but with a different
          observed value ('Budget Review' still on screen, rather than
          about:blank). Recorded because it is why that assertion's message
          names two possible causes and asserts neither.
        * `test_back_to_a_query_less_state_...` at its own assertion, "Back left
          1 result row(s) in the ledger": the app never reacts to Back, so the
          ledger keeps the row renderResults last drew.
        `test_forward_...` stays GREEN under C, and that is containment rather
        than an escape: with popstate unwired, Back and Forward move the URL and
        change nothing on screen, so the second note is still rendered when
        Forward returns to it and the same-document probe survives. The Back
        test above is the one that notices.
  D. main.js:142 — `runSearch()` re-guarded with `if (state.q)`, the original
     bug. -> 1 failed, 3 passed. `test_back_to_a_query_less_state_...` at "Back
     left 1 result row(s) in the ledger".

THE STRUCTURAL CLAIM SURVIVED THE RE-RUN; THE ARITHMETIC DID NOT. A and B still
redden DISJOINT sets — A reddens {back, forward, query-less}, B reddens {typing}
— so claims (1) and (2) are still independent rather than two spellings of one
property. A pushes-nothing implementation fails the navigation tests and passes
the typing test; a pushes-everything one does the reverse. Only the counts were
wrong.

WHY THEY WERE WRONG — AND IT IS NOT DRIFT, WHICH MATTERS. A and C were recorded
as 2F/2P and 1F/3P, both omitting `test_back_to_a_query_less_state_...`. Nothing
that changed since can explain that. This file has exactly one commit (3b16527)
and is byte-identical to it, so those runs saw the text above. And the omission
is unreachable under ANY version of the app: under A every history write is a
replace, so the page holds one entry and Back leaves the document — a property of
the browser and the mutation, independent of results.js, of pagination, of
everything the two later commits touched. Under C the app never reacts to Back at
all, and record D's own observed message ("Back left 1 result row(s) in the
ledger") proves that same assertion was reachable and falsifiable when these
records were written. So A and C were never re-run after this fourth test joined
the file. The failure is a number written once and never re-derived, not a tree
that moved underneath it.

ONE FURTHER CLAIM IN THIS BLOCK HAD EXPIRED and is corrected rather than dropped:
it read "no `git checkout --` (these files are untracked, so it could not have
restored them anyway)". They ARE tracked now — `git ls-files --error-unmatch
src/brain/ui/static/js/store.js` succeeds. The byte-exact copy + sha256 method is
kept because it is the stronger restore, not because git cannot do it.
"""
from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from brain.ui.app import static_dir

pytestmark = pytest.mark.browser

#: A two-note vault. Synthetic throughout — no real titles, paths or ids.
#: Two is the minimum that can distinguish "Back restored the previous note"
#: from "Back left the current one on screen", and the titles are deliberately
#: dissimilar so a failure message names which one is showing.
TREE: dict[str, Any] = {
    "count": 2,
    "name": "",
    "path": "",
    "empty_hint": "Nothing here yet.",
    "children": [],
    "notes": [
        {"id": "n-first", "title": "Vendor Evaluation",
         "path": "vendor-evaluation.md", "draft": False,
         "tier": "vault", "date": "2026-01-04"},
        {"id": "n-second", "title": "Budget Review",
         "path": "budget-review.md", "draft": False,
         "tier": "vault", "date": "2026-01-05"},
    ],
}

#: id -> title, DERIVED from TREE rather than restated, so editing the seed
#: cannot leave an expectation pointing at a note that no longer exists.
TITLES: dict[str, str] = {note["id"]: note["title"] for note in TREE["notes"]}

FIRST, SECOND = "n-first", "n-second"

_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": TREE,
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
    #: ONE result, not zero, and that is load-bearing. `renderResults` empties
    #: the ledger and returns on `searchStatus === "idle"`, so with an empty
    #: result set "the stale list was cleared" and "there was never a list"
    #: are the same DOM — and the staleness test below could not fail.
    "/api/search": {"results": [{"id": SECOND, "title": "Budget Review",
                                 "source_kind": "manual", "date": "2026-01-05",
                                 "snippet": "a synthetic snippet", "tags": []}],
                    "total_documents": 1,
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
                    "html": "<p>line</p>", "headings": [],
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


#: Predicate used only to let the app finish reacting. NOT an oracle — see
#: :func:`_settle`.
_TITLE_IS = """(expected) => {
    const h1 = document.querySelector("#inspector h1");
    return h1 !== null && h1.textContent === expected;
}"""


def _settle(page: Any, arg: Any) -> None:
    """Wait for the app to finish reacting. TIMING ONLY, NEVER THE ORACLE.

    A ``wait_for_function`` whose predicate IS the property under test looks
    like a strong check and is a weak report: when it fails it raises
    ``TimeoutError: Timeout 4000ms exceeded`` and the informative assertion
    below it — the one that would say WHICH note is actually on screen — never
    runs, because it is unreachable once the wait throws. The first mutation run
    of this file did exactly that: "2 failed", both of them bare timeouts,
    naming no observed value.

    So the timeout is swallowed here and the assertion is left to speak. If the
    app settled, the assertion passes and the wait cost nothing; if it did not,
    the assertion reports what is there instead of what the clock did. Same
    lesson the marginalia scroll test recorded from the other side: a wait and
    an assertion are different instruments, and only one of them is a witness.
    """
    _settle_until(page, _TITLE_IS, arg=arg)


def _settle_until(page: Any, predicate: str, arg: Any = None) -> None:
    """The general form of :func:`_settle`. Same rule, same reason.

    Every bounded wait in this module goes through here, so no test can grow a
    wait that doubles as its oracle. That has now happened twice in this file
    during authoring — once on the title predicate, once on a URL predicate —
    and both times the mutation run reported a bare "Timeout 4000ms exceeded"
    naming nothing. A rule that has to be remembered per call site is not a
    rule; routing every wait through one helper is.
    """
    with contextlib.suppress(Exception):  # non-fatal by design — see above
        page.wait_for_function(predicate, arg=arg, timeout=4000)


def _open(page: Any, note_id: str) -> None:
    """Open a note through the REAL ``openNote``, then wait for it on screen.

    Waiting for the TITLE rather than a fixed delay: the assertions downstream
    are about which note is rendered, so the setup has to be certain the render
    happened at all. A sleep here would turn a genuine failure into a flake.
    """
    page.evaluate(
        """async (id) => {
            const inspector = await import("/static/js/inspector.js");
            await inspector.openNote(id);
        }""",
        arg=note_id,
    )
    _settle(page, TITLES[note_id])
    assert _shown_title(page) == TITLES[note_id], (
        f"setup failed: opening {note_id!r} left {_shown_title(page)!r} in the "
        f"inspector, expected {TITLES[note_id]!r}. Nothing below this line is "
        f"measuring history."
    )


def _shown_title(page: Any) -> str | None:
    return page.evaluate(
        """() => {
            const h1 = document.querySelector("#inspector h1");
            return h1 === null ? null : h1.textContent;
        }"""
    )


def _url_param(page: Any, key: str) -> str | None:
    values = parse_qs(urlparse(page.url).query).get(key)
    return values[0] if values else None


def _type_query(page: Any, text: str) -> None:
    """Type into the real search box, one keystroke at a time.

    ``fill()`` would set the value in one shot and fire a single ``input``
    event, which is precisely the case this test must NOT measure: the defect
    is one history entry PER CHARACTER, so the characters have to arrive
    separately.
    """
    page.click("#q")
    page.type("#q", text, delay=20)
    # Past the 200ms URL debounce, so the replaceState has certainly run and a
    # buggy pushState would certainly have landed. Without this the test could
    # pass by outrunning the bug.
    page.wait_for_timeout(400)


#: A value stamped onto ``window`` that CANNOT survive a document navigation.
#:
#: Back and Forward between two notes must be same-document ``popstate`` moves.
#: If the entries are not really in the page's history, the browser leaves the
#: document instead — and a Forward that comes back RELOADS the app, which then
#: boots, reads the URL, and opens the right note anyway. The screen therefore
#: looks correct and the feature is absent, which is this project's signature
#: defect in yet another costume. Measured, not hypothesised: with `pushState`
#: reverted to `replaceState`, `test_forward_...` PASSED on the rendered title
#: alone, because Back had gone to `about:blank` and Forward re-entered the app
#: from scratch. This probe is what makes that distinguishable.
_PROBE = "same-document"


def _mark(page: Any) -> None:
    page.evaluate("""(value) => { window.__historyProbe = value; }""", arg=_PROBE)


def _probe(page: Any) -> Any:
    return page.evaluate("""() => window.__historyProbe || null""")


# ------------------------------------------- claim 1: navigations are pushed --


def test_back_after_opening_two_notes_shows_the_first_note_again(page: Any) -> None:
    """THE T11 test. Open A, open B, Back -> A is on screen and in the URL.

    Asserts the RENDERED TITLE, not ``history.length`` — see the module
    docstring for why a length assertion cannot fail in either direction that
    matters. The URL check rides alongside so a Back that restored the document
    but not the address bar (leaving a shareable link to the wrong note) is
    caught too.
    """
    _open(page, FIRST)
    _open(page, SECOND)
    assert _shown_title(page) == TITLES[SECOND], "the second note never opened"
    _mark(page)

    page.go_back()
    _settle(page, TITLES[FIRST])

    assert _shown_title(page) == TITLES[FIRST], (
        f"Back left {_shown_title(page)!r} in the inspector; expected "
        f"{TITLES[FIRST]!r}. Two different causes produce this and the message "
        f"deliberately names neither as fact: the navigation was recorded with "
        f"replaceState (so the second note overwrote the first's entry and "
        f"there is nothing to go back to), or popstate is not wired (so the "
        f"URL moved and the app did not). Check which before fixing."
    )
    assert _url_param(page, "id") == FIRST, (
        f"the URL's id is {_url_param(page, 'id')!r} after Back, expected "
        f"{FIRST!r} — the document was restored but the address bar was not, "
        f"so copying the link would share the wrong note"
    )
    assert _probe(page) == _PROBE, (
        "Back left the document entirely — the window probe is gone, so this "
        "was not a same-document popstate. Whatever is on screen was rebuilt "
        "by a fresh boot, not restored from history."
    )


def test_forward_after_back_returns_to_the_second_note(page: Any) -> None:
    """Forward is not implied by Back, and is a different failure.

    A handler that rebuilt state only when going backwards — or one that pushed
    a fresh entry while restoring, truncating the forward stack — passes the
    test above and strands the user here. ``popstate`` fires for both
    directions; this is what says so.
    """
    _open(page, FIRST)
    _open(page, SECOND)
    _mark(page)

    page.go_back()
    _settle(page, TITLES[FIRST])
    page.go_forward()
    _settle(page, TITLES[SECOND])

    assert _shown_title(page) == TITLES[SECOND], (
        f"Forward left {_shown_title(page)!r} on screen, expected "
        f"{TITLES[SECOND]!r}"
    )
    assert _url_param(page, "id") == SECOND
    assert _probe(page) == _PROBE, (
        "the round trip left and re-entered the document. The rendered title "
        "above is then satisfied by a fresh boot reading the URL, so it proves "
        "nothing about popstate — which is exactly what this assertion is here "
        "to separate."
    )


# ----------------------------------------------- claim 2: typing is not pushed --


def test_typing_adds_no_history_entries_so_back_skips_the_keystrokes(
    page: Any,
) -> None:
    """The debounce exception, as a user-visible property.

    Open a note (ONE navigation), then type four characters. Back must return
    to the pre-note state — no ``id`` at all — because the four keystrokes
    contributed nothing to the stack.

    If typing pushed, Back would land on an intermediate keystroke URL, which
    still carries ``id=n-first``. That is what this asserts: the ABSENCE of the
    id after a single Back. Stating it that way means the test fails for the
    right reason (one Back was not enough) rather than by counting anything.

    ANTI-VACUITY: the ``q`` assertion at the end pins that the typing actually
    reached the URL. Without it, an implementation that ignored typing
    altogether — never syncing the query at all — would satisfy the Back
    assertion for entirely the wrong reason, and this test would certify a
    search box that produces no shareable link.
    """
    _open(page, FIRST)
    assert _url_param(page, "id") == FIRST

    _type_query(page, "vend")
    assert _url_param(page, "q") == "vend", (
        f"typing did not reach the URL at all (q={_url_param(page, 'q')!r}); "
        f"the Back assertion below would then hold for the wrong reason"
    )
    assert _url_param(page, "id") == FIRST, "typing dropped the open note"

    page.go_back()
    _settle_until(page, "() => !location.search.includes('id=')")

    assert _url_param(page, "id") is None, (
        f"one Back left id={_url_param(page, 'id')!r} in the URL. The four "
        f"keystrokes each pushed a history entry, so Back walks the query one "
        f"character at a time instead of returning to where the user was."
    )
    assert _url_param(page, "q") is None, (
        f"one Back left q={_url_param(page, 'q')!r}; the pre-note state had no "
        f"query, so this is an intermediate keystroke entry"
    )


def _result_rows(page: Any) -> int:
    return page.evaluate("""() => document.querySelectorAll("#results a.result").length""")


def test_back_to_a_query_less_state_clears_the_result_list(page: Any) -> None:
    """Found in review by the agent that collided with this task on T11.

    ``onPopState`` originally called ``runSearch()`` behind ``if (state.q)``.
    Going Back to a state with no query therefore restored the note and the
    URL, and left the PREVIOUS result list sitting in the ledger under an empty
    search box — a ledger describing a search the address bar no longer says was
    made. Worse than a stale note, because nothing about it looks wrong: the
    rows are real, they are just answers to a question that has been retracted.

    The fix is to call ``runSearch()`` unconditionally; it already clears and
    returns to ``idle`` on an empty query, so the emptying rule stays in
    results.js where it belongs rather than being copied into main.js.

    ANTI-VACUITY, and it needed two halves. The first assertion pins that rows
    were ever there — with a zero-result stub the ledger is empty in both the
    fixed and the broken app, and this test would pass against the defect. The
    ``#q`` assertion pins that the search box was emptied too, so a Back that
    cleared the rows but left the query text would still be caught.
    """
    _open(page, FIRST)
    _type_query(page, "vend")

    assert _result_rows(page) > 0, (
        "setup failed: no result rows rendered, so 'the list was cleared' "
        "below would be indistinguishable from 'there was never a list'"
    )

    page.go_back()
    _settle_until(page, "() => !location.search.includes('q=')")

    assert _url_param(page, "q") is None, "Back did not leave the query state"
    assert _result_rows(page) == 0, (
        f"Back left {_result_rows(page)} result row(s) in the ledger while the "
        f"URL carries no query. The list answers a search the address bar no "
        f"longer records."
    )
    assert page.evaluate("""() => document.getElementById("q").value""") == "", (
        "the search box still holds the old query after Back, so the control, "
        "the URL and the ledger disagree about what was searched for"
    )
