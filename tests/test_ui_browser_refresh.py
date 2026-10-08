"""The inspector's three blocks REFRESH after a save, EXECUTED in a real browser.

``js/marginalia.js`` (backlinks), ``js/graph.js`` and ``js/related.js`` each
cache per note, so before this a saved ``[[wikilink]]`` showed up in none of
them until a full reload. ONE mechanism fixes all three: a successful save
bumps the saved note's revision (``bumpNoteRevision`` in ``js/store.js``) and
each module keys its cache on ``noteKey(id)`` — ``id`` plus that revision. So
the saved note's entries stop matching and its blocks refetch; no other note's
key moves, and nothing refetches on an ordinary dispatch.

Same construction and harness as the graph and related suites
(``tests/ui_graph_harness.py``); everything is reached through ``boot()``. The
save is stubbed at the network layer (``PUT /api/notes/{id}``, the request
``saveNote`` in ``js/inspector.js`` sends). Every id, title and snippet is
synthetic. Run it by path:

    pytest tests/test_ui_browser_refresh.py -m browser --no-cov
"""
from __future__ import annotations

import contextlib
from typing import Any

import pytest

from tests.ui_graph_harness import (
    _ERRORS,
    _GRAPH,
    _HOLD,
    _LINKS,
    _PUTS,
    _RELATED,
    _REQUESTS,
    ALPHA_ID,
    BRAVO_ID,
    CHARLIE_ID,
    KINDS,
    LINKS_PAYLOAD,
    ROOT_ID,
    _dispatch,
    _page_fixture,  # noqa: F401 — registers the `page` fixture
    _serve_related_fixture,  # noqa: F401 — registers `serve_related`
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`, which `page` uses
    assert_canonical_blocks,
    graph_payload,
    inspector_children,
    related_payload,
    release_held,
    settle,
    start_open,
    wait_for_held,
)

pytestmark = [pytest.mark.browser, pytest.mark.usefixtures("serve_related")]


#: The neighbourhood BEFORE the save (N1) and AFTER it (N2). Each block shows
#: something different in each, so "shows N2" cannot be satisfied by N1.
N2_LINKER = "Synthetic Linker After Save"
N2_LINKS: dict[str, Any] = {
    **LINKS_PAYLOAD,
    "backlinks": [{**LINKS_PAYLOAD["backlinks"][0], "id": CHARLIE_ID, "title": N2_LINKER}],
}
N2_GRAPH = graph_payload(
    neighbours=[(BRAVO_ID, "Bravo Synthetic Note", "vault")],
    edges=[{"src": ROOT_ID, "dst": BRAVO_ID, "kind": "wiki"}],
)
N2_RELATED = related_payload([(BRAVO_ID, "Bravo Synthetic Note", "Saved snippet.", False)])


_EDIT = '.note-bar button:has-text("Edit")'


def _open_settled(page: Any, note_id: str) -> None:
    """Open a note and wait until all three blocks are drawn from their fetches."""
    start_open(page, note_id)
    page.wait_for_selector("#inspector > nav.related-rail")
    page.wait_for_selector("#inspector > figure.local-graph svg")
    page.wait_for_selector("#inspector > .marginalia .backlinks-rail a")
    settle(page)


def _fetches(note_id: str) -> dict[str, int]:
    return {k: _REQUESTS.count(f"/api/notes/{note_id}/{k}") for k in KINDS}


def _shown(page: Any) -> dict[str, list[str]]:
    return page.evaluate(
        """() => ({
            links: [...document.querySelectorAll('.backlinks-rail a')].map(a => a.textContent),
            graph: [...document.querySelectorAll('.local-graph a.node')]
                .map(a => a.getAttribute('data-note-id')),
            related: [...document.querySelectorAll('.related-rail a[data-note-id]')]
                .map(a => a.getAttribute('data-note-id')),
        })"""
    )


#: True once ALL THREE blocks show the post-save neighbourhood. One condition
#: over the three, because the three refetches are independent: waiting on one
#: block and then settling two frames would race the other two.
_SHOWS_N2_JS = """([linker, id]) => {
    const texts = (sel) => [...document.querySelectorAll(sel)].map((e) => e.textContent);
    const ids = (sel) => [...document.querySelectorAll(sel)]
        .map((e) => e.getAttribute("data-note-id"));
    const one = (list, value) => list.length === 1 && list[0] === value;
    return one(texts(".backlinks-rail a"), linker)
        && one(ids(".local-graph a.node"), id)
        && one(ids(".related-rail a[data-note-id]"), id);
}"""


def _wait_for_n2(page: Any) -> None:
    """Give the three post-save refetches a bounded chance to land. A timeout is
    NOT a failure here: the assertion that follows is the oracle, so a missing
    refresh goes red there, with the evidence, rather than as a bare timeout."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    with contextlib.suppress(PlaywrightTimeout):
        page.wait_for_function(_SHOWS_N2_JS, arg=[N2_LINKER, BRAVO_ID], timeout=3000)


def _save_an_edit(page: Any, text: str) -> None:
    page.click(_EDIT)
    page.wait_for_selector(".editor")
    page.fill(".editor", text)
    page.click(".note-bar .accent-btn")
    page.wait_for_selector(".note-body")


def test_saving_a_note_refreshes_all_three_blocks(page: Any) -> None:
    _open_settled(page, ROOT_ID)
    before = _shown(page)
    assert before["links"] == ["Synthetic Linker"], f"precondition (N1): {before}"
    assert BRAVO_ID not in before["related"], f"precondition (N1): {before}"
    assert _fetches(ROOT_ID) == {k: 1 for k in KINDS}

    _LINKS["payload"] = N2_LINKS
    _GRAPH["payload"] = N2_GRAPH
    _RELATED["payload"] = N2_RELATED
    _save_an_edit(page, "# Synthetic Root Note\n\nNow links [[Bravo Synthetic Note]].\n")
    assert len(_PUTS) == 1, f"precondition: the save never reached the server: {_PUTS}"

    _wait_for_n2(page)
    after = _shown(page)
    assert after == {"links": [N2_LINKER], "graph": [BRAVO_ID], "related": [BRAVO_ID]}, (
        f"the blocks still show the pre-save neighbourhood after a save: {after}; "
        f"fetches for the saved note: {_fetches(ROOT_ID)}"
    )
    assert _fetches(ROOT_ID) == {k: 2 for k in KINDS}, (
        f"each block must refetch exactly once after a save: {_fetches(ROOT_ID)}"
    )
    assert_canonical_blocks(inspector_children(page), "after a save")
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"


def test_an_unsaved_edit_does_not_refetch_and_other_notes_keep_their_cache(
    page: Any,
) -> None:
    """The cache still works: leaving the editor without saving, and switching
    notes, fetch nothing new. A save of X drops only X's entries."""
    _open_settled(page, ROOT_ID)
    page.click(_EDIT)
    page.wait_for_selector(".editor")
    page.fill(".editor", "an edit that is never saved")
    page.click('.note-bar button:has-text("Preview")')
    page.wait_for_selector(".note-body")

    _open_settled(page, ALPHA_ID)
    _open_settled(page, ROOT_ID)
    page.wait_for_timeout(200)
    assert _PUTS == [], "precondition: nothing was meant to be saved"
    assert _fetches(ROOT_ID) == {k: 1 for k in KINDS}, (
        f"returning to a note after an UNSAVED edit refetched: {_fetches(ROOT_ID)}"
    )

    # Now save the root; the other note's cache must survive it.
    _save_an_edit(page, "# Synthetic Root Note\n\nSaved.\n")
    assert len(_PUTS) == 1, f"precondition: the save never reached the server: {_PUTS}"
    start_open(page, ALPHA_ID)
    page.wait_for_function(
        "(t) => document.querySelector('.note-title')?.textContent === t",
        arg="Alpha Synthetic Note",
    )
    page.wait_for_selector("#inspector > nav.related-rail")
    page.wait_for_timeout(200)
    assert _fetches(ALPHA_ID) == {k: 1 for k in KINDS}, (
        f"saving one note dropped ANOTHER note's cache: {_fetches(ALPHA_ID)}"
    )


def test_a_pre_save_response_landing_after_the_save_never_paints(page: Any) -> None:
    """The save-while-in-flight race: a response REQUESTED before the save but
    DELIVERED after it must not overwrite the post-save answer.

    Correct by construction today — each module captures ``noteKey(id)`` when
    it asks, so the late pre-save answer files under the superseded key and
    never matches again. Nothing else in the suite would notice if the key were
    recomputed when the answer lands instead: that files the stale answer under
    the CURRENT key and paints the pre-save neighbourhood. This test does.
    """
    _HOLD.update({"related", "links"})
    start_open(page, ROOT_ID)
    page.wait_for_selector("#inspector > figure.local-graph svg")
    wait_for_held(page, 2)  # the PRE-save /related and /links, parked with N1 bodies

    _HOLD.clear()  # the post-save refetches are delivered as they arrive
    _LINKS["payload"] = N2_LINKS
    _GRAPH["payload"] = N2_GRAPH
    _RELATED["payload"] = N2_RELATED
    _save_an_edit(page, "# Synthetic Root Note\n\nNow links [[Bravo Synthetic Note]].\n")
    assert len(_PUTS) == 1, f"precondition: the save never reached the server: {_PUTS}"
    page.wait_for_function(_SHOWS_N2_JS, arg=[N2_LINKER, BRAVO_ID], timeout=5000)

    # NOW deliver the two answers that were asked for before the save.
    release_held(page, ["related", "links"])
    _dispatch(page, "{}")  # re-render from whatever the caches now hold
    after = _shown(page)
    assert after == {"links": [N2_LINKER], "graph": [BRAVO_ID], "related": [BRAVO_ID]}, (
        f"a pre-save response delivered after the save repainted the old "
        f"neighbourhood: {after}"
    )
    fetched = _fetches(ROOT_ID)
    assert (fetched["related"], fetched["links"]) == (2, 2), (
        f"each held kind must be requested exactly twice (before and after the "
        f"save): {fetched}"
    )
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"
