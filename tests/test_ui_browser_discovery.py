"""T17 — the ledger's idle state, EXECUTED in a real browser.

Fifth harness alongside ``test_ui_browser.py``, ``…_nav.py``, ``…_reading.py``
and ``…_lede.py``, built the same way: the real ``index.html``, the real
``css/`` and ``js/`` off disk, every API call stubbed at the network layer. No
Postgres, no Ollama, no contention for the machine-wide test-database lock.

**The filename is load-bearing.** ``tests/test_ci_workflow.py`` discovers every
module carrying the ``browser`` marker and fails if one sits outside the CI
selection, which names the path glob ``tests/test_ui_browser*.py``.

**NOTHING HERE MOUNTS THE MODULE ITSELF, and that is the point.** The palette,
marginalia and reading suites all call their ``wireX()`` from ``page.evaluate``
because ``js/main.js`` was owned elsewhere, and every one of them passed while
the feature was unreachable from a booted app. ``js/discovery.js`` is loaded by
``index.html``'s own ``<script type="module">`` and mounts itself, so simply
booting the page is the reachability test. Delete that script tag and every
assertion below fails.

WHAT THESE TESTS DO NOT PROVE, stated because the plan's acceptance criterion
invites the opposite reading. "No drafts, no ``people/`` pages" is a SERVER
claim, enforced by ``_DISCOVERABLE`` in ui/queries.py and asserted against the
real route payload by ``test_recent_hides_drafts_and_the_people_hub`` and
``test_tag_page_hides_drafts_and_the_people_hub`` in
tests/test_ui_routes_discovery.py. The client cannot re-check it: the payload is
``{id, title, vault_path, source_kind, date}`` and carries **no draft flag at
all**. A client-side draft filter is therefore not redundant-but-harmless, it is
unimplementable — and asserting it here against a stub the test itself wrote
would be a test that cannot fail. See the T17 finding filed against the plan's
mutation column.
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

#: The seeded rail. Synthetic throughout — no real titles, paths or ids.
#: Deliberately NOT alphabetical and NOT date-sorted by title, so "the rail
#: rendered the payload in the order the server sent it" is a claim that can
#: fail: a client that re-sorted by title or by date would produce a different
#: sequence than this one.
RECENT: list[dict[str, Any]] = [
    {"id": "n-third", "title": "Zoning Notes", "vault_path": "zoning.md",
     "source_kind": "manual", "date": "2026-03-04"},
    {"id": "n-first", "title": "Alpha Retro", "vault_path": "alpha.md",
     "source_kind": "krisp", "date": "2026-03-02"},
    {"id": "n-second", "title": "Budget Review", "vault_path": "budget.md",
     "source_kind": "gmail", "date": "2026-02-27"},
]

#: The titles in payload order — DERIVED, so editing the seed cannot strand an
#: expectation on a document that no longer exists.
RECENT_ORDER: list[str] = [doc["title"] for doc in RECENT]

#: The tag index. ``vendors`` is the interesting one: its count (5) deliberately
#: EXCEEDS the two documents ``/api/tags/vendors`` returns.
#:
#: Since ``/api/tags`` moved to ``browseable_tag_counts`` that shortfall has one
#: production cause — ``routes_discovery.TAG_PAGE_LIMIT`` truncating a long tag
#: page — rather than the mixture of drafts, generated pages and sensitivity it
#: represented before. The stub uses small numbers instead of 51 rows because
#: what is under test is the CLIENT's behaviour when ``shown < indexed``, and
#: the client cannot see the cap either way. ``solo`` agrees with its
#: click-through, so the reconciliation line must NOT appear there.
TAGS: list[dict[str, Any]] = [
    {"value": "solo", "count": 1},
    {"value": "vendors", "count": 5},
]

TAG_DOCUMENTS: dict[str, list[dict[str, Any]]] = {
    "vendors": [RECENT[0], RECENT[2]],
    "solo": [RECENT[1]],
}

_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": {"count": 0, "name": "", "path": "", "empty_hint": "Nothing here yet.",
                  "children": [], "notes": []},
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
    "/api/search": {"results": [], "total_documents": 0,
                    "timing_ms": {"total": 1, "embed": 0, "sql": 1},
                    "session_id": "s-synthetic"},
    "/api/recent": {"documents": RECENT, "count": len(RECENT)},
    "/api/tags": {"tags": TAGS, "count": len(TAGS)},
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

    ``/api/tags/<tag>`` is answered from :data:`TAG_DOCUMENTS` by the tag in the
    URL — reflectively, like the note route in the sibling suites. A fixed
    payload would return the same documents whichever tag was clicked, so the
    ledger would look correct even when the click sent the wrong tag, and the
    oracle would be measuring the stub instead of the app.
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
        tag = re.fullmatch(r"/api/tags/(?P<tag>[^/]+)", path)
        if tag is not None:
            name = tag.group("tag")
            documents = TAG_DOCUMENTS.get(name, [])
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"tag": name, "documents": documents, "count": len(documents)}
                ),
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
            # The rail is the thing under test, so waiting for it here would
            # make every failure a fixture error. Wait for the module to have
            # mounted at all, then let each test assert what it renders.
            pg.wait_for_selector("#discovery", state="attached", timeout=8000)
            yield pg
        finally:
            browser.close()


def _rail_titles(page: Any) -> list[str]:
    return page.eval_on_selector_all(
        "#discovery .disc-list .disc-title", "els => els.map(e => e.textContent)"
    )


def _tag_labels(page: Any) -> list[str]:
    return page.eval_on_selector_all(
        "#discovery .disc-tag-name", "els => els.map(e => e.textContent)"
    )


def _notes(page: Any) -> list[str]:
    return page.eval_on_selector_all(
        "#discovery .disc-note", "els => els.map(e => e.textContent)"
    )


def _click_tag(page: Any, tag: str) -> None:
    page.click(f'#discovery .disc-tag[data-tag="{tag}"]')
    page.wait_for_selector("#discovery .disc-back", timeout=4000)


# ------------------------------------------- claim 1: the rail is reachable --


def test_booting_with_no_query_fills_the_idle_ledger_with_the_recent_rail(
    page: Any,
) -> None:
    """THE T17 acceptance test: boot, no query, the rail is on screen.

    Asserts the rendered titles IN PAYLOAD ORDER. The seed is neither
    alphabetical nor title-sorted, so a client that re-sorted would produce a
    different sequence and fail here — "the rail is visible" would not.

    Nothing in this test mounts js/discovery.js. It is reached only through
    index.html's <script type="module">, so this doubles as the reachability
    assertion the palette shipped without.

    MUTATION, MEASURED: in js/discovery.js's renderRecent, iterate
    ``recent.slice().reverse()`` instead of ``recent``. Result was **1 failed,
    5 passed** — this test alone, on the order assertion, reporting
    ``['Budget Review', 'Alpha Retro', 'Zoning Notes']`` against the expected
    ``['Zoning Notes', 'Alpha Retro', 'Budget Review']``.
    """
    page.wait_for_selector("#discovery .disc-row", timeout=4000)
    assert _rail_titles(page) == RECENT_ORDER, (
        f"the rail rendered {_rail_titles(page)}, expected {RECENT_ORDER} — the "
        "order the route returned. A client that re-sorts is showing something "
        "other than 'most recent'."
    )


def test_the_idle_rail_is_hidden_once_a_search_runs(page: Any) -> None:
    """One occupant per column: the rail yields to the result list.

    Two lists of documents stacked in one column is an ambiguous page, not a
    richer one — and the lower list is the one the user just asked for.

    MUTATION, MEASURED: drop the ``if (state.searchStatus !== "idle")`` early
    return in render(). Result was **1 failed, 5 passed** — this test alone.
    """
    page.wait_for_selector("#discovery .disc-row", timeout=4000)
    page.click("#q")
    page.type("#q", "budget", delay=20)
    page.wait_for_function(
        '() => document.getElementById("discovery").hidden === true', timeout=4000
    )
    assert page.eval_on_selector("#discovery", "e => e.hidden") is True


# ------------------------------------------------ claim 2: the tag index --


def test_clicking_a_tag_shows_only_the_documents_carrying_it(page: Any) -> None:
    """The second acceptance criterion, with a reflective stub as the oracle.

    ``vendors`` and ``solo`` return DIFFERENT document sets, so opening the
    wrong tag renders the wrong titles rather than the same ones.

    MUTATION, MEASURED — and it reddens TWO tests, not one. In openTag, request
    a fixed ``/api/tags/solo`` instead of the clicked tag. Result was **2
    failed, 4 passed**: this test on the titles, AND
    ``test_a_tag_whose_count_exceeds_its_rows_reconciles_the_two_numbers``,
    because the view then holds ``solo`` (indexed 1, shown 1) so its
    reconciliation line correctly disappears.

    That is recorded rather than tidied away. A reader replaying this mutation
    and seeing two failures would otherwise conclude the entry is broken. The
    reconciliation test has its own mutation that isolates it — see there — so
    the overlap costs no coverage; it only means this one mutation cannot serve
    as that test's independent proof.
    """
    page.wait_for_selector("#discovery .disc-tag", timeout=4000)
    _click_tag(page, "vendors")

    expected = [doc["title"] for doc in TAG_DOCUMENTS["vendors"]]
    assert _rail_titles(page) == expected, (
        f"clicking #vendors showed {_rail_titles(page)}, expected {expected}"
    )
    assert "Alpha Retro" not in _rail_titles(page), (
        "a document that does not carry #vendors is in the tag view"
    )


def test_the_tag_index_states_that_its_counts_are_browseable(page: Any) -> None:
    """Task #31, part one — now satisfied by DERIVATION, not by apology.

    #31 offered two ways to stop a count lying about its own click-through:
    derive it from a browse-consistent query, or label it. The label was taken
    first because the query did not exist. ``browseable_tag_counts`` now does,
    and ``/api/tags`` serves it, so the counts here ARE the rows a click
    produces and the old sentence — "counts cover the whole corpus, including
    drafts and generated pages that browsing hides" — became FALSE.

    THE ASSERTION IS TWO-SIDED ON PURPOSE. It checks the true statement is
    present AND that the false one is gone; asserting only the new wording would
    pass if both sentences were rendered, which is how a corrected string ships
    beside the claim it corrects.

    MUTATION, MEASURED: delete the ``disc-note`` line in renderTagIndex.
    Result was **1 failed, 5 passed** — this test alone.
    """
    page.wait_for_selector("#discovery .disc-tag", timeout=4000)
    assert _tag_labels(page) == [tag["value"] for tag in TAGS]
    notes = " ".join(_notes(page))
    assert "browse to" in notes, (
        f"the tag index does not state the scope of its counts; notes were "
        f"{notes!r}"
    )
    assert "whole corpus" not in notes, (
        f"the index still claims its counts are corpus-wide, which stopped "
        f"being true when /api/tags moved to browseable_tag_counts: {notes!r}"
    )


def test_a_tag_whose_count_exceeds_its_rows_reconciles_the_two_numbers(
    page: Any,
) -> None:
    """Task #31, part two — an INDEPENDENT claim from the label above.

    The index-wide label says counts are corpus-wide. This says the tag VIEW
    states both numbers when they actually differ, at the moment the reader can
    see the shorter list. ``vendors`` is indexed 5 and returns 2.

    The line deliberately does not ATTRIBUTE the gap. The payload carries
    neither a draft flag nor the server's cap, so "3 drafts hidden" would be a
    guess rendered as a fact — the same failure as the unlabelled count it
    replaces. It names both possible causes, which is true in every case.

    TWO MUTATIONS, BOTH MEASURED, because this test now carries two claims.
    (a) make tagScopeNote ``return null`` unconditionally -> **1 failed, 5
    passed**, on the "5 tagged / 2 shown" assertion. (b) restore an attribution
    to the string (``— browsing hides drafts.``) -> **1 failed, 5 passed**, on
    the anti-attribution loop. Neither implies the other: (a) removes the line,
    (b) makes it claim a cause it cannot know.
    """
    page.wait_for_selector("#discovery .disc-tag", timeout=4000)
    _click_tag(page, "vendors")
    notes = " ".join(_notes(page))
    assert "5 tagged" in notes and "2 shown" in notes, (
        f"the tag view does not reconcile 5 indexed against 2 shown; notes were "
        f"{notes!r}"
    )
    # THE STRING MUST NOT NAME A CAUSE. It used to say "browsing hides drafts
    # and generated pages", which was false about a confidential document the
    # moment the sensitivity predicate landed. Asserting the absence of an
    # attribution is what stops a well-meaning explanation being added back.
    for cause in ("draft", "generated", "capped", "confidential"):
        assert cause not in notes.lower(), (
            f"the reconciliation line attributes the gap to {cause!r}: {notes!r}. "
            "The payload cannot tell the causes apart, so naming one is a guess "
            "rendered as a fact."
        )


def test_a_tag_whose_count_matches_its_rows_says_nothing(page: Any) -> None:
    """The other half of the same branch, and the reason it is a separate test.

    A reconciliation line that appeared on EVERY tag would satisfy the test
    above while telling the reader nothing — it would be noise attached to
    agreement. ``solo`` is indexed 1 and returns 1, so the line must be absent.

    MUTATION, MEASURED: weaken tagScopeNote's guard to ``if (indexed == null)
    return null;`` so the line renders whenever the index has loaded. Result
    was **1 failed, 5 passed** — this test alone; the ``vendors`` test above
    still passed, which is what makes the two branches independent claims.

    SECOND MUTATION, MEASURED — and it REFUTED the reason I added the presence
    clause. Making openTag store ``documents: []`` reddens this test **with or
    without** that clause, because dropping ``shown`` to 0 triggers the
    reconciliation line instead of suppressing it. See the comment at the
    assertion: it is kept for the claim it makes, not for a hole it closes.
    """
    page.wait_for_selector("#discovery .disc-tag", timeout=4000)
    _click_tag(page, "solo")

    # PRESENCE FIRST — but NOT for the reason I first wrote here, and the
    # correction is worth more than the assertion.
    #
    # I added this believing the absence assertion below was vacuous under a
    # "renders the view, swallows the rows" change. MEASURED, IT IS NOT: with
    # `documents: []` the absence assertion FAILS on its own (1 failed, run with
    # this clause removed), because swallowing the rows drops `shown` to 0 while
    # `indexed` stays 1 — which TRIGGERS the reconciliation line rather than
    # suppressing it. The note's own condition protects it here.
    #
    # So this clause is NOT the unique catcher for that mutation, and no
    # single-point mutation was found for which it is. It is kept because it
    # asserts a DIFFERENT claim the test otherwise makes only implicitly — that
    # #solo's view shows exactly #solo's one document — not because it closes a
    # hole. Recorded this way so nobody later reads it as proven-necessary and
    # copies the pattern for a reason that does not hold.
    assert _rail_titles(page) == ["Alpha Retro"], (
        f"the #solo tag view did not render its one document; got "
        f"{_rail_titles(page)}"
    )

    notes = " ".join(_notes(page))
    assert "tagged" not in notes, (
        f"#solo's count agrees with its click-through, so there is nothing to "
        f"reconcile, but the view said {notes!r}"
    )
