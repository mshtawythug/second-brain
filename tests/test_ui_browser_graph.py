"""The sidebar local graph (``js/graph.js``), EXECUTED in a real browser.

Same construction as ``tests/test_ui_browser.py`` and
``tests/test_ui_browser_reading.py``: the real ``index.html``, the real
``css/`` and ``js/`` off disk, every API call stubbed at the network layer. No
Postgres, no Ollama, no contention for the test-database lock. The fixture is
shared with ``tests/test_ui_browser_graph_layout.py`` and
``tests/test_ui_browser_graph_refit.py`` through ``tests/ui_graph_harness.py``,
so the three graph suites hold one copy of the stub routing and the fixtures
between them. The label-geometry tests on the server's ring live in that layout
module, and the refit when a box resizes in the refit module; this one covers
painting, interaction, accessibility, degraded states and hardening.

**The filename is load-bearing.** CI selects ``tests/test_ui_browser*.py`` by
path (a bare ``-m browser`` collects modules that open a database connection at
import time), and ``tests/test_ci_workflow.py`` fails if a ``browser``-marked
module sits outside that glob. Run it by path:

    pytest tests/test_ui_browser_graph.py -m browser --no-cov

**UNLIKE THE MARGINALIA TESTS, THIS FILE DOES NOT MOUNT THE MODULE ITSELF.**
``test_ui_browser_reading.py`` calls ``wireMarginalia()`` from
``page.evaluate``, which is why it cannot see a mis-wired ``boot()``. Here the
graph is reached ONLY through ``js/main.js``'s ``boot()``, so the last-child
test runs against the real wiring. Since ``placeInspectorBlock`` (``js/dom.js``)
took over the block order it no longer depends on REGISTRATION order; what
the last-child test now pins is that order with the related rail absent, and
``tests/test_ui_browser_inspector_blocks.py`` pins it for every arrival order.

The ``/graph`` payload follows the contract the server route implements: root
is ``nodes[0]``, every node is pre-placed, titles are plain strings. Every id
and title here is synthetic.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from tests.ui_graph_harness import (
    _ERRORS,
    _GRAPH,
    _NOTE_EXTRA,
    _REQUESTS,
    ALPHA_ID,
    BRAVO_ID,
    CHARLIE_ID,
    EDGES,
    NEIGHBOURS,
    ROOT_ID,
    _dispatch,
    _open,
    _page_fixture,  # noqa: F401 — registers the `page` fixture
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`, which `page` uses
    graph_payload,
    open_without_waiting_on_blocks,
    set_lenses,
)

pytestmark = pytest.mark.browser


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
    """The CHARACTER cap, alone: 28 characters and an ellipsis.

    Bravo is drawn as the ONLY neighbour, at 12 o'clock with nothing on its
    row, so the width fit (graph.js ``fitLabels``) has a slot far wider than
    any 29-character label and the character cap is what cuts it. On the
    default three-neighbour ring Bravo shares a row with Charlie 174 units
    away, and the fit cuts it further to stay clear of Charlie's label — by
    how much depends on the font, which is the layout suite's subject
    (``tests/test_ui_browser_graph_layout.py``), not this test's.
    """
    _GRAPH["payload"] = graph_payload(
        neighbours=[NEIGHBOURS[1]],
        edges=[{"src": BRAVO_ID, "dst": ROOT_ID, "kind": "derived"}],
    )
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
    """(7) The block order through boot(), related rail absent — see header."""
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
        f"#inspector children are {children}; the graph must be LAST when no "
        "related rail is drawn (the harness serves /related as 404 by default). "
        "The order is placeInspectorBlock's (js/dom.js); every arrival order "
        "is pinned by tests/test_ui_browser_inspector_blocks.py."
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

    Withheld means the BODY is not served. That is not the gate
    ``routes_graph`` refuses on — it refuses on the TITLES lens — so for a
    withheld note the client is deliberately stricter than the route: nothing
    about a withheld note is drawn, including who it links with, and the
    inspector's own withheld notice already explains the absence. The case the
    route DOES refuse, a confidential note whose body is served, is (11).
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


_GRAPH_NOTICE = "figure.local-graph p.graph-withheld"


def test_a_confidential_note_under_the_default_lenses_never_requests_its_graph(
    page: Any,
) -> None:
    """(11) The server's DEFAULT lenses — bodies served, titles not.

    The note opens in full (it is NOT withheld) and its backlinks render, but
    ``routes_graph`` refuses its graph with 403 ``graph_withheld``, because
    titles are not served. Before this was mirrored the client asked anyway and
    drew a blank block plus a console error. Now it does not ask, and says why
    in the graph's place.
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
    assert not any(r.endswith("/graph") for r in _REQUESTS), (
        f"/graph was requested for a confidential note this server will not graph: "
        f"{_REQUESTS}"
    )
    assert page.locator(_GRAPH_NOTICE).count() == 1, "no notice in the graph's place"
    assert page.locator(_GRAPH_NOTICE).text_content() == (
        "The graph is hidden for confidential notes on this server."
    )
    assert page.locator(".local-graph svg").count() == 0
    assert page.locator(".marginalia .backlinks-rail").text_content().count(
        "Synthetic Linker"
    ) == 1, "the backlinks rail did not render beside the notice"
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"


@pytest.mark.parametrize(
    ("titles", "bodies", "requested"),
    [(False, True, False), (True, True, True), (True, False, True)],
    ids=["titles-off", "titles-and-bodies-on", "titles-on-bodies-off"],
)
def test_the_graph_gate_mirrors_the_route_on_the_titles_lens_alone(
    page: Any, titles: bool, bodies: bool, requested: bool,
) -> None:
    """(12) ``graphRefusedHere`` is ``routes_graph``'s gate and nothing else:
    a confidential note is graphed exactly when titles are served, whatever the
    bodies lens says. (``titles-on-bodies-off`` would be withheld by the note
    route on a real server; it is here to pin that the predicate ignores the
    bodies lens, not as a reachable state.)"""
    _NOTE_EXTRA["sensitivity"] = "confidential"
    set_lenses(page, titles=titles, bodies=bodies)
    open_without_waiting_on_blocks(page)
    if requested:
        page.wait_for_selector(".local-graph svg")
    assert any(r.endswith("/graph") for r in _REQUESTS) is requested, _REQUESTS
    assert (page.locator(_GRAPH_NOTICE).count() == 1) is not requested
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"
