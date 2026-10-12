"""Local-graph LABEL PLACEMENT, in a real browser: where a neighbour's label
hangs below its own circle.

Split from ``tests/test_ui_browser_graph_layout.py``, which is near the
800-line ceiling. That module checks where labels sit relative to the
inspector and to each other. This one checks where each sits relative to its
OWN node (plan §8). The ring, faces and oracle come from
``tests/ui_graph_geometry.py`` and the stub routing from
``tests/ui_graph_harness.py``; neither is copied here.

**graph.js's constants are pinned at every test's TEARDOWN**, by the autouse
``spec_constants_pin`` fixture imported below, as in the layout module.
``LABEL_GAP`` is one of them.

**The filename is load-bearing.** CI names every browser module explicitly
(``.github/workflows/ci.yml``), and ``tests/test_ci_workflow.py`` fails if a
``browser``-marked module is missing from that list. Run it by path:

    pytest tests/test_ui_browser_graph_labels.py -m browser --no-cov
"""
from __future__ import annotations

from typing import Any

import pytest

from tests.ui_graph_geometry import (
    _FACES,
    _SPEC_LABEL_GAP,
    _SPEC_LABEL_MAX_CHARS,
    _SPEC_LABEL_THRESHOLD,
    _assert_optimal_cuts,
    _open_ring,
    _server_ring_payload,
    _spec_constants_pin_fixture,  # noqa: F401 — autouse: pins graph.js's constants after each test
    _use_face,
)
from tests.ui_graph_harness import (
    _GRAPH,
    ALPHA_ID,
    BRAVO_ID,
    _open,
    _page_fixture,  # noqa: F401 — registers the `page` fixture
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`, which `page` uses
)

pytestmark = pytest.mark.browser

#: Below this, two positions in viewBox units are the same number computed twice.
_HANG_EPSILON = 1e-3

#: For every NEIGHBOUR: the bottom of its circle (``cy + r``), the circle's
#: painted stroke (computed ``stroke-width``), the top of its label's box as
#: shown, and the top of that box at the full title. The full title is set on
#: the label, measured, and put back. In viewBox units (``getBBox``), as
#: fitLabels measures.
_HANG_JS = """() => [...document.querySelectorAll('.local-graph a.node')].map((node) => {
    const circle = node.querySelector('circle');
    const text = node.querySelector('text');
    const shown = text.textContent;
    const shownTop = text.getBBox().y;
    text.textContent = node.querySelector('title').textContent;
    const titleTop = text.getBBox().y;
    text.textContent = shown;
    return {
        id: node.getAttribute('data-note-id'), shown, shownTop, titleTop,
        baseline: Number(text.getAttribute('y')),
        below: Number(circle.getAttribute('cy')) + Number(circle.getAttribute('r')),
        stroke: parseFloat(getComputedStyle(circle).strokeWidth),
    };
})"""


def _hang_faults(hangs: list[dict[str, Any]]) -> list[str]:
    """Every neighbour label whose box reaches into its circle's painted
    stroke, or whose box at the full title is not hung exactly
    ``_SPEC_LABEL_GAP`` below the circle."""
    faults = []
    for hang in hangs:
        painted = hang["below"] + hang["stroke"] / 2
        if hang["shownTop"] < painted:
            faults.append(f"{hang['shown']!r}: box top {hang['shownTop']:.2f} is inside its "
                          f"circle, painted to {painted:.2f}")
        gap = hang["titleTop"] - hang["below"]
        if abs(gap - _SPEC_LABEL_GAP) > _HANG_EPSILON:
            faults.append(f"{hang['shown']!r}: box hangs {gap:.3f} below its circle, "
                          f"not {_SPEC_LABEL_GAP}")
    return faults


@pytest.mark.parametrize("face", _FACES)
@pytest.mark.parametrize("viewport_width", [320, 400, 1280])
def test_a_neighbour_label_hangs_its_gap_below_its_own_circle(
    page: Any, viewport_width: int, face: str,
) -> None:
    """(14c) On every ring up to the threshold, each neighbour's label box
    clears its own circle, and starts exactly the spec's gap below it.

    The box, not the baseline. Hung by its baseline, at the old fixed gap of
    10, a label's box sat 3 to 5 units INSIDE its circle (an ascent of 13 to
    14 units, rounded to whole device pixels, so the overlap varies with
    width as well as face), and every test stayed green: no test looked at a
    label beside its own node. Each label is checked twice. Its box as SHOWN
    must clear the circle's painted stroke, which is read from the computed
    style and not from graph.js. Its box at the FULL title, the box graph.js
    hangs by, must start ``_SPEC_LABEL_GAP`` below the circle, to within
    rounding. That catches a gap in either direction. Every title here is at
    most the cap, so the full title is the capped title graph.js measures.
    """
    _use_face(page, face)
    for count in range(1, _SPEC_LABEL_THRESHOLD + 1):
        titles = _open_ring(page, count, viewport_width)
        assert all(len(title) <= _SPEC_LABEL_MAX_CHARS for _, title in titles), (
            "precondition: a title past the cap, so its full title is not what graph.js hangs by"
        )
        hangs = page.evaluate(_HANG_JS)
        assert len(hangs) == count, f"precondition: {count} neighbours drew {len(hangs)} labels"
        assert _hang_faults(hangs) == [], (
            f"count {count} at {viewport_width}px: {_hang_faults(hangs)}"
        )
        _assert_optimal_cuts(page, f"count {count} at {viewport_width}px")


#: A title that draws an emoji. The emoji comes from a fallback face whose
#: box is taller than the label face's (measured: ascent 15 against 13 or
#: 14, in Chromium on macOS), so a label hung by its baseline at any gap set
#: for the label face would reach higher than a plain one.
_EMOJI_TITLE = "Synthetic Launch Note \N{ROCKET}"
_PLAIN_TITLE = "Synthetic Launch Note Plain"


def test_a_label_that_draws_an_emoji_hangs_by_its_own_taller_box(page: Any) -> None:
    """(14d) A label's hang is measured on ITS title. A title with an emoji
    hangs its taller box exactly as far below its circle as a plain title
    does, so its baseline sits LOWER.

    One plain neighbour and one emoji neighbour, at 12 and 6 o'clock on the
    server's ring. A hang measured on anything but the label's own text, such
    as a fixed reference string or the label face alone, puts both baselines
    the same distance below their circles. The emoji's box then reaches above
    the gap. The precondition shows the emoji box really is taller.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _GRAPH["payload"] = _server_ring_payload([(ALPHA_ID, _PLAIN_TITLE), (BRAVO_ID, _EMOJI_TITLE)])
    _open(page, "aaaaaaaa-0000-4000-8000-000000000a01")
    page.wait_for_selector(".local-graph svg")

    hangs = {hang["id"]: hang for hang in page.evaluate(_HANG_JS)}
    plain, emoji = hangs[ALPHA_ID], hangs[BRAVO_ID]
    plain_ascent = plain["baseline"] - plain["titleTop"]
    emoji_ascent = emoji["baseline"] - emoji["titleTop"]
    assert emoji_ascent > plain_ascent + _HANG_EPSILON, (
        f"precondition: the emoji's box ({emoji_ascent:.2f}) is no taller than the "
        f"plain one ({plain_ascent:.2f}), so this tests nothing a plain title does not"
    )
    assert _hang_faults([plain, emoji]) == [], _hang_faults([plain, emoji])
    _assert_optimal_cuts(page, "a plain and an emoji title")
