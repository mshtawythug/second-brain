"""Local-graph LABEL GEOMETRY on the server's real ring, in a real browser.

Split out of ``tests/test_ui_browser_graph.py`` to keep each module under the
800-line ceiling; the stub routing and fixtures are shared through
``tests/ui_graph_harness.py`` and the ring, faces and measurements through
``tests/ui_graph_geometry.py``, not copied. Everything here builds its
``/graph`` stub with the server's own ring formula (``graph_layout``) and
measures label BOXES: inside the inspector, never intersecting, clear of each
other's halo, legible on a phone, and hidden at rest on a ring too crowded to
label — and holds every label's TEXT to an optimality oracle
(``_assert_optimal_cuts``): the longest cut of its title that fits the slot
the spec gives it, recomputed in the test. A box check cannot see a label cut
SHORTER than it needs to be — the left label of a shared row reduced to a bare
"…" stays inside every box — so every test here that checks a label's fit
applies the oracle too, at every width and face it runs. Four test runs here
check no fit and do not apply it (plan §7.4): (14b), which checks where the
root's label box sits; (15c), which checks accessible names; and both (15n)
runs, which check the crowded-ring threshold. The refit that keeps them so
when a box resizes, and what its ResizeObserver watches, is
``tests/test_ui_browser_graph_refit.py``.

**The four parametrized geometric tests run twice: at the platform's default
font, and at a deliberately WIDE face** (``_WIDE_FACE_CSS``) — the rim,
overlap, threshold and crowded-ring tests. The label geometry was first
measured against macOS metrics only, and it passed here while GitHub's Linux
runner — whose default sans is the wider DejaVu Sans — rendered overlapping
and clipped labels. The ``wide`` runs put that failure on every machine, and
each one asserts at its END that the wide face really did force a cut
(``_assert_the_face_bit``), so a face that stops being wide cannot turn them
into silent copies of the ``platform`` runs. **One named exemption:** the
crowded-ring test's two 1280 ``wide`` runs ARE copies of their ``platform``
runs and do not assert the cut, because at 1280 no 28-character label reaches
the only bound a crowded ring has, the inspector's edge (measured; the reason
is in that test).

**Five run at the platform font, by design**, each placing its nodes so its
outcome does not hang on the font: (15i) the clearance at both edges of an
inspector with side borders, (15j) the clearance between two labels sharing a
row, both with titles of ``i`` — one ``i`` narrower than the clearance, which
a widened face works against — (15k) a slot placed between a long title's
27- and 28-character cuts, (15l) a slot narrower than the bare "…", and
(15m) a slot that holds a long title's cut one past the cap. Each measures,
as preconditions, that its placement really did produce the case it is about.
Two more run at the platform font only, being about no label's WIDTH, which
is what the wide face widens: (14b) the root's label sits above the root, on
a row of its own, and (15n) the crowded-ring threshold is the spec's 4 — a
ring of 4 is labelled at rest, a ring of 5 is crowded.

**graph.js's constants are pinned at every test's TEARDOWN**, by the autouse
``spec_constants_pin`` fixture imported from ``tests/ui_graph_geometry.py``:
after the test's last assertion, so a changed constant fails first on what it
does to the labels, and an ERROR at teardown (not a FAIL) where it does nothing
the test can see.

**The filename is load-bearing.** CI names every browser module explicitly
(``.github/workflows/ci.yml``), and ``tests/test_ci_workflow.py`` fails if a
``browser``-marked module is missing from that list. Run it by path:

    pytest tests/test_ui_browser_graph_layout.py -m browser --no-cov
"""
from __future__ import annotations

import math
from typing import Any

import pytest

from tests.ui_graph_geometry import (
    _EXTENTS_JS,
    _FACES,
    _INSPECTOR_BOX_JS,
    _LABELS_JS,
    _RIM_TITLES,
    _SERVER_SIZE,
    _SPEC_LABEL_CLEARANCE,
    _SPEC_LABEL_MAX_CHARS,
    _SPEC_LABEL_THRESHOLD,
    _TWO_FRAMES_JS,
    _assert_optimal_cuts,
    _assert_the_face_bit,
    _crowd_titles,
    _fits,
    _halo_shortfalls,
    _intersections,
    _server_ring_payload,
    _spec_constants_pin_fixture,  # noqa: F401 — autouse: pins graph.js's constants after each test
    _use_face,
)
from tests.ui_graph_harness import (
    _GRAPH,
    ALPHA_ID,
    BRAVO_ID,
    ROOT_ID,
    ROOT_TITLE,
    _open,
    _page_fixture,  # noqa: F401 — registers the `page` fixture
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`, which `page` uses
)

pytestmark = pytest.mark.browser


# ------------------------------------------------------------------ layout --


#: 320 / 400: below 780px the ledger and inspector become a two-view stack
#: (components.css), so on a phone the inspector is the whole viewport with the
#: narrow padding — the narrowest it ever gets. 1280: the normal desktop.
@pytest.mark.parametrize("face", _FACES)
@pytest.mark.parametrize("viewport_width", [320, 400, 1280])
def test_rim_labels_stay_inside_the_inspector(page: Any, viewport_width: int, face: str) -> None:
    """(13) Labels at the server's 3 and 9 o'clock nodes are not cut off.

    TWO assertions on the edge, because the first one ALONE CANNOT FAIL on
    label spill — measured: text overflowing an ``overflow: visible`` svg is
    INK overflow in Chromium, not scrollable overflow, so it never widens the
    inspector's scroll box. It is CLIPPED at the inspector's edge instead
    (``overflow-y: auto`` clips both axes). So the property a reader would
    notice — every label wholly inside the visible inspector — is asserted
    directly, and the scroll check stays as the cheap half. A third, exact
    in advance units, asks for the halo's width of clearance as well, and the
    oracle that no label is cut shorter than its slot requires.
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
    short = _halo_shortfalls(page.evaluate(_EXTENTS_JS))
    assert short == [], f"labels sit inside their own halo of the edge or a neighbour: {short}"
    _assert_optimal_cuts(page, f"the rim ring at {viewport_width}px")
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
    the root's label cannot trade one collision for another unnoticed. Then
    the halo: labels sharing a row keep its width apart (``_halo_shortfalls``).
    Then the oracle: none of them is cut shorter than its slot requires.
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
    short = _halo_shortfalls(page.evaluate(_EXTENTS_JS))
    assert short == [], f"labels sit inside their own halo of the edge or a neighbour: {short}"
    _assert_optimal_cuts(page, f"the server ring at {viewport_width}px")
    _assert_the_face_bit(page, face)


#: The root's circle top and label row, and every neighbour label's row, in
#: viewBox units (``getBBox``, as fitLabels measures rows).
_ROOT_ROW_JS = """() => {
    const root = document.querySelector('.local-graph .node-root');
    const circle = root.querySelector('circle');
    const row = (t) => { const b = t.getBBox(); return [t.textContent, b.y, b.y + b.height]; };
    return {
        circleTop: Number(circle.getAttribute('cy')) - Number(circle.getAttribute('r')),
        label: row(root.querySelector('text')),
        others: [...document.querySelectorAll('.local-graph a.node > text')].map(row),
    };
}"""


def test_the_root_label_sits_above_the_root_on_a_row_of_its_own(page: Any) -> None:
    """(14b) The root's label is drawn ABOVE the root — its box's bottom at or
    over the circle's top — and so, on the server's ring, shares no row with
    any other label: no neighbour's slot is narrowed by it.

    (14) cannot pin this. Below the root, the root's label shares a row with
    the 3 and 9 o'clock labels, and fitLabels cuts all three until none
    overlaps — so every box check stays green, and only the cuts moved. Rows
    are judged as fitLabels judges them: within the spec's clearance.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    page.wait_for_selector(".local-graph svg")

    rows = page.evaluate(_ROOT_ROW_JS)
    _, top, bottom = rows["label"]
    assert bottom <= rows["circleTop"], (
        f"the root's label reaches {bottom:.2f}, below the root's top {rows['circleTop']:.2f}"
    )
    sharing = [text for text, other_top, other_bottom in rows["others"]
               if other_bottom + _SPEC_LABEL_CLEARANCE > top
               and bottom + _SPEC_LABEL_CLEARANCE > other_top]
    assert sharing == [], f"labels share the root label's row: {sharing}"


# --------------------------------------------------------- crowded rings --


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


def _nearest_on_ring(count: int, clock_hour: int) -> int:
    """The index of the neighbour nearest ``clock_hour`` on the server's ring
    of ``count`` (neighbour ``i`` at ``2*pi*i/count`` clockwise from 12)."""
    target = 2 * math.pi * clock_hour / 12

    def off(index: int) -> float:
        delta = abs(2 * math.pi * index / count - target) % (2 * math.pi)
        return min(delta, 2 * math.pi - delta)

    return min(range(count), key=off)


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
    At every count, every label is also held to the oracle. The threshold is
    the SPEC's, not graph.js's: read from graph.js, it would loop to 5 the
    moment graph.js said 5, and pass the very rings the threshold exists to
    keep unlabelled. graph.js's constants are pinned at teardown, after the
    last count, so a changed one fails first on the counts whose cuts it
    changes (for a clearance of 0, 3 and 4: measured).
    """
    _use_face(page, face)
    for count in range(1, _SPEC_LABEL_THRESHOLD + 1):
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
        _assert_optimal_cuts(page, f"count {count} at {viewport_width}px")
    # The last ring drawn is the threshold's own, which has a shared row.
    _assert_the_face_bit(page, face)


@pytest.mark.parametrize("face", _FACES)
@pytest.mark.parametrize("count_kind", ["threshold+1", "cap"])
@pytest.mark.parametrize("viewport_width", _WIDTHS)
def test_a_crowded_ring_labels_only_the_neighbour_in_hand(
    page: Any, viewport_width: int, count_kind: str, face: str,
) -> None:
    """(15b) Over the threshold, a neighbour's label shows only while it is
    hovered or focused — and then it collides with nothing that is showing,
    and is cut by the inspector's edge ALONE.

    The NO-INTERSECTION assertion comes first, at rest, so a threshold set too
    high fails on what the reader would actually see: overlapping text.

    **The shown label's TEXT is pinned, not only its box.** On a crowded ring
    no two labels show together, so fitLabels skips the row bound there; were
    it applied, a hidden neighbour's box would still cut the label in hand —
    beside 12 o'clock on the 24 ring, "Synthetic Planning Note 0001" became
    "S…", and the box checks below stayed green. So: at 1280, where the edge
    never binds, the label is its whole (capped) title; at every width EVERY
    label — the one in hand and the hidden ones — is the LONGEST cut the
    edge-only slot holds: the oracle, without its row bound.

    **The 1280 ``wide`` runs are exempt from the face bit, and are copies of
    the 1280 ``platform`` runs.** On a crowded ring only the inspector's edge
    binds a label (no two show together), and at 1280 the inspector centres
    the graph with room to spare: measured, a rim label's slot is ~540
    viewBox units, while the widest 28-character label the wide face draws is
    ~250 — and the character cap cuts any longer title first. No title this
    test could use reaches the edge there, so nothing at 1280 tests the fit;
    320 and 400 do, and assert it.
    """
    count = _SPEC_LABEL_THRESHOLD + 1 if count_kind == "threshold+1" else 24
    _use_face(page, face)
    titles = _open_ring(page, count, viewport_width)

    labels = page.evaluate(_LABELS_JS)
    assert len(labels) == 1 + count, "precondition: not every node was drawn"
    hits = _intersections(labels)
    assert hits == [], f"{count} neighbours at rest: labels overlap: {hits}"
    shown = [lab["id"] for lab in labels if lab["visible"] and not lab["root"]]
    assert shown == [], f"{count} neighbours at rest, yet {len(shown)} labels show"

    # Two DIFFERENT neighbours at both counts. Hover: index 1 — beside 12
    # o'clock on the 24 ring, its row shared with the 12 o'clock label's
    # (hidden) box; on the threshold+1 ring of 5, the 2 o'clock node, level with
    # the root's label. Focus: the node nearest 9 o'clock, from the ring's
    # geometry — 18 of 24 (on the root's row), 4 of 5 (10 o'clock, level with
    # the root's label too).
    nine = _nearest_on_ring(count, 9)
    assert nine != 1, f"precondition: hover and focus both target #{nine}"
    left, right = page.evaluate(_INSPECTOR_BOX_JS)
    for index, how in ((1, "hover"), (nine, "focus")):
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
        # The oracle with NO row bound: every label, the one in hand and the
        # hidden ones alike, is the longest cut the inspector's edge alone
        # allows — so a row bound applied here over-cuts and fails it.
        fits = _assert_optimal_cuts(page, f"{how} on #{index}", rows=False)
        fit = next(f for f in fits if f["id"] == node_id)
        if viewport_width == _EDGE_NEVER_BINDS_CROWDED:
            assert fit["longer"] is None, (
                f"{how} on #{index} at 1280, where the edge never binds: "
                f"{fit['shown']!r} is not the whole title {fit['title']!r}"
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


_CROWDED_JS = "() => document.querySelector('.local-graph svg').hasAttribute('data-crowded')"


@pytest.mark.parametrize(
    "count", [_SPEC_LABEL_THRESHOLD, _SPEC_LABEL_THRESHOLD + 1], ids=["threshold", "threshold+1"],
)
def test_a_ring_is_crowded_from_one_past_the_specified_threshold(page: Any, count: int) -> None:
    """(15n) The threshold is the SPEC's 4 (plan §7.2), held by the test and
    not read from graph.js: a ring of 4 is not crowded and shows every label
    at rest; a ring of 5 is crowded (``data-crowded``, by which graph.css
    hides neighbour labels and fitLabels drops the row bound) and shows only
    the root's.

    Both directions: a threshold of 3 crowds the 4-ring, and one of 5 leaves
    the 5-ring labelled, three labels on the root's row, each cut to about
    half a title.
    """
    titles = _open_ring(page, count, 1280)
    crowded = page.evaluate(_CROWDED_JS)
    labels = page.evaluate(_LABELS_JS)
    assert len(labels) == 1 + count, "precondition: not every node was drawn"
    assert crowded == (count > _SPEC_LABEL_THRESHOLD), (
        f"a ring of {count} is {'' if crowded else 'not '}crowded"
    )
    shown = sorted(lab["id"] for lab in labels if lab["visible"])
    labelled = [ROOT_ID] if crowded else [ROOT_ID, *(node_id for node_id, _ in titles)]
    assert shown == sorted(labelled), f"a ring of {count} at rest shows {len(shown)} labels"


#: A neighbour whose title is all ``i``, the narrowest common glyph, so one
#: more character widens its label by less than the halo and the clearance —
#: see 15i and 15j.
_NARROW_TITLE = "i" * 28

#: How far inside each of the inspector's padding edges, in viewBox units, 15i
#: places a neighbour: a slot of twice this less the clearance on each side
#: (24 units at clearance 4), which holds a few ``i``.
_NARROW_NODE_INSET = 16

#: Borders on BOTH sides of the inspector, so neither padding edge the labels
#: are clipped at is the border-box edge ``getBoundingClientRect`` reports, and
#: the padding box's width (``clientWidth``) is not the border box's.
_INSPECTOR_BORDER_CSS = (
    "#inspector { border-left: 24px solid transparent;"
    " border-right: 24px solid transparent; }"
)

#: The inspector's two border widths, in px, and the svg's scale (px per
#: viewBox unit).
_BORDERS_JS = """() => {
    const style = getComputedStyle(document.getElementById('inspector'));
    return [parseFloat(style.borderLeftWidth), parseFloat(style.borderRightWidth),
            document.querySelector('.local-graph svg').getScreenCTM().a];
}"""

#: Every label running past either of the inspector's PADDING edges.
_PADDING_SPILL_JS = """() => {
    const host = document.getElementById('inspector');
    const left = host.getBoundingClientRect().left + host.clientLeft;
    const right = left + host.clientWidth;
    return [...document.querySelectorAll('.local-graph text')]
      .filter((t) => {
          const r = t.getBoundingClientRect();
          return r.left < left || r.right > right;
      })
      .map((t) => t.textContent);
}"""


def _padding_edges(page: Any) -> tuple[float, float]:
    """Draw the server ring once to MEASURE where the inspector's padding edges
    fall in viewBox units, so a test places its nodes relative to them —
    whatever the layout, not at a hand-picked x."""
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    page.wait_for_selector(".local-graph svg")
    extents = page.evaluate(_EXTENTS_JS)
    return extents["left"], extents["right"]


def _draw_beside_root(
    page: Any, note_id: str, neighbours: list[tuple[str, str, float, float]],
) -> None:
    """Draw the root at the centre and each neighbour ``(id, title, x, y)`` —
    viewBox units — where it is given, as note ``note_id``: the client caches
    each note's graph, so reopening an id would redraw without a request."""
    centre = _SERVER_SIZE / 2
    _GRAPH["payload"] = {
        "id": ROOT_ID, "width": _SERVER_SIZE, "height": _SERVER_SIZE,
        "nodes": [
            {"id": ROOT_ID, "title": ROOT_TITLE, "kind": "vault",
             "x": centre, "y": centre, "r": 9, "root": True},
            *({"id": node_id, "title": title, "kind": "vault",
               "x": x, "y": y, "r": 6, "root": False}
              for node_id, title, x, y in neighbours),
        ],
        "edges": [{"src": ROOT_ID, "dst": node_id, "kind": "wiki"}
                  for node_id, *_ in neighbours],
        "truncated": 0, "corpus_linked": True,
    }
    _open(page, note_id)
    page.wait_for_selector(".local-graph svg")
    page.evaluate(_TWO_FRAMES_JS)


def _halo(page: Any) -> float:
    """The halo graph.css strokes round a label (its computed stroke-width)."""
    return float(page.evaluate(_EXTENTS_JS)["labels"][-1]["halo"])


def test_the_edge_clearance_covers_the_label_halo(page: Any) -> None:
    """(15i) A label cut by the inspector's edge — either edge — keeps clear of
    it by more than the halo graph.css strokes round every glyph, and is the
    longest cut its edge slot holds: on any font.

    The rim and overlap tests assert the same bars, but whether they catch a
    fit with NO clearance hangs on the font: such a fit widens the slot by the
    clearance on each side, and changes the cut only if one more character
    fits in that room — for most letters it does not. Here the outcome is
    fixed: two neighbours, each placed just inside one padding edge so its
    slot is a few characters wide, titled in ``i`` — so one more character is
    narrower than the two clearances it would gain (a precondition measures
    it), a clearance-free fit always takes it, and the oracle
    (``_assert_optimal_cuts``) sees the label overrun its slot. Its gap to the
    edge would also be under half an ``i``, under the halo (measured too).

    The inspector carries a border on BOTH sides, so the edges fitLabels fits
    to must be the padding box's: ``+ host.clientLeft`` on the left, and
    ``clientWidth`` — not the border box's ``offsetWidth`` — on the right.
    The app's own #inspector has no side border, which leaves both terms
    untested everywhere else; each border is wider, in the slot it would add,
    than one more character (measured), so a fit to the border box takes one.
    """
    page.set_viewport_size({"width": 320, "height": 800})
    page.add_style_tag(content=_INSPECTOR_BORDER_CSS)
    left, right = _padding_edges(page)
    centre = _SERVER_SIZE / 2
    _draw_beside_root(page, "aaaaaaaa-0000-4000-8000-000000000bbb", [
        (ALPHA_ID, _NARROW_TITLE, left + _NARROW_NODE_INSET, centre),
        (BRAVO_ID, _NARROW_TITLE, right - _NARROW_NODE_INSET, centre),
    ])

    border_left, border_right, scale = page.evaluate(_BORDERS_JS)
    assert border_left > 0 and border_right > 0, (
        f"precondition: the inspector lacks a side border ({border_left}, {border_right})"
    )
    spill = page.evaluate(_PADDING_SPILL_JS)
    assert spill == [], f"labels run under the inspector's border: {spill}"
    short = _halo_shortfalls(page.evaluate(_EXTENTS_JS))
    assert short == [], f"a cut label sits inside its own halo of the edge: {short}"
    fits = {fit["id"]: fit for fit in _assert_optimal_cuts(page, "beside the edges")}

    halo = _halo(page)
    for node_id, side, border in ((ALPHA_ID, "left", border_left),
                                  (BRAVO_ID, "right", border_right)):
        fit = fits[node_id]
        assert fit["shown"].endswith("…") and fit["kept"] > 0, (
            f"precondition: the {side} edge did not cut its label to a few "
            f"characters ({fit['shown']!r})"
        )
        step = fit["longerWidth"] - fit["width"]
        assert step / 2 < halo, (
            f"precondition: one more character widens the {side} label by "
            f"{step / 2:.2f} a side, not under the halo ({halo})"
        )
        assert fit["longerWidth"] <= fit["slot"] + 2 * _SPEC_LABEL_CLEARANCE, (
            f"precondition: a clearance-free fit at the {side} edge would not "
            f"take one more character ({fit['longerWidth']:.2f} > {fit['slot']:.2f} "
            f"+ 2 * {_SPEC_LABEL_CLEARANCE})"
        )
        assert fit["longerWidth"] <= fit["slot"] + 2 * border / scale, (
            f"precondition: a fit to the {side} border box would not take one "
            "more character"
        )


#: Half the distance between 15j's two neighbours, in viewBox units: their row
#: slot is twice this less the clearance (36 units at clearance 4), which
#: holds a few ``i``, and both sit far enough inside the inspector that only
#: the row binds.
_NARROW_PAIR_HALF_GAP = 20


@pytest.mark.parametrize("stacking", ["level", "stacked"])
def test_the_row_clearance_covers_the_label_halo(page: Any, stacking: str) -> None:
    """(15j) Two labels sharing a row keep the clearance between them, and
    each is the longest cut the row allows: on any font.

    The rim, overlap and threshold tests hold every label to the oracle's
    slot, rows included — but whether a row fit WITHOUT the clearance breaks
    it hangs on the titles. Such a fit widens each row slot by the clearance,
    and changes a cut only if one more character fits in that room; the rim
    titles happened to leave it room to spare. Here the outcome is fixed: two
    neighbours 40 units apart and far from either edge, titled in ``i`` — so
    one more character is narrower than the clearance (a precondition
    measures it), a clearance-free fit always takes it, and the oracle sees
    the label overrun its slot. Clearance-free, each label would end under
    half an ``i`` from the midpoint between the two nodes.

    ``level`` draws the pair on one row. ``stacked`` drops one node until the
    two label BOXES are half the clearance apart vertically: by the rule
    fitLabels applies, that is still one row — a box within the clearance of
    another shares its row — so the cuts must be the same. A row test that
    left the clearance out would call them apart and draw both titles whole,
    halo against halo.
    """
    page.set_viewport_size({"width": 320, "height": 800})
    centre = _SERVER_SIZE / 2
    row = centre + 60
    drop = 0.0
    if stacking == "stacked":
        _padding_edges(page)  # a first draw, to measure a label box's height
        box = page.evaluate(_EXTENTS_JS)["labels"][-1]
        drop = box["bottom"] - box["top"] + _SPEC_LABEL_CLEARANCE / 2
    _draw_beside_root(page, "aaaaaaaa-0000-4000-8000-000000000ccc", [
        (ALPHA_ID, _NARROW_TITLE, centre - _NARROW_PAIR_HALF_GAP, row),
        (BRAVO_ID, _NARROW_TITLE, centre + _NARROW_PAIR_HALF_GAP, row + drop),
    ])

    boxes = {lab["id"]: lab for lab in page.evaluate(_EXTENTS_JS)["labels"]}
    between = boxes[BRAVO_ID]["top"] - boxes[ALPHA_ID]["bottom"]
    assert between < _SPEC_LABEL_CLEARANCE, (
        f"precondition: the pair's boxes are {between:.2f} apart vertically, "
        "too far to share a row"
    )
    hits = _intersections(page.evaluate(_LABELS_JS))
    assert hits == [], f"the pair's labels overlap: {hits}"
    short = _halo_shortfalls(page.evaluate(_EXTENTS_JS))
    assert short == [], f"the pair's labels sit inside each other's halo: {short}"
    fits = {fit["id"]: fit for fit in _assert_optimal_cuts(page, f"a narrow {stacking} row")}

    for node_id in (ALPHA_ID, BRAVO_ID):
        fit = fits[node_id]
        assert fit["slot"] == pytest.approx(
            2 * _NARROW_PAIR_HALF_GAP - _SPEC_LABEL_CLEARANCE), (
            f"precondition: the row does not bind ({fit['slot']:.2f})"
        )
        assert fit["shown"].endswith("…") and fit["kept"] > 0, (
            f"precondition: the row did not cut its label to a few characters "
            f"({fit['shown']!r})"
        )
        assert fit["longerWidth"] <= fit["slot"] + _SPEC_LABEL_CLEARANCE, (
            "precondition: a clearance-free row fit would not take one more "
            f"character ({fit['longerWidth']:.2f} > {fit['slot']:.2f} "
            f"+ {_SPEC_LABEL_CLEARANCE})"
        )
    if stacking == "stacked":
        assert between > 0, f"precondition: the stacked boxes overlap ({between:.2f})"


#: A synthetic title well past the character cap.
_LONG_TITLE = "Synthetic Planning Note With A Longer Title"

#: The measured lengths of ``cuts`` on one neighbour's label — set on it,
#: measured, and put back.
_CUT_LENGTHS_JS = """([id, cuts]) => {
    const text = [...document.querySelectorAll('.local-graph [data-note-id]')]
        .find((n) => n.getAttribute('data-note-id') === id).querySelector('text');
    const shown = text.textContent;
    const lengths = cuts.map((cut) => {
        text.textContent = cut;
        return text.getComputedTextLength();
    });
    text.textContent = shown;
    return lengths;
}"""


def test_a_label_is_cut_one_short_of_the_cap_when_only_that_fits(page: Any) -> None:
    """(15k) The fit searches EVERY cut up to the cap: a slot that holds the
    title cut at 27 characters, but not at 28, shows exactly 27.

    Every other test's NEIGHBOUR titles are exactly 28 characters, except
    (15m)'s, whose slot is placed to hold MORE than the cap — so their
    longest cut short of the whole title is 27 + "…", and whether any slot
    falls between that and the whole title is font luck. A search that
    stopped at 26 passed them all. Here the slot is PLACED there: one
    neighbour with a title past the cap, beside the inspector's left edge at
    the distance that makes its edge slot the midpoint of the 27- and
    28-character cuts' measured lengths (measured first on the same kind of
    label, then again on its own — the precondition).
    """
    page.set_viewport_size({"width": 320, "height": 800})
    left, _ = _padding_edges(page)
    cuts = [f"{_LONG_TITLE[:n]}…" for n in (_SPEC_LABEL_MAX_CHARS - 1, _SPEC_LABEL_MAX_CHARS)]
    short_cut, cap_cut = page.evaluate(_CUT_LENGTHS_JS, [ALPHA_ID, cuts])
    slot = (short_cut + cap_cut) / 2
    centre = _SERVER_SIZE / 2
    _draw_beside_root(page, "aaaaaaaa-0000-4000-8000-000000000ddd", [
        (ALPHA_ID, _LONG_TITLE, left + _SPEC_LABEL_CLEARANCE + slot / 2, centre + 60),
    ])

    short_cut, cap_cut = page.evaluate(_CUT_LENGTHS_JS, [ALPHA_ID, cuts])
    fit = next(fit for fit in _fits(page, rows=True) if fit["id"] == ALPHA_ID)
    assert short_cut <= fit["slot"] < cap_cut, (
        f"precondition: the slot ({fit['slot']:.2f}) is not between the 27- and "
        f"28-character cuts ({short_cut:.2f}, {cap_cut:.2f})"
    )
    _assert_optimal_cuts(page, "a slot one character short of the cap")


def test_a_slot_narrower_than_the_ellipsis_leaves_the_bare_ellipsis(page: Any) -> None:
    """(15l) The floor: a slot too narrow for even one character and "…"
    leaves the bare "…" — fitText's ``lo = 0`` — and the oracle accepts it.

    The server's ring never makes such a slot (graph.js's header), so nothing
    else reaches the floor, or the oracle's one exemption: a label may overrun
    its slot only when it keeps no character at all (``_fit_faults`` (b)).
    Here the slot is PLACED below the floor: one neighbour ON the inspector's
    left padding edge, so its edge slot is negative — narrower than "…"
    itself (the precondition measures it). A search that kept one character
    shows "A…", which overruns the slot and fails the oracle; an oracle
    without the exemption fails the "…" itself.
    """
    page.set_viewport_size({"width": 320, "height": 800})
    left, _ = _padding_edges(page)
    centre = _SERVER_SIZE / 2
    _draw_beside_root(page, "aaaaaaaa-0000-4000-8000-000000000eee", [
        (ALPHA_ID, _RIM_TITLES[0][1], left, centre),
    ])

    (ellipsis,) = page.evaluate(_CUT_LENGTHS_JS, [ALPHA_ID, ["…"]])
    fit = next(fit for fit in _fits(page, rows=True) if fit["id"] == ALPHA_ID)
    assert fit["slot"] < ellipsis, (
        f"precondition: the slot ({fit['slot']:.2f}) holds the bare ellipsis "
        f"({ellipsis:.2f}), so the floor is not reached"
    )
    fits = _assert_optimal_cuts(page, "a slot under the floor")
    fit = next(fit for fit in fits if fit["id"] == ALPHA_ID)
    assert fit["shown"] == "…", f"the floor is the bare ellipsis, not {fit['shown']!r}"


def test_a_title_past_the_cap_is_cut_at_the_cap_where_more_would_fit(page: Any) -> None:
    """(15m) The character cap binds from ABOVE: a title past the cap, in a
    slot that would hold one character more, still shows exactly
    ``_SPEC_LABEL_MAX_CHARS`` characters and "…".

    Every other title in this module is at most the cap, except (15k)'s,
    whose slot is too narrow for even the capped cut — so a cap raised to 29
    changed no label they draw, and only the constant check caught it. Here
    one neighbour with a long title sits mid-canvas, its row its own, where
    its edge slot holds the 29-character cut (the precondition measures it):
    the oracle, which holds its own cap, fails a 29-character label as not a
    cut at the cap.
    """
    page.set_viewport_size({"width": 320, "height": 800})
    centre = _SERVER_SIZE / 2
    _draw_beside_root(page, "aaaaaaaa-0000-4000-8000-000000000fff", [
        (ALPHA_ID, _LONG_TITLE, centre, centre - 80),
    ])

    over = f"{_LONG_TITLE[:_SPEC_LABEL_MAX_CHARS + 1]}…"
    (over_length,) = page.evaluate(_CUT_LENGTHS_JS, [ALPHA_ID, [over]])
    fit = next(fit for fit in _fits(page, rows=True) if fit["id"] == ALPHA_ID)
    assert over_length <= fit["slot"], (
        f"precondition: the slot ({fit['slot']:.2f}) does not hold the cut one "
        f"past the cap ({over_length:.2f}), so a raised cap would change nothing"
    )
    fits = _assert_optimal_cuts(page, "a roomy slot past the cap")
    fit = next(fit for fit in fits if fit["id"] == ALPHA_ID)
    assert fit["shown"] == f"{_LONG_TITLE[:_SPEC_LABEL_MAX_CHARS]}…", (
        f"a title past the cap shows {fit['shown']!r}"
    )
