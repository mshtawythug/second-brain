"""Local-graph LABEL GEOMETRY on the server's real ring, in a real browser.

Split out of ``tests/test_ui_browser_graph.py`` to keep each module under the
800-line ceiling; the stub routing and fixtures are shared through
``tests/ui_graph_harness.py``, not copied. Everything here builds its
``/graph`` stub with the server's own ring formula (``graph_layout``) and
measures label BOXES: inside the inspector, never intersecting, legible on a
phone, and hidden at rest on a ring too crowded to label.

**Every geometric test runs twice: at the platform's default font, and at a
deliberately WIDE face** (``_WIDE_FACE_CSS``). The label geometry was first
measured against macOS metrics only, and it passed here while GitHub's Linux
runner — whose default sans is the wider DejaVu Sans — rendered overlapping
and clipped labels. The ``wide`` runs put that failure on every machine, and
each one asserts at its END that the wide face really did force a cut, so a
face that stops being wide cannot turn them into silent copies of the
``platform`` runs. **One named exemption:** the crowded-ring test's two 1280
``wide`` runs ARE copies of their ``platform`` runs and do not assert the
cut, because at 1280 no 28-character label reaches the only bound a crowded
ring has, the inspector's edge (measured; the reason is in that test).

**The filename is load-bearing.** CI selects ``tests/test_ui_browser*.py`` by
path, and ``tests/test_ci_workflow.py`` fails if a ``browser``-marked module
sits outside that glob. Run it by path:

    pytest tests/test_ui_browser_graph_layout.py -m browser --no-cov
"""
from __future__ import annotations

import math
import re
from typing import Any

import pytest

from brain.ui.app import static_dir
from tests.ui_graph_harness import (
    _GRAPH,
    ALPHA_ID,
    BRAVO_ID,
    CHARLIE_ID,
    ROOT_ID,
    ROOT_TITLE,
    _open,
    _page_fixture,  # noqa: F401 — registers the `page` fixture
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`, which `page` uses
)

pytestmark = pytest.mark.browser


# ------------------------------------------------------------------ layout --


DELTA_ID = "aaaaaaaa-0000-4000-8000-00000000000f"

#: The SERVER's ring, not a hand-placed one. Same formula and constants as
#: ``brain.ui.graph_layout`` (``RING_RADIUS`` = 110.0, ``DEFAULT_SIZE`` = 320,
#: neighbour ``i`` of ``count`` at ``theta = 2*pi*i/count``, starting at 12
#: o'clock and running clockwise: ``x = c + R*sin(theta)``, ``y = c - R*cos(theta)``).
#: An earlier version of this test used a hand-made ring with its outer nodes
#: at x = 73 / 247 and passed, while the server's real ring puts them at
#: x = 50 / 270 — where the labels were in fact clipped. Copied rather than
#: imported: graph_layout is the server route's module (a separate task), and
#: this file stubs the network and imports nothing of the server beyond
#: ``static_dir``. If graph_layout's ring changes, change these with it.
_SERVER_RING_RADIUS = 110.0
_SERVER_SIZE = 320

#: Wider than any default sans this suite will meet, on every platform: a
#: wide real face where one is installed (Verdana ships with macOS; DejaVu
#: Sans is the Linux default), widened again by letter-spacing, which does
#: not depend on any installed font at all. Scoped to the graph's labels, so
#: only the subject of these tests changes.
_WIDE_FACE_CSS = (
    ".local-graph .node text {"
    " font-family: Verdana, 'DejaVu Sans', sans-serif; letter-spacing: 0.12em; }"
)
_FACES = ["platform", "wide"]


def _use_face(page: Any, face: str) -> None:
    """Apply ``face`` BEFORE the graph is drawn: labels are fitted at draw time."""
    if face == "wide":
        page.add_style_tag(content=_WIDE_FACE_CSS)


#: How many drawn labels the width fit cut. Every title in this module is
#: exactly 28 characters — at the character cap, never over it — so any
#: ellipsis on a label is the width fit's work.
_CUT_LABELS_JS = """() => [...document.querySelectorAll('.local-graph text')]
    .filter((t) => t.textContent.endsWith('…')).length"""


def _assert_the_face_bit(page: Any, face: str) -> None:
    """The ``wide`` face must have forced at least one cut, or its run proved
    nothing the ``platform`` run did not. Asserted LAST, after the geometry,
    so a missing fit fails on overlapping or clipped labels — what a reader
    would see — and not here."""
    if face == "wide":
        assert page.evaluate(_CUT_LABELS_JS) > 0, (
            "precondition: the wide face cut no label, so this run tested nothing new"
        )

#: Four neighbours: a count divisible by 4 puts two of them exactly at 3 and 9
#: o'clock, the rim positions. Every title is exactly 28 characters — the
#: longest label the client draws without cutting it.
_RIM_TITLES: list[tuple[str, str]] = [
    (ALPHA_ID, "Alpha Synthetic Planning Doc"),
    (BRAVO_ID, "Bravo Synthetic Planning Doc"),
    (CHARLIE_ID, "Delta Synthetic Planning Doc"),
    (DELTA_ID, "Hotel Synthetic Planning Doc"),
]


def _crowd_titles(count: int) -> list[tuple[str, str]]:
    """``count`` synthetic neighbours, every title exactly 28 characters."""
    return [
        (f"aaaaaaaa-0000-4000-8000-{0x100 + i:012x}", f"Synthetic Planning Note {i:04d}")
        for i in range(count)
    ]


def _server_ring_payload(
    titles: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """A /graph body whose geometry is the server's, for the layout tests."""
    chosen = _RIM_TITLES if titles is None else titles
    centre = _SERVER_SIZE / 2
    count = len(chosen)
    nodes: list[dict[str, Any]] = [{
        "id": ROOT_ID, "title": ROOT_TITLE, "kind": "vault",
        "x": centre, "y": centre, "r": 9, "root": True,
    }]
    for index, (node_id, title) in enumerate(chosen):
        theta = 2 * math.pi * index / count
        nodes.append({
            "id": node_id, "title": title, "kind": "vault",
            "x": centre + _SERVER_RING_RADIUS * math.sin(theta),
            "y": centre - _SERVER_RING_RADIUS * math.cos(theta),
            "r": 6, "root": False,
        })
    return {
        "id": ROOT_ID, "width": _SERVER_SIZE, "height": _SERVER_SIZE, "nodes": nodes,
        "edges": [{"src": ROOT_ID, "dst": node_id, "kind": "wiki"} for node_id, _ in chosen],
        "truncated": 0, "corpus_linked": True,
    }


#: 320 / 400: below 780px the ledger and inspector become a two-view stack
#: (components.css), so on a phone the inspector is the whole viewport with the
#: narrow padding — the narrowest it ever gets. 1280: the normal desktop.
@pytest.mark.parametrize("face", _FACES)
@pytest.mark.parametrize("viewport_width", [320, 400, 1280])
def test_rim_labels_stay_inside_the_inspector(page: Any, viewport_width: int, face: str) -> None:
    """(13) Labels at the server's 3 and 9 o'clock nodes are not cut off.

    TWO assertions, because the first one ALONE CANNOT FAIL on label spill —
    measured: text overflowing an ``overflow: visible`` svg is INK overflow in
    Chromium, not scrollable overflow, so it never widens the inspector's
    scroll box. It is CLIPPED at the inspector's edge instead
    (``overflow-y: auto`` clips both axes). So the property a reader would
    notice — every label wholly inside the visible inspector — is asserted
    directly, and the scroll check stays as the cheap half.
    """
    assert all(len(title) == 28 for _, title in _RIM_TITLES)
    page.set_viewport_size({"width": viewport_width, "height": 800})
    _use_face(page, face)
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    page.wait_for_selector(".local-graph svg")

    xs = sorted(round(n["x"]) for n in _GRAPH["payload"]["nodes"][1:])
    assert xs[0] == 50 and xs[-1] == 270, f"precondition: no node on the rim ({xs})"
    assert page.locator(".local-graph text").count() == 1 + len(_RIM_TITLES), (
        "precondition: not every label was drawn"
    )

    scroll, client = page.evaluate(
        """() => {
            const i = document.getElementById('inspector');
            return [i.scrollWidth, i.clientWidth];
        }"""
    )
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
    assert spill == [], f"labels run past the inspector and are clipped: {spill}"
    _assert_the_face_bit(page, face)


@pytest.mark.parametrize("face", _FACES)
@pytest.mark.parametrize("viewport_width", [320, 400, 1280])
def test_no_two_labels_overlap_on_the_server_ring(
    page: Any, viewport_width: int, face: str,
) -> None:
    """(14) On the server's real ring, no label box intersects another.

    The 3 and 9 o'clock neighbours sit on the root's row, so their labels
    (drawn BELOW their nodes) share a band with anything drawn below the root.
    The 12 o'clock neighbour's label sits below IT, i.e. above the root — the
    other side a root label could move to. Every pair is checked, so moving
    the root's label cannot trade one collision for another unnoticed.
    """
    page.set_viewport_size({"width": viewport_width, "height": 800})
    _use_face(page, face)
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    page.wait_for_selector(".local-graph svg")

    overlaps = page.evaluate(
        """() => {
            const boxes = [...document.querySelectorAll('.local-graph text')]
              .map((t) => [t.textContent, t.getBoundingClientRect()]);
            const hits = [];
            for (let i = 0; i < boxes.length; i++) {
              for (let j = i + 1; j < boxes.length; j++) {
                const [na, a] = boxes[i]; const [nb, b] = boxes[j];
                if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) {
                  hits.push(`${na} [${Math.round(a.left)}..${Math.round(a.right)} x `
                    + `${Math.round(a.top)}..${Math.round(a.bottom)}] meets ${nb} `
                    + `[${Math.round(b.left)}..${Math.round(b.right)} x `
                    + `${Math.round(b.top)}..${Math.round(b.bottom)}]`);
                }
              }
            }
            return [boxes.length, hits];
        }"""
    )
    count, hits = overlaps
    assert count == 1 + len(_RIM_TITLES), "precondition: not every label was drawn"
    assert hits == [], f"labels overlap: {hits}"
    _assert_the_face_bit(page, face)


# --------------------------------------------------------- crowded rings --


def _label_threshold() -> int:
    """``MAX_LABELLED_NEIGHBOURS`` read from js/graph.js — one number, one place.

    A copy here would let the test and the module disagree silently; parsing
    the constant keeps the test aimed at whatever the module actually ships.
    Absent (the module predates the threshold) reads as 24, the server's cap,
    i.e. "always label" — which is exactly the behaviour being replaced.
    """
    source = (static_dir() / "js" / "graph.js").read_text(encoding="utf-8")
    found = re.search(r"const MAX_LABELLED_NEIGHBOURS = (\d+);", source)
    return int(found.group(1)) if found else 24


#: Every label on the canvas: whose it is, whether it is VISIBLE (a hidden
#: label still has a box), its box, and its rendered font size in px.
_LABELS_JS = """() => {
    const svg = document.querySelector('.local-graph svg');
    const scale = svg.getScreenCTM().a;
    return [...svg.querySelectorAll('text')].map((t) => {
        const r = t.getBoundingClientRect();
        return {
            id: t.closest('[data-note-id]').getAttribute('data-note-id'),
            root: t.closest('.node-root') !== null,
            visible: getComputedStyle(t).visibility === 'visible',
            box: [r.left, r.right, r.top, r.bottom],
            px: parseFloat(getComputedStyle(t).fontSize) * scale,
        };
    });
}"""

_INSPECTOR_BOX_JS = """() => {
    const r = document.getElementById('inspector').getBoundingClientRect();
    return [r.left, r.right];
}"""


def _intersections(labels: list[dict[str, Any]]) -> list[str]:
    shown = [lab for lab in labels if lab["visible"]]
    hits = []
    for i, a in enumerate(shown):
        for b in shown[i + 1:]:
            (al, ar, at, ab), (bl, br, bt, bb) = a["box"], b["box"]
            if al < br and bl < ar and at < bb and bt < ab:
                hits.append(f"{a['id'][-4:]} {[round(v) for v in a['box']]} meets "
                            f"{b['id'][-4:]} {[round(v) for v in b['box']]}")
    return hits


def _open_ring(page: Any, count: int, width: int) -> list[tuple[str, str]]:
    titles = _crowd_titles(count)
    page.set_viewport_size({"width": width, "height": 800})
    _GRAPH["payload"] = _server_ring_payload(titles)
    # A distinct note per count: the client caches each note's graph, so
    # reopening the same id would draw the PREVIOUS ring without a request.
    _open(page, f"aaaaaaaa-0000-4000-8000-{0x900 + count:012x}")
    page.wait_for_selector(".local-graph svg")
    return titles


_WIDTHS = [320, 400, 1280]

#: The one width at which a crowded ring's wide run cannot force a cut — see
#: test_a_crowded_ring_labels_only_the_neighbour_in_hand's docstring.
_EDGE_NEVER_BINDS_CROWDED = 1280


@pytest.mark.parametrize("face", _FACES)
@pytest.mark.parametrize("viewport_width", _WIDTHS)
def test_every_ring_up_to_the_threshold_is_fully_labelled_and_legible(
    page: Any, viewport_width: int, face: str,
) -> None:
    """(15a) Counts 1..threshold: every label shown, none intersecting, >=10px.

    EVERY count up to the threshold, not just the threshold itself: which
    labels share a row is not monotonic in the count (at 3 the two lower
    labels do, at 4 the rim pair does, at 2 none do — and on macOS metrics,
    before graph.js fitted labels to their slots, 5 collided and 6 did not),
    so "the threshold count is clean" would not imply the smaller ones are.
    """
    threshold = _label_threshold()
    _use_face(page, face)
    for count in range(1, threshold + 1):
        _open_ring(page, count, viewport_width)
        labels = page.evaluate(_LABELS_JS)
        left, right = page.evaluate(_INSPECTOR_BOX_JS)
        assert len(labels) == 1 + count, f"precondition: {count} neighbours drew {len(labels)}"
        assert all(lab["visible"] for lab in labels), (
            f"count {count} is at or under the threshold, but a label is hidden"
        )
        hits = _intersections(labels)
        assert hits == [], f"count {count} at {viewport_width}px: labels overlap: {hits}"
        cut = [lab["id"][-4:] for lab in labels
               if lab["box"][0] < left or lab["box"][1] > right]
        assert cut == [], f"count {count} at {viewport_width}px: labels clipped: {cut}"
        small = sorted({round(lab["px"], 2) for lab in labels if lab["px"] < 10})
        assert small == [], (
            f"labels render at {small}px at a {viewport_width}px viewport; "
            "they must be at least 10px to be readable"
        )
    # The last ring drawn is the threshold's own, which has a shared row.
    _assert_the_face_bit(page, face)


@pytest.mark.parametrize("face", _FACES)
@pytest.mark.parametrize("count_kind", ["threshold+1", "cap"])
@pytest.mark.parametrize("viewport_width", _WIDTHS)
def test_a_crowded_ring_labels_only_the_neighbour_in_hand(
    page: Any, viewport_width: int, count_kind: str, face: str,
) -> None:
    """(15b) Over the threshold, a neighbour's label shows only while it is
    hovered or focused — and then it collides with nothing that is showing.

    The NO-INTERSECTION assertion comes first, at rest, so a threshold set too
    high fails on what the reader would actually see: overlapping text.

    **The 1280 ``wide`` runs are exempt from the face bit, and are copies of
    the 1280 ``platform`` runs.** On a crowded ring only the inspector's edge
    binds a label (no two show together), and at 1280 the inspector centres
    the graph with room to spare: measured, a rim label's slot is ~540
    viewBox units, while the widest 28-character label the wide face draws is
    ~250 — and the character cap cuts any longer title first. No title this
    test could use reaches the edge there, so nothing at 1280 tests the fit;
    320 and 400 do, and assert it.
    """
    count = _label_threshold() + 1 if count_kind == "threshold+1" else 24
    _use_face(page, face)
    titles = _open_ring(page, count, viewport_width)

    labels = page.evaluate(_LABELS_JS)
    assert len(labels) == 1 + count, "precondition: not every node was drawn"
    hits = _intersections(labels)
    assert hits == [], f"{count} neighbours at rest: labels overlap: {hits}"
    shown = [lab["id"] for lab in labels if lab["visible"] and not lab["root"]]
    assert shown == [], f"{count} neighbours at rest, yet {len(shown)} labels show"

    # Index 1 sits beside 12 o'clock; count // 4 is 3 o'clock, on the root's row.
    left, right = page.evaluate(_INSPECTOR_BOX_JS)
    for index, how in ((1, "hover"), (count // 4, "focus")):
        node_id = titles[index][0]
        selector = f'.local-graph a.node[data-note-id="{node_id}"]'
        if how == "hover":
            page.hover(f"{selector} circle")
        else:
            page.mouse.move(1, 1)
            page.focus(selector)
        labels = page.evaluate(_LABELS_JS)
        shown = [lab["id"] for lab in labels if lab["visible"] and not lab["root"]]
        assert shown == [node_id], f"{how} on #{index} showed {shown}"
        hits = _intersections(labels)
        assert hits == [], f"{how} on #{index}: the shown label overlaps: {hits}"
        mine = next(lab for lab in labels if lab["id"] == node_id)
        assert left <= mine["box"][0] and mine["box"][1] <= right, (
            f"{how} on #{index}: the label is clipped by the inspector"
        )
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        page.mouse.move(1, 1)
    if viewport_width != _EDGE_NEVER_BINDS_CROWDED:  # the exemption, docstring above
        _assert_the_face_bit(page, face)


def test_a_crowded_ring_still_names_every_neighbour(page: Any) -> None:
    """(15c) Hiding labels is visual only: at the 24 cap every neighbour is
    still a link named by its FULL title (from its <title>)."""
    titles = _open_ring(page, 24, 1280)
    snapshot = page.locator("figure.local-graph").aria_snapshot()
    missing = [title for _, title in titles if f'link "{title}"' not in snapshot]
    assert missing == [], f"neighbours lost their accessible name: {missing}\n{snapshot}"


def test_labels_are_refitted_when_the_inspector_narrows(page: Any) -> None:
    """(15d) A label's slot is partly the inspector's edge, and that edge moves
    in viewBox units when the window is resized — so a ring fitted on a wide
    window must be REFITTED when it narrows, not left at its old cut.

    Drawn at 1280, where the rim labels have room to spare against the
    inspector, then narrowed to 320, where the same cut runs past it. Wide
    face, so the difference does not hang on one machine's metrics.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _use_face(page, "wide")
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    page.wait_for_selector(".local-graph svg")
    wide_cut = page.evaluate("() => [...document.querySelectorAll('.local-graph text')]"
                             ".map((t) => t.textContent)")

    page.set_viewport_size({"width": 320, "height": 800})
    # The observer's callback runs in the frame after layout; two frames settle it.
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    labels = page.evaluate(_LABELS_JS)
    left, right = page.evaluate(_INSPECTOR_BOX_JS)
    cut = [lab["id"][-4:] for lab in labels if lab["box"][0] < left or lab["box"][1] > right]
    assert cut == [], f"after narrowing to 320px, labels are clipped: {cut}"
    assert _intersections(labels) == [], "after narrowing to 320px, labels overlap"
    narrow_cut = page.evaluate("() => [...document.querySelectorAll('.local-graph text')]"
                               ".map((t) => t.textContent)")
    assert narrow_cut != wide_cut, "precondition: narrowing changed no label's cut"


_LABEL_TEXTS_JS = (
    "() => [...document.querySelectorAll('.local-graph text')].map((t) => t.textContent)"
)

#: Hide the inspector the way the phone layout does (components.css: list view
#: below 780px sets it `display: none`), redraw, and read the labels IN THE
#: SAME TASK. Read a frame later and the ResizeObserver has already refitted
#: them, so a broken draw-time fit would be repaired before any assertion saw
#: it — and stay broken for good in a browser without ResizeObserver.
_DRAW_WHILE_HIDDEN_JS = """async () => {
    const graph = await import("/static/js/graph.js");
    document.body.dataset.view = "list";
    graph.renderGraph();
    const inspector = document.getElementById("inspector");
    return [getComputedStyle(inspector).display,
            [...document.querySelectorAll('.local-graph text')].map((t) => t.textContent)];
}"""


def test_a_graph_drawn_in_a_hidden_inspector_is_not_cut_to_ellipses(page: Any) -> None:
    """(15e) A hidden inspector has no box to fit labels to, so a draw there
    must leave them alone — not fit them to a slot measured off a zero box.

    The regression: in Chromium a ``display: none`` svg still returns a
    non-null ``getScreenCTM()`` (a = 1), so a guard on the CTM passed, the
    inspector's all-zero rect made every slot negative, and every label — the
    root's included — became a bare "…". Phone width, list view, any dispatch
    while a note is open. Then, shown again, the labels must be fitted exactly
    as a fresh draw on the visible inspector fits them. Wide face, so that fit
    has cuts to make and "fitted correctly" is not trivially "untouched".
    """
    page.set_viewport_size({"width": 400, "height": 800})
    _use_face(page, "wide")
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    page.wait_for_selector(".local-graph svg")

    display, hidden = page.evaluate(_DRAW_WHILE_HIDDEN_JS)
    assert display == "none", f"precondition: the inspector is not hidden ({display})"
    assert len(hidden) == 1 + len(_RIM_TITLES), "precondition: not every label was drawn"
    bare = [text for text in hidden if text == "…"]
    assert bare == [], f"drawn while hidden, {len(bare)} labels became a bare ellipsis: {hidden}"

    page.evaluate("() => { document.body.dataset.view = 'note'; }")
    # The observer's callback runs in the frame after layout; two frames settle it.
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    refitted = page.evaluate(_LABEL_TEXTS_JS)
    labels = page.evaluate(_LABELS_JS)
    left, right = page.evaluate(_INSPECTOR_BOX_JS)
    clipped = [lab["id"][-4:] for lab in labels if lab["box"][0] < left or lab["box"][1] > right]
    assert clipped == [], f"shown again, labels are clipped: {clipped}"
    assert _intersections(labels) == [], "shown again, labels overlap"
    page.evaluate("async () => (await import('/static/js/graph.js')).renderGraph()")
    assert refitted == page.evaluate(_LABEL_TEXTS_JS), (
        "shown again, the labels are not the fit a fresh draw makes"
    )
    _assert_the_face_bit(page, "wide")
