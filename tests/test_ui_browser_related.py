"""The related-notes rail (``js/related.js``), EXECUTED in a real browser.

Same construction as ``tests/test_ui_browser_graph.py``, and it shares that
module's harness (``tests/ui_graph_harness.py``): the real ``index.html``, the
real ``css/`` and ``js/`` off disk, every API call stubbed at the network
layer. No Postgres, no Ollama, no contention for the test-database lock.

**The rail is reached ONLY through ``js/main.js``'s ``boot()``** — nothing here
calls ``wireRelated()`` from ``page.evaluate`` — so a wiring regression in
``boot()`` turns this file red rather than being masked by the test mounting
the module itself (the trap ``test_ui_browser_reading.py`` documents).

**The filename is load-bearing.** CI runs the browser modules by explicit
path, and ``tests/test_ci_workflow.py`` fails if a ``browser``-marked module is
missing from that list. Run it by path:

    pytest tests/test_ui_browser_related.py -m browser --no-cov

The ``/related`` payload follows the contract the server route implements:
``{id, related: [{id, title, vault_path, source, score, snippet,
snippet_truncated}], count, vector_sim_floor}``. Every id, title and snippet
here is synthetic.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from tests.ui_graph_harness import (
    _ERRORS,
    _HEALTH,
    _HELD,
    _HOLD,
    _NOTE_EXTRA,
    _RELATED,
    _REQUESTS,
    ALPHA_ID,
    BRAVO_ID,
    CHARLIE_ID,
    RELATED,
    ROOT_ID,
    _dispatch,
    _page_fixture,  # noqa: F401 — registers the `page` fixture
    _serve_related_fixture,  # noqa: F401 — registers `serve_related`
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`, which `page` uses
    open_without_waiting_on_blocks,
    reboot,
    related_payload,
    set_lenses,
    settle,
    wait_for_held,
)

pytestmark = [pytest.mark.browser, pytest.mark.usefixtures("serve_related")]

_RAIL = "#inspector > nav.related-rail"


def _open(page: Any, note_id: str = ROOT_ID) -> None:
    """Open a note through the REAL inspector and wait until its ``/related``
    response has been delivered AND handled (see the harness's ``_open`` for
    why two frames plus a bare dispatch settle that)."""
    with page.expect_response(re.compile(r"/api/notes/[^/]+/related$")):
        page.evaluate(
            """async (id) => {
                const inspector = await import("/static/js/inspector.js");
                await inspector.openNote(id);
            }""",
            note_id,
        )
    page.wait_for_selector(".note-body")
    settle(page)
    _dispatch(page, "{}")


def _rail_ids(page: Any) -> list[str]:
    return page.eval_on_selector_all(
        f"{_RAIL} a[data-note-id]", "els => els.map(e => e.getAttribute('data-note-id'))"
    )


# ---------------------------------------------------------------- painting --


def test_opening_a_note_paints_every_related_row_in_order(page: Any) -> None:
    """Guard the guard: every test below is vacuous if nothing painted."""
    _open(page)
    page.wait_for_selector(_RAIL)

    rail = page.locator(_RAIL)
    assert rail.count() == 1
    assert rail.get_attribute("aria-label") == "Related notes"
    assert page.locator(f"{_RAIL} > h2.rail-heading").text_content() == "Related"
    assert page.locator(f"{_RAIL} > ul > li").count() == len(RELATED)
    assert _rail_ids(page) == [row[0] for row in RELATED], (
        "related rows are missing or out of the order the server sent"
    )
    hrefs = page.eval_on_selector_all(
        f"{_RAIL} a[data-note-id]", "els => els.map(e => e.getAttribute('href'))"
    )
    assert hrefs == [f"?id={row[0]}" for row in RELATED]
    titles = page.eval_on_selector_all(
        f"{_RAIL} a[data-note-id]", "els => els.map(e => e.textContent)"
    )
    assert titles == [row[1] for row in RELATED]


def test_snippets_are_shown_and_a_truncated_one_is_marked(page: Any) -> None:
    _open(page)
    page.wait_for_selector(_RAIL)
    snippets = page.eval_on_selector_all(
        f"{_RAIL} p.related-snippet", "els => els.map(e => e.textContent)"
    )
    (_, _, whole, whole_cut), (_, _, cut, cut_cut) = RELATED
    assert (whole_cut, cut_cut) == (False, True), "precondition: fixture shape changed"
    assert snippets == [whole, f"{cut}…"], (
        "a whole snippet must be shown as sent and a truncated one with a trailing …"
    )


def test_the_score_is_not_displayed(page: Any) -> None:
    _open(page)
    page.wait_for_selector(_RAIL)
    assert "0.5" not in page.locator(_RAIL).text_content()


def test_an_empty_title_is_called_untitled(page: Any) -> None:
    _RELATED["payload"] = related_payload([(ALPHA_ID, "   ", "Synthetic snippet.", False)])
    _open(page)
    page.wait_for_selector(_RAIL)
    assert page.locator(f"{_RAIL} a[data-note-id]").text_content() == "Untitled"


def test_a_row_without_a_snippet_has_no_snippet_element(page: Any) -> None:
    _RELATED["payload"] = related_payload([(ALPHA_ID, "Alpha Synthetic Note", "", False)])
    _open(page)
    page.wait_for_selector(_RAIL)
    assert page.locator(f"{_RAIL} p.related-snippet").count() == 0


# ------------------------------------------------------------- interaction --


def test_clicking_a_row_opens_that_note_without_reloading(page: Any) -> None:
    """A marker planted on ``window`` survives an in-place open and is wiped by
    a reload — the same oracle the graph suite uses, for the same reason."""
    _open(page)
    page.wait_for_selector(_RAIL)
    page.evaluate("() => { window.__noReload = 1; }")

    with page.expect_request(re.compile(rf"/api/notes/{CHARLIE_ID}$")):
        page.click(f'{_RAIL} a[data-note-id="{CHARLIE_ID}"]')
    page.wait_for_function(
        "(t) => document.querySelector('.note-title')?.textContent === t",
        arg="Charlie Synthetic Note",
    )
    assert page.evaluate("() => window.__noReload") == 1, "the click reloaded the page"
    assert f"id={CHARLIE_ID}" in page.url
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"


def test_editing_hides_the_rail_and_preview_restores_it_from_cache(page: Any) -> None:
    _open(page)
    page.wait_for_selector(_RAIL)
    _dispatch(page, '{"editing": true}')
    page.wait_for_selector(".editor")
    assert page.locator(".related-rail").count() == 0, "the rail is drawn beside the editor"

    _dispatch(page, '{"editing": false}')
    page.wait_for_selector(".note-body")
    assert page.locator(_RAIL).count() == 1
    assert sum(r.endswith("/related") for r in _REQUESTS) == 1, (
        f"the rail refetched on a dispatch: {_REQUESTS}"
    )


def test_a_slow_response_for_one_note_does_not_paint_under_another(page: Any) -> None:
    """The stale-response guard: A's rail must never appear under B."""
    _HOLD.add("related")
    page.evaluate(
        """async (id) => {
            const inspector = await import("/static/js/inspector.js");
            await inspector.openNote(id);
        }""",
        ROOT_ID,
    )
    wait_for_held(page, 1)
    held_for_root = _HELD.pop()

    page.evaluate(
        """async (id) => {
            const inspector = await import("/static/js/inspector.js");
            await inspector.openNote(id);
        }""",
        BRAVO_ID,
    )
    wait_for_held(page, 1)
    with page.expect_response(re.compile(r"/api/notes/[^/]+/related$")):
        held_for_root[1]()
    settle(page)
    page.wait_for_function(
        "(t) => document.querySelector('.note-title')?.textContent === t",
        arg="Bravo Synthetic Note With A Deliberately Long Title",
    )
    assert page.locator(".related-rail").count() == 0, (
        "the root note's related rows were painted under a different note"
    )


# ---------------------------------------------------------- degraded states --


def test_an_empty_list_draws_no_rail_at_all(page: Any) -> None:
    """``related: []`` is a normal answer, and it draws NOTHING — not a heading
    over an empty list."""
    _RELATED["payload"] = related_payload([])
    _open(page)
    assert any(r.endswith("/related") for r in _REQUESTS), (
        "precondition: /related was never requested, so the empty path never ran"
    )
    assert page.locator(".related-rail").count() == 0
    assert page.locator("#inspector .rail-heading", has_text="Related").count() == 0


@pytest.mark.parametrize("status", [500, 403], ids=["server-error", "related-withheld"])
def test_a_failed_request_leaves_the_note_readable_and_silent(
    page: Any, status: int,
) -> None:
    """403 ``related_withheld`` and any other failure: note intact, no rail, no
    toast, no uncaught error."""
    _RELATED["status"] = status
    _open(page)
    assert page.locator(".note-body").count() == 1
    assert page.locator(".related-rail").count() == 0
    assert page.locator("#toast").is_hidden(), "a toast fired for a supplementary fetch"
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"
    assert any(r.endswith("/related") for r in _REQUESTS), (
        "precondition: /related was never requested, so the failure path never ran"
    )


def test_a_failed_request_is_not_retried_on_every_dispatch(page: Any) -> None:
    _RELATED["status"] = 500
    _open(page)
    for _ in range(3):
        _dispatch(page, "{}")
    settle(page)
    assert sum(r.endswith("/related") for r in _REQUESTS) == 1, (
        f"a failed /related was retried on a dispatch: {_REQUESTS}"
    )


def test_a_withheld_note_never_requests_its_related_notes(page: Any) -> None:
    """A withheld note (body not served) is never asked about, and draws nothing.

    This is ONE of the cases ``routes_related`` refuses, not all of them: it
    also refuses a confidential note whose body IS served while titles are not
    — the server's default — and that case is pinned separately, below.
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
    settle(page)
    _dispatch(page, "{}")
    # A BOUNDED ABSENCE CHECK, not a sleep-to-pass: the assertion below is that
    # a request did NOT happen, and absence has no event to wait for. This only
    # gives a wrongly-issued request time to show up; with a correct client the
    # test passes whether or not the wait elapses.
    page.wait_for_timeout(150)
    assert f"/api/notes/{ROOT_ID}" in _REQUESTS, (
        "precondition: the note itself was never requested"
    )
    assert not any(r.endswith("/related") for r in _REQUESTS), (
        f"/related was requested for a withheld note: {_REQUESTS}"
    )
    assert page.locator(".related-rail").count() == 0
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"


# --------------------------------------------------------------- hardening --


def test_a_markup_title_and_snippet_render_as_literal_text(page: Any) -> None:
    """The XSS guard: a title and a snippet are text, never parsed."""
    _RELATED["payload"] = related_payload([(ALPHA_ID, "<b>x</b>", "<i>y</i>", False)])
    _open(page)
    page.wait_for_selector(_RAIL)
    assert page.locator(f"{_RAIL} a[data-note-id]").text_content() == "<b>x</b>"
    assert page.locator(f"{_RAIL} p.related-snippet").text_content() == "<i>y</i>"
    assert page.locator(f"{_RAIL} b, {_RAIL} i").count() == 0, (
        "a title or snippet was parsed as markup"
    )


def test_an_id_is_encoded_into_the_href(page: Any) -> None:
    _RELATED["payload"] = related_payload([("a b&c", "Synthetic Odd Id", "s", False)])
    _open(page)
    page.wait_for_selector(_RAIL)
    assert page.locator(f"{_RAIL} a[data-note-id]").get_attribute("href") == "?id=a%20b%26c"


def test_the_rail_is_readable_in_both_themes(page: Any) -> None:
    """Title and snippet ink against the inspector's ground meet WCAG AA."""
    # The link's colour TRANSITIONS (--dur-fast), so a read straight after the
    # theme flip measures the old theme's ink on the new ground. Reduced motion
    # turns that transition off (related.css), and the getAnimations() wait is
    # the deterministic backstop for any transition elsewhere — no fixed sleep.
    page.emulate_media(reduced_motion="reduce")
    _open(page)
    page.wait_for_selector(_RAIL)
    for theme in ("light", "dark"):
        page.evaluate(
            "(t) => document.documentElement.setAttribute('data-theme', t)", theme
        )
        page.wait_for_function("() => document.getAnimations().length === 0")
        ratios = page.evaluate(_CONTRAST_JS, [f"{_RAIL} a", f"{_RAIL} p.related-snippet",
                                              f"{_RAIL} .rail-heading"])
        for selector, ratio in ratios.items():
            assert ratio >= 4.5, f"{selector} is {ratio:.2f}:1 in {theme}, below AA"


#: Resolve each selector's computed colour against #inspector's background and
#: return the WCAG contrast ratio. Colours are read through a 1x1 canvas so any
#: CSS colour syntax (oklch included) comes back as sRGB bytes.
_CONTRAST_JS = """(selectors) => {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", {willReadFrequently: true});
    const rgb = (css) => {
        ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = "#000"; ctx.fillStyle = css;
        ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3);
    };
    const lum = ([r, g, b]) => {
        const f = (c) => {
            c /= 255;
            return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
        };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const inspector = document.getElementById("inspector");
    const ground = lum(rgb(getComputedStyle(inspector).backgroundColor));
    const out = {};
    for (const sel of selectors) {
        const ink = lum(rgb(getComputedStyle(document.querySelector(sel)).color));
        const [hi, lo] = ink > ground ? [ink, ground] : [ground, ink];
        out[sel] = (hi + 0.05) / (lo + 0.05);
    }
    return out;
}"""


_RELATED_NOTICE = "#inspector > section.related-rail p.related-withheld"


def test_a_confidential_note_under_the_default_lenses_never_requests_related(
    page: Any,
) -> None:
    """The server's DEFAULT lenses — bodies served, titles not.

    The note opens in full (it is NOT withheld) and its backlinks render, but
    ``routes_related`` refuses it with 403 ``related_withheld``, because it
    serves related notes of a confidential root only when titles AND bodies are
    served. Before this was mirrored the client asked anyway and drew a blank
    rail plus a console error. Now it does not ask, and says why in the rail's
    place.
    """
    _NOTE_EXTRA["sensitivity"] = "confidential"
    open_without_waiting_on_blocks(page)
    page.wait_for_selector("#inspector > .marginalia .backlinks-rail a")

    assert f"/api/notes/{ROOT_ID}" in _REQUESTS, (
        "precondition: the note itself was never requested"
    )
    assert any(r.endswith("/links") for r in _REQUESTS), (
        "precondition: the backlinks were never requested"
    )
    assert not any(r.endswith("/related") for r in _REQUESTS), (
        f"/related was requested for a confidential note this server will not rank: "
        f"{_REQUESTS}"
    )
    assert page.locator(_RELATED_NOTICE).count() == 1, "no notice in the rail's place"
    assert page.locator(_RELATED_NOTICE).text_content() == (
        "Related notes are hidden for confidential notes on this server."
    )
    assert page.locator(_RAIL).count() == 0
    assert page.locator(".marginalia .backlinks-rail").text_content().count(
        "Synthetic Linker"
    ) == 1, "the backlinks rail did not render beside the notice"
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"


@pytest.mark.parametrize(
    ("titles", "bodies", "requested"),
    [(False, True, False), (True, False, False), (True, True, True)],
    ids=["titles-off", "bodies-off", "titles-and-bodies-on"],
)
def test_the_related_gate_mirrors_the_route_on_both_lenses(
    page: Any, titles: bool, bodies: bool, requested: bool,
) -> None:
    """``relatedRefusedHere`` is ``routes_related``'s gate and nothing else: a
    confidential note is ranked exactly when titles AND bodies are served.
    (``bodies-off`` would be withheld by the note route on a real server; it is
    here to pin that the predicate needs both lenses, not as a reachable state.)"""
    _NOTE_EXTRA["sensitivity"] = "confidential"
    set_lenses(page, titles=titles, bodies=bodies)
    open_without_waiting_on_blocks(page)
    if requested:
        page.wait_for_selector(_RAIL)
    assert any(r.endswith("/related") for r in _REQUESTS) is requested, _REQUESTS
    assert (page.locator(_RELATED_NOTICE).count() == 1) is not requested
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"


_GRAPH_NOTICE = "#inspector > figure.local-graph p.graph-withheld"

#: The two degraded health answers ``servesConfidential`` (store.js) must FAIL
#: CLOSED on: no health at all (boot() catches the failure and still opens
#: notes, so this is reachable), and a payload that omits the titles key while
#: the bodies key is present and true.
_DEGRADED_HEALTH = {
    "health-500": {"status": 500},
    "titles-key-missing": {"payload": {
        "status": "ok", "read_only": False, "notices": [],
        "serve_confidential_bodies": True,
    }},
}


def _assert_health_is_degraded(page: Any, degraded: str) -> None:
    """Precondition: the rebooted client really holds the degraded answer —
    no health at all, or health without the titles key — or the test below
    would be measuring the default lenses."""
    health = page.evaluate(
        "async () => (await import('/static/js/store.js')).state.health"
    )
    if degraded == "health-500":
        assert health is None, f"precondition: health was not a failure: {health}"
    else:
        assert health is not None and "serve_confidential_titles" not in health, (
            f"precondition: health still carries the titles key: {health}"
        )


@pytest.mark.parametrize("degraded", list(_DEGRADED_HEALTH), ids=list(_DEGRADED_HEALTH))
def test_a_degraded_health_answer_fails_closed_for_a_confidential_note(
    page: Any, degraded: str,
) -> None:
    """``servesConfidential`` reads a missing or failed health as NOT served."""
    _HEALTH.update(_DEGRADED_HEALTH[degraded])
    reboot(page)
    _assert_health_is_degraded(page, degraded)
    _NOTE_EXTRA["sensitivity"] = "confidential"
    open_without_waiting_on_blocks(page)  # a bounded absence window — see its docstring
    page.wait_for_selector("#inspector > .marginalia .backlinks-rail a")

    assert f"/api/notes/{ROOT_ID}" in _REQUESTS, (
        "precondition: the note itself was never requested"
    )
    assert not any(r.endswith("/graph") for r in _REQUESTS), (
        f"/graph was requested under a degraded health answer: {_REQUESTS}"
    )
    assert not any(r.endswith("/related") for r in _REQUESTS), (
        f"/related was requested under a degraded health answer: {_REQUESTS}"
    )
    assert page.locator(_GRAPH_NOTICE).count() == 1, "no graph notice"
    assert page.locator(_RELATED_NOTICE).count() == 1, "no related notice"
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"


@pytest.mark.parametrize("degraded", list(_DEGRADED_HEALTH), ids=list(_DEGRADED_HEALTH))
def test_a_degraded_health_answer_still_draws_both_blocks_for_a_normal_note(
    page: Any, degraded: str,
) -> None:
    """Fail closed must not mean fail everything: a NON-confidential note in
    the same degraded state fetches and draws its graph and related rail."""
    _HEALTH.update(_DEGRADED_HEALTH[degraded])
    reboot(page)
    _assert_health_is_degraded(page, degraded)
    _open(page)
    page.wait_for_selector("#inspector > figure.local-graph svg")
    page.wait_for_selector(_RAIL)

    assert any(r.endswith("/graph") for r in _REQUESTS), _REQUESTS
    assert any(r.endswith("/related") for r in _REQUESTS), _REQUESTS
    assert page.locator(_GRAPH_NOTICE).count() == 0
    assert page.locator(_RELATED_NOTICE).count() == 0
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"
