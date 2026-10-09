"""Local-graph LABEL GEOMETRY on the server's real ring, in a real browser.

Split out of ``tests/test_ui_browser_graph.py`` to keep each module under the
800-line ceiling; the stub routing and fixtures are shared through
``tests/ui_graph_harness.py`` and the ring, faces and measurements through
``tests/ui_graph_geometry.py``, not copied. Everything here builds its
``/graph`` stub with the server's own ring formula (``graph_layout``) and
measures label BOXES: inside the inspector, never intersecting, clear of each
other's halo, legible on a phone, and hidden at rest on a ring too crowded to
label. The refit that keeps them so when a box resizes, and what its
ResizeObserver watches, is ``tests/test_ui_browser_graph_refit.py``.

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

**One runs at the platform font, by design:** (15i), the edge clearance
against the label halo. Its subject is one ``i`` being narrower than the halo,
which a widened face works against; it guards that, and that the edge really
did cut its label, itself.

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
    _TWO_FRAMES_JS,
    _assert_the_face_bit,
    _crowd_titles,
    _graph_js_constant,
    _halo_shortfalls,
    _intersections,
    _label_threshold,
    _server_ring_payload,
    _use_face,
)
from tests.ui_graph_harness import (
    _GRAPH,
    ALPHA_ID,
    ROOT_ID,
    ROOT_TITLE,
    _open,
    _page_fixture,  # noqa: F401 — registers the `page` fixture
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`, which `page` uses
)

pytestmark = pytest.mark.browser


# ------------------------------------------------------------------ layout --


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
    in advance units, asks for the halo's width of clearance as well.
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
    _assert_the_face_bit(page, face)


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

def _label_max_chars() -> int:
    """``LABEL_MAX_CHARS``, the client's character cap, read from graph.js."""
    found = _graph_js_constant("LABEL_MAX_CHARS")
    assert found is not None, "precondition: graph.js has no LABEL_MAX_CHARS"
    return found


def _capped(title: str, cap: int) -> str:
    """What graph.js ``cut`` draws for ``title`` at ``cap`` characters."""
    return title if len(title) <= cap else f"{title[:cap]}…"


#: One label measured against the INSPECTOR'S EDGE ALONE, the only bound a
#: crowded ring has: its slot is twice the distance from its node to the
#: nearer edge, less ``clearance`` on each side (fitLabels' edge rule). Also
#: measures the next-longer cut of the same title — one more character — by
#: setting it on the label, and puts the label back. Same element, same font,
#: same ``getComputedTextLength`` fitText uses, so the comparison is exact.
_EDGE_FIT_JS = """([id, clearance, cap]) => {
    const svg = document.querySelector('.local-graph svg');
    const node = [...svg.querySelectorAll('[data-note-id]')]
        .find((n) => n.getAttribute('data-note-id') === id);
    const text = node.querySelector('text');
    const title = node.querySelector('title').textContent;
    const ctm = svg.getScreenCTM();
    const host = document.getElementById('inspector');
    const edge = host.getBoundingClientRect().left + host.clientLeft;
    const left = (edge - ctm.e) / ctm.a + clearance;
    const right = (edge + host.clientWidth - ctm.e) / ctm.a - clearance;
    const x = Number(text.getAttribute('x'));
    const shown = text.textContent;
    const kept = shown.endsWith('…') ? shown.length - 1 : shown.length;
    let longer = null;
    let longerWidth = null;
    if (kept < Math.min(title.length, cap)) {
        longer = title.length > kept + 1 ? `${title.slice(0, kept + 1)}…` : title;
        text.textContent = longer;
        longerWidth = text.getComputedTextLength();
        text.textContent = shown;
    }
    return {shown, kept, title, slot: 2 * Math.min(x - left, right - x),
            width: text.getComputedTextLength(), longer, longerWidth};
}"""


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
    hovered or focused — and then it collides with nothing that is showing,
    and is cut by the inspector's edge ALONE.

    The NO-INTERSECTION assertion comes first, at rest, so a threshold set too
    high fails on what the reader would actually see: overlapping text.

    **The shown label's TEXT is pinned, not only its box.** On a crowded ring
    no two labels show together, so fitLabels skips the row bound there; were
    it applied, a hidden neighbour's box would still cut the label in hand —
    beside 12 o'clock on the 24 ring, "Synthetic Planning Note 0001" became
    "S…", and the box checks below stayed green. So: at 1280, where the edge
    never binds, the label is its whole (capped) title; at every width it is
    the LONGEST cut the edge-only slot holds — it fits, and one character
    more would not.

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

    # Two DIFFERENT neighbours at both counts. Hover: index 1 — beside 12
    # o'clock on the 24 ring, its row shared with the 12 o'clock label's
    # (hidden) box; on the threshold+1 ring of 5, the 2 o'clock node, level with
    # the root's label. Focus: the node nearest 9 o'clock, from the ring's
    # geometry — 18 of 24 (on the root's row), 4 of 5 (10 o'clock, level with
    # the root's label too).
    clearance = _graph_js_constant("LABEL_CLEARANCE")
    assert clearance is not None, "precondition: graph.js has no LABEL_CLEARANCE"
    cap = _label_max_chars()
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
        fit = page.evaluate(_EDGE_FIT_JS, [node_id, clearance, cap])
        if viewport_width == _EDGE_NEVER_BINDS_CROWDED:
            assert fit["shown"] == _capped(fit["title"], cap), (
                f"{how} on #{index} at 1280, where the edge never binds: "
                f"{fit['shown']!r} is not the whole title {fit['title']!r}"
            )
        assert fit["width"] <= fit["slot"], (
            f"{how} on #{index}: {fit['shown']!r} is wider than its edge slot"
        )
        assert fit["longer"] is None or fit["longerWidth"] > fit["slot"], (
            f"{how} on #{index}: cut to {fit['shown']!r}, but {fit['longer']!r} "
            f"({fit['longerWidth']:.1f}) fits the edge slot ({fit['slot']:.1f}) — "
            "something other than the inspector's edge cut it"
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


#: A neighbour whose title is all ``i``, the narrowest common glyph, so one
#: more character widens its label by well under the halo — see 15i.
_NARROW_TITLE = "i" * 28

#: How far inside the inspector's padding edge, in viewBox units, 15i places
#: its neighbour: a slot of twice this, less the clearance, holds a few ``i``.
_NARROW_NODE_INSET = 16

#: A left border on the inspector, so the padding edge the label is clipped
#: at is NOT the border-box edge ``getBoundingClientRect`` reports.
_INSPECTOR_BORDER_CSS = "#inspector { border-left: 24px solid transparent; }"


def test_the_edge_clearance_covers_the_label_halo(page: Any) -> None:
    """(15i) A label cut by the inspector's edge keeps clear of it by more than
    the halo graph.css strokes round every glyph — on any font.

    The rim and overlap tests assert the same bar (``_halo_shortfalls``), but
    whether they catch a fit with NO clearance hangs on the font: such a fit
    leaves each side a gap of anything under half the next character's width,
    and for most letters that half is wider than the halo. Measured on macOS
    with the clearance set to 0, four of their ``wide`` runs went red and every
    ``platform`` run stayed green. Here the outcome is fixed: one neighbour,
    placed near the left edge so its slot is a few characters wide, titled in
    ``i`` — so the gap a clearance-free fit leaves is under half of one ``i``,
    which the last precondition measures to be under the halo. With the
    clearance, the gap is at least the clearance, which is over the halo.

    The inspector carries a LEFT BORDER, so the edge fitLabels fits to must be
    the padding edge (``+ host.clientLeft``), not the border box: the app's
    own #inspector has no left border, which leaves that term untested
    everywhere else.
    """
    page.set_viewport_size({"width": 320, "height": 800})
    page.add_style_tag(content=_INSPECTOR_BORDER_CSS)
    # Draw once to MEASURE where the inspector's padding edge falls in viewBox
    # units, then place the neighbour a fixed distance inside it — so its slot
    # is a few characters wide whatever the layout, not by a hand-picked x.
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    page.wait_for_selector(".local-graph svg")
    edge = page.evaluate(_EXTENTS_JS)["left"]
    centre = _SERVER_SIZE / 2
    _GRAPH["payload"] = {
        "id": ROOT_ID, "width": _SERVER_SIZE, "height": _SERVER_SIZE,
        "nodes": [
            {"id": ROOT_ID, "title": ROOT_TITLE, "kind": "vault",
             "x": centre, "y": centre, "r": 9, "root": True},
            {"id": ALPHA_ID, "title": _NARROW_TITLE, "kind": "vault",
             "x": edge + _NARROW_NODE_INSET, "y": centre, "r": 6, "root": False},
        ],
        "edges": [{"src": ROOT_ID, "dst": ALPHA_ID, "kind": "wiki"}],
        "truncated": 0, "corpus_linked": True,
    }
    # A distinct note: the client caches each note's graph, so reopening the
    # first id would redraw the ring without a request.
    _open(page, "aaaaaaaa-0000-4000-8000-000000000bbb")
    page.wait_for_selector(".local-graph svg")
    page.evaluate(_TWO_FRAMES_JS)

    assert page.evaluate("() => document.getElementById('inspector').clientLeft") > 0, (
        "precondition: the inspector has no left border"
    )
    spill = page.evaluate(
        """() => {
            const host = document.getElementById('inspector');
            const box = host.getBoundingClientRect();
            const padding = box.left + host.clientLeft;
            return [...document.querySelectorAll('.local-graph text')]
              .filter((t) => t.getBoundingClientRect().left < padding)
              .map((t) => t.textContent);
        }"""
    )
    assert spill == [], f"labels run under the inspector's left border: {spill}"
    short = _halo_shortfalls(page.evaluate(_EXTENTS_JS))
    assert short == [], f"the cut label sits inside its own halo of the edge: {short}"

    fit = page.evaluate(_EDGE_FIT_JS, [ALPHA_ID, 0, _label_max_chars()])
    halo = next(lab["halo"] for lab in page.evaluate(_EXTENTS_JS)["labels"]
                if lab["text"] == fit["shown"])
    assert fit["shown"].endswith("…") and fit["kept"] > 0, (
        f"precondition: the edge did not cut the label to a few characters ({fit['shown']!r})"
    )
    assert (fit["longerWidth"] - fit["width"]) / 2 < halo, (
        "precondition: one more character widens each side by "
        f"{(fit['longerWidth'] - fit['width']) / 2:.2f}, not under the halo ({halo})"
    )


