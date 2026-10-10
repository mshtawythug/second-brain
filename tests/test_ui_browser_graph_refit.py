"""Local-graph label REFIT, in a real browser: the labels are refitted when
the inspector or the drawing resizes, and the ResizeObserver that does it
watches the drawn graph and nothing else.

Split out of ``tests/test_ui_browser_graph_layout.py`` to keep each module
under the 800-line ceiling; the stub routing and fixtures come from
``tests/ui_graph_harness.py`` and the ring, faces and measurements from
``tests/ui_graph_geometry.py``, not copied.

**Four of these tests are WIDE-ONLY**, each with its own face guard:

- (15d) refit when the window narrows — guarded by ``narrow_cut != wide_cut``;
- (15e) a draw into a hidden inspector — ends in ``_assert_the_face_bit``;
- (15f) refit when only the inspector resizes and (15g) when only the drawing
  does — guarded in ``_assert_refitted_as_a_fresh_draw`` by a fresh draw's cut
  differing from the cut before the resize.

(15h) pins what the observer watches, and that a withdrawn drawing is
collectable.

**The filename is load-bearing.** CI names every browser module explicitly
(``.github/workflows/ci.yml``), and ``tests/test_ci_workflow.py`` fails if a
``browser``-marked module is missing from that list. Run it by path:

    pytest tests/test_ui_browser_graph_refit.py -m browser --no-cov
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.ui_graph_geometry import (
    _INSPECTOR_BOX_JS,
    _LABEL_TEXTS_JS,
    _LABELS_JS,
    _RIM_TITLES,
    _TWO_FRAMES_JS,
    _assert_the_face_bit,
    _intersections,
    _server_ring_payload,
    _use_face,
)
from tests.ui_graph_harness import (
    _GRAPH,
    _dispatch,
    _open,
    _page_fixture,  # noqa: F401 — registers the `page` fixture
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`, which `page` uses
    reboot,
)

pytestmark = pytest.mark.browser


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
    page.evaluate(_TWO_FRAMES_JS)
    wide_cut = page.evaluate(_LABEL_TEXTS_JS)

    page.set_viewport_size({"width": 320, "height": 800})
    page.evaluate(_TWO_FRAMES_JS)
    labels = page.evaluate(_LABELS_JS)
    left, right = page.evaluate(_INSPECTOR_BOX_JS)
    cut = [lab["id"][-4:] for lab in labels if lab["box"][0] < left or lab["box"][1] > right]
    assert cut == [], f"after narrowing to 320px, labels are clipped: {cut}"
    assert _intersections(labels) == [], "after narrowing to 320px, labels overlap"
    narrow_cut = page.evaluate(_LABEL_TEXTS_JS)
    assert narrow_cut != wide_cut, "precondition: narrowing changed no label's cut"


#: Hide the inspector the way the phone layout does (components.css: list view
#: below 780px sets it `display: none`), redraw, and read the labels IN THE
#: SAME TASK. Read a frame later and the ResizeObserver has already refitted
#: them, so a broken draw-time fit would be repaired before any assertion saw
#: it.
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
    page.evaluate(_TWO_FRAMES_JS)
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


#: The drawing's border-box width and both inline paddings, in px: its content
#: box — the box the ResizeObserver reports — is fixed when all three are.
_SVG_BOX_JS = """() => {
    const svg = document.querySelector('.local-graph svg');
    const style = getComputedStyle(svg);
    return [svg.getBoundingClientRect().width, style.paddingLeft, style.paddingRight];
}"""

#: The inspector's padding-box width without any scrollbar: the width fitLabels
#: fits to.
_INSPECTOR_WIDTH_JS = "() => document.getElementById('inspector').clientWidth"


def _assert_refitted_as_a_fresh_draw(page: Any, before: list[str], change: str) -> None:
    """After ``change``, settled, the labels must be cut exactly as a fresh
    draw cuts them. The precondition compares the FRESH draw with ``before``,
    not the refit: the change must alter the fit (or the test tested nothing),
    and that must hold whether or not the observer refitted — so a missing
    refit fails on the refit assertion, not on the precondition."""
    page.evaluate(_TWO_FRAMES_JS)
    refitted = page.evaluate(_LABEL_TEXTS_JS)
    page.evaluate("async () => (await import('/static/js/graph.js')).renderGraph()")
    fresh = page.evaluate(_LABEL_TEXTS_JS)
    assert fresh != before, f"precondition: {change} changed no label's fit"
    assert refitted == fresh, f"after {change}, the labels were not refitted: {refitted}"


def test_labels_are_refitted_when_only_the_inspector_resizes(page: Any) -> None:
    """(15f) The drawing's box need not move when the inspector's does: the
    svg is capped at 24rem, and only its PERCENTAGE gutter makes its content
    box follow the figure on a wide desktop. Pin the drawing — 24rem with a
    fixed 2rem gutter, the cap's own size at 1280 — and narrow the window to
    320: only the inspector moves, and the labels must still be refitted.
    Wide face, so the narrowed fit has cuts to make.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _use_face(page, "wide")
    page.add_style_tag(content=".local-graph svg { width: 24rem; padding-inline: 2rem; }")
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    page.wait_for_selector(".local-graph svg")
    page.evaluate(_TWO_FRAMES_JS)
    svg_box = page.evaluate(_SVG_BOX_JS)
    inspector_width = page.evaluate(_INSPECTOR_WIDTH_JS)
    before = page.evaluate(_LABEL_TEXTS_JS)

    page.set_viewport_size({"width": 320, "height": 800})
    assert page.evaluate(_SVG_BOX_JS) == svg_box, "precondition: the drawing's box moved"
    assert page.evaluate(_INSPECTOR_WIDTH_JS) < inspector_width, (
        "precondition: the inspector did not narrow"
    )
    _assert_refitted_as_a_fresh_draw(page, before, "narrowing the inspector alone")


def test_labels_are_refitted_when_only_the_drawing_resizes(page: Any) -> None:
    """(15g) The converse: the drawing can resize while the inspector does not
    — a cap that changes with the root font size, say. At 400, where the wide
    face's rim labels are cut by the inspector's edge, shrink the drawing to
    12rem and leave the window alone: the edge recedes in viewBox units, and
    the labels must be refitted to it.
    """
    page.set_viewport_size({"width": 400, "height": 800})
    _use_face(page, "wide")
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    page.wait_for_selector(".local-graph svg")
    page.evaluate(_TWO_FRAMES_JS)
    svg_box = page.evaluate(_SVG_BOX_JS)
    inspector_width = page.evaluate(_INSPECTOR_WIDTH_JS)
    before = page.evaluate(_LABEL_TEXTS_JS)

    page.add_style_tag(content=".local-graph svg { max-width: 12rem; }")
    assert page.evaluate(_SVG_BOX_JS) != svg_box, "precondition: the drawing did not resize"
    assert page.evaluate(_INSPECTOR_WIDTH_JS) == inspector_width, (
        "precondition: the inspector resized"
    )
    _assert_refitted_as_a_fresh_draw(page, before, "shrinking the drawing alone")


#: Installed BEFORE the app loads (an init script, applied by ``reboot``):
#: a ResizeObserver that records what it watches, so a test can see the
#: observer's targets. It wraps the browser's class; it changes nothing in
#: graph.js. ONE set, shared by every instance, suffices because only graph.js
#: constructs a ResizeObserver today. Only inspector.js is PINNED to have none
#: (``check_resize_is_not_inert`` asserts on its source); nothing pins the
#: other modules. A second observer that watched anything during 15h would
#: share the set — adding its targets, and clearing graph.js's on its own
#: disconnect — so 15h's exact counts would fail loudly, not pass: the set
#: would then need splitting per instance.
_RECORDING_RESIZE_OBSERVER_JS = """(() => {
    const live = new Set();
    window.__resizeTargets = () => [...live];
    window.ResizeObserver = class extends window.ResizeObserver {
        observe(target, options) { live.add(target); super.observe(target, options); }
        unobserve(target) { live.delete(target); super.unobserve(target); }
        disconnect() { live.clear(); super.disconnect(); }
    };
})();"""

#: [how many targets are watched, is the drawn svg one, is #inspector one,
#: how many watched targets are detached from the document].
_RESIZE_TARGETS_JS = """() => {
    const targets = window.__resizeTargets();
    const svg = document.querySelector('.local-graph svg');
    return [targets.length, svg !== null && targets.includes(svg),
            targets.includes(document.getElementById('inspector')),
            targets.filter((t) => !t.isConnected).length];
}"""


#: A WEAK reference to the drawn svg, which does not itself keep it alive.
_HOLD_WEAKLY_JS = (
    "() => { window.__withdrawnSvg = "
    "new WeakRef(document.querySelector('.local-graph svg')); }"
)


def test_the_refit_observer_watches_only_the_drawn_graph(page: Any) -> None:
    """(15h) Every draw lets go of the previous one: the observer watches the
    drawn svg and #inspector and nothing else — not the svg a redraw replaced,
    which would stay alive, detached, for as long as the page — and once no
    graph is drawn (the editor owns the inspector), it watches nothing.

    ``_open`` draws twice (once when /graph lands, again on its closing
    dispatch), so a draw that did not disconnect leaves a detached svg here.

    And once withdrawn, the svg is COLLECTABLE: graph.js's own reference to
    the drawn svg (``fitted``) is cleared with the disconnect, so a full
    collection — forced through the DevTools protocol — clears a WeakRef to
    it. Kept, it would hold the withdrawn svg until the next draw.
    """
    page.add_init_script(_RECORDING_RESIZE_OBSERVER_JS)
    reboot(page)
    _GRAPH["payload"] = _server_ring_payload()
    _open(page)
    # No wait_for_selector: it returns an ElementHandle, which would keep the
    # svg alive from Playwright's side and void the collection check below.
    # _open has settled the draw, and the target check sees the svg or fails.
    assert page.evaluate(_RESIZE_TARGETS_JS) == [2, True, True, 0], (
        "after a redraw, the observer does not watch exactly the drawn svg and #inspector"
    )
    page.evaluate(_HOLD_WEAKLY_JS)

    _dispatch(page, '{"editing": true}')
    assert page.evaluate("() => document.querySelector('.local-graph')") is None, (
        "precondition: the editor did not take the graph's place"
    )
    assert page.evaluate(_RESIZE_TARGETS_JS) == [0, False, False, 0], (
        "with no graph drawn, the observer still watches something"
    )
    page.context.new_cdp_session(page).send("HeapProfiler.collectGarbage")
    assert page.evaluate("() => window.__withdrawnSvg.deref() === undefined"), (
        "the withdrawn svg survived a full collection: something — graph.js's "
        "`fitted` — still holds it"
    )
