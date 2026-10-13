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
    _CUT_LENGTHS_JS,
    _FACES,
    _SPEC_LABEL_CLEARANCE,
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

#: The selector of graph.css's keyboard-focus rule for a node's circle: its
#: ring is the widest stroke a circle is painted with.
_FOCUS_RING_SELECTOR = ".local-graph a.node:focus-visible circle"

#: For every NEIGHBOUR: the bottom of its circle (``cy + r``), the circle's
#: WIDEST painted stroke, the top of its label's box as shown, and the top of
#: that box at the full title. The full title is set on the label, measured,
#: and put back. The widest stroke is the larger of the resting stroke
#: (computed ``stroke-width``) and the focus ring's, which is read from the
#: LOADED stylesheet's rule rather than by focusing each node, because
#: ``:focus-visible`` on a scripted focus depends on the last input. A
#: missing rule is reported as null and fails the test. In viewBox units
#: (``getBBox``), as fitLabels measures.
_HANG_JS = """(focusSelector) => {
    const focusRule = [...document.styleSheets]
        .flatMap((sheet) => [...sheet.cssRules])
        .find((rule) => rule.selectorText === focusSelector);
    const focusRing = focusRule ? parseFloat(focusRule.style.strokeWidth) : null;
    return [...document.querySelectorAll('.local-graph a.node')].map((node) => {
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
        stroke: parseFloat(getComputedStyle(circle).strokeWidth), focusRing,
    };
    });
}"""


def _hang_faults(hangs: list[dict[str, Any]]) -> list[str]:
    """Every neighbour label whose box reaches into its circle's WIDEST
    painted stroke (the focus ring, or the resting stroke if wider), or whose
    box at the full title is not hung exactly ``_SPEC_LABEL_GAP`` below the
    circle."""
    faults = []
    for hang in hangs:
        if hang["focusRing"] is None:
            faults.append(f"no {_FOCUS_RING_SELECTOR!r} rule in the loaded stylesheets")
            continue
        painted = hang["below"] + max(hang["stroke"], hang["focusRing"]) / 2
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
    10, a plain label's box sat 3.2 to 3.5 units INSIDE its circle. Its ascent
    is whole in device pixels, so in units it moves with the width (13.23 /
    13.46 / 13.32 at 320 / 400 / 1280, the same at both faces). Every test
    stayed green: no test looked at a label beside its own node. Each label
    is checked twice. Its box as SHOWN must clear the circle's widest painted
    stroke, the focus ring, which is read from graph.css as loaded and not
    from graph.js. Its box at the FULL title, the box graph.js
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
        hangs = page.evaluate(_HANG_JS, _FOCUS_RING_SELECTOR)
        assert len(hangs) == count, f"precondition: {count} neighbours drew {len(hangs)} labels"
        assert _hang_faults(hangs) == [], (
            f"count {count} at {viewport_width}px: {_hang_faults(hangs)}"
        )
        _assert_optimal_cuts(page, f"count {count} at {viewport_width}px")


#: A title that draws an emoji. The emoji comes from a fallback face whose
#: box is taller than the label face's (measured in the drawn graph, Chromium
#: on macOS: ascent 15.87 / 15.53 / 15.54 units against 13.23 / 13.46 / 13.32
#: for a plain title, at 320 / 400 / 1280px), so a label hung by its baseline
#: at any gap set for the label face would reach higher than a plain one.
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

    hangs = {hang["id"]: hang for hang in page.evaluate(_HANG_JS, _FOCUS_RING_SELECTOR)}
    plain, emoji = hangs[ALPHA_ID], hangs[BRAVO_ID]
    plain_ascent = plain["baseline"] - plain["titleTop"]
    emoji_ascent = emoji["baseline"] - emoji["titleTop"]
    assert emoji_ascent > plain_ascent + _HANG_EPSILON, (
        f"precondition: the emoji's box ({emoji_ascent:.2f}) is no taller than the "
        f"plain one ({plain_ascent:.2f}), so this tests nothing a plain title does not"
    )
    assert _hang_faults([plain, emoji]) == [], _hang_faults([plain, emoji])
    _assert_optimal_cuts(page, "a plain and an emoji title")


# ------------------------------------------------------- grapheme-safe cut --

#: 27 plain characters: whatever is appended is the title's 28th character,
#: the last one the cap keeps.
_PREFIX = "Synthetic Planning Note Six"

#: A ZWJ emoji sequence, one grapheme of seven code points (eleven UTF-16 code
#: units), and an astral letter, one grapheme of one code point (two code
#: units). Each is the title's 28th character in the tests below, so slicing
#: by code unit splits it, and slicing by code point splits the sequence.
_FAMILY = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"
_ASTRAL = "\U0001D538"
_FIRST_CODE_POINT_OF_FAMILY = "\U0001F468"

#: What follows the 28th character, so every title below runs past the cap.
_TAIL = " Extra Words"

#: Each case: the title, and the label it must show in a slot that holds more
#: than the cap. ``native`` is a browser with ``Intl.Segmenter``. ``absent``
#: is one without it (Firefox before 125, say), where graph.js falls back to
#: code points. That fallback keeps an astral letter whole but may split a
#: ZWJ sequence at a joint, never inside a code point (plan §8.3).
_CUT_CASES = {
    "zwj-native": (f"{_PREFIX}{_FAMILY}{_TAIL}", f"{_PREFIX}{_FAMILY}…"),
    "astral-native": (f"{_PREFIX}{_ASTRAL}{_TAIL}", f"{_PREFIX}{_ASTRAL}…"),
    "zwj-absent": (f"{_PREFIX}{_FAMILY}{_TAIL}", f"{_PREFIX}{_FIRST_CODE_POINT_OF_FAMILY}…"),
    "astral-absent": (f"{_PREFIX}{_ASTRAL}{_TAIL}", f"{_PREFIX}{_ASTRAL}…"),
    "exactly-the-cap": (f"{_PREFIX}{_FAMILY}", f"{_PREFIX}{_FAMILY}"),
    "space-at-the-cap": ("Synthetic Planning Notebook Extra Words",
                         "Synthetic Planning Notebook…"),
}

_NO_SEGMENTER_JS = "delete Intl.Segmenter;"

#: A neighbour's edge slot, from the inspector's padding box as the oracle
#: computes it, but with no ``Intl.Segmenter``, so it also runs where the
#: segmenter has been removed. A lone neighbour at 12 o'clock shares no row.
_EDGE_SLOT_JS = """(clearance) => {
    const svg = document.querySelector('.local-graph svg');
    const ctm = svg.getScreenCTM();
    const host = document.getElementById('inspector');
    const edge = host.getBoundingClientRect().left + host.clientLeft;
    const left = (edge - ctm.e) / ctm.a + clearance;
    const right = (edge + host.clientWidth - ctm.e) / ctm.a - clearance;
    const x = Number(document.querySelector('.local-graph a.node text').getAttribute('x'));
    return 2 * Math.min(x - left, right - x);
}"""


def _draw_lone_neighbour(page: Any, title: str, note_id: str) -> None:
    """Draw the server's ring of one neighbour, at 12 o'clock mid-canvas,
    where at 1280 its slot is far wider than any capped title."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _GRAPH["payload"] = _server_ring_payload([(ALPHA_ID, title)])
    _open(page, note_id)
    page.wait_for_selector(".local-graph svg")


@pytest.mark.parametrize("case", sorted(_CUT_CASES))
def test_a_label_is_cut_at_the_cap_by_whole_characters(page: Any, case: str) -> None:
    """(16) The cap counts CHARACTERS as a reader sees them, grapheme
    clusters, and a cut never leaves a space before "…".

    Every title here has its 28th character placed where a careless cut
    goes wrong. A code-unit slice leaves half a surrogate pair. A code-point
    slice leaves a lone 👨 out of a family. A 28-character title that is
    longer in code units gets an ellipsis it does not need. A 28th character
    that is a space is kept before the "…". The slot holds the WHOLE title,
    so the cap alone decides the label (precondition). Where
    ``Intl.Segmenter`` is present the oracle, which states the cut rule
    itself, also holds the label to it.
    """
    title, expected = _CUT_CASES[case]
    if case.endswith("-absent"):
        page.add_init_script(_NO_SEGMENTER_JS)
        page.reload()
        page.wait_for_selector('[role="treeitem"]')
        assert page.evaluate("() => typeof Intl.Segmenter") == "undefined", (
            "precondition: Intl.Segmenter is still present, so the fallback did not run"
        )
    index = sorted(_CUT_CASES).index(case)
    _draw_lone_neighbour(page, title, f"aaaaaaaa-0000-4000-8000-{0xb00 + index:012x}")

    (whole,) = page.evaluate(_CUT_LENGTHS_JS, [ALPHA_ID, [title]])
    slot = page.evaluate(_EDGE_SLOT_JS, _SPEC_LABEL_CLEARANCE)
    assert whole <= slot, (
        f"precondition: the slot ({slot:.2f}) does not hold the whole title ({whole:.2f}), "
        "so the width fit, not the cap, may have cut this label"
    )
    shown = page.evaluate("() => document.querySelector('.local-graph a.node text').textContent")
    assert shown == expected, f"{case}: {title!r} is shown as {shown!r}, not {expected!r}"
    if not case.endswith("-absent"):
        _assert_optimal_cuts(page, case)
