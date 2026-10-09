"""Shared label-geometry helpers for the local-graph browser suites.

Imported by ``tests/test_ui_browser_graph_layout.py`` (label geometry on the
server's ring) and ``tests/test_ui_browser_graph_refit.py`` (the refit when a
box resizes, and what its ResizeObserver watches). Split out of the first so
each stays under the 800-line ceiling; ONE copy of the ring, the faces and the
measurements, so the two cannot drift apart.

**Deliberately not a ``test_*`` module and deliberately unmarked**, like
``tests/ui_graph_harness.py``: it holds no tests, so pytest never collects it,
and it carries no ``browser`` marker, so ``tests/test_ci_workflow.py`` does not
ask CI to name it. Every module that imports it applies
``pytestmark = pytest.mark.browser`` itself.

**Label clearance is asserted against the HALO** (``_halo_shortfalls``, from
graph.css's computed ``stroke-width``), never against graph.js's
``LABEL_CLEARANCE``, so mutating the clearance cannot lower the bar with it.
"""
from __future__ import annotations

import math
import re
from typing import Any

from brain.ui.app import static_dir
from tests.ui_graph_harness import ALPHA_ID, BRAVO_ID, CHARLIE_ID, ROOT_ID, ROOT_TITLE

DELTA_ID = "aaaaaaaa-0000-4000-8000-00000000000f"

#: The SERVER's ring, not a hand-placed one. Same formula and constants as
#: ``brain.ui.graph_layout`` (``RING_RADIUS`` = 110.0, ``DEFAULT_SIZE`` = 320,
#: neighbour ``i`` of ``count`` at ``theta = 2*pi*i/count``, starting at 12
#: o'clock and running clockwise: ``x = c + R*sin(theta)``, ``y = c - R*cos(theta)``).
#: An earlier version of the layout tests used a hand-made ring with its outer nodes
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


#: How many drawn labels the width fit cut. Every title the ring helpers here
#: draw is exactly 28 characters — at the character cap, never over it — so
#: any ellipsis on a label is the width fit's work.
_CUT_LABELS_JS = """() => [...document.querySelectorAll('.local-graph text')]
    .filter((t) => t.textContent.endsWith('…')).length"""


def _assert_the_face_bit(page: Any, face: str) -> None:
    """The ``wide`` face must have forced at least one cut, or its run proved
    nothing the ``platform`` run did not. Asserted LAST, after the geometry,
    so a missing fit fails on overlapping or clipped labels — what a reader
    would see — and not here.

    At 1280 the rim labels never reach the inspector's edge, so the cut the
    rim test's 1280 ``wide`` run counts comes from the ROW bound (the 3 and 9
    o'clock labels share a row). With the fit removed, that run still fails on
    geometry, not here: its halo check (``_halo_shortfalls``) finds the two
    rim labels overlapping."""
    if face == "wide":
        assert page.evaluate(_CUT_LABELS_JS) > 0, (
            "precondition: the wide face cut no label, so this run tested nothing new"
        )

#: Every label's ADVANCE extent in viewBox units — ``x`` plus or minus half
#: its ``getComputedTextLength()``, the same length fitText measures, so the
#: comparisons below are exact and never hang on glyph ink — with its row
#: (``getBBox``), whether it is visible, and the halo graph.css strokes round
#: it (its computed ``stroke-width``). ``left``/``right`` are the inspector's
#: padding box — what ``overflow-y: auto`` clips to — in the same units.
_EXTENTS_JS = """() => {
    const svg = document.querySelector('.local-graph svg');
    const ctm = svg.getScreenCTM();
    const host = document.getElementById('inspector');
    const edge = host.getBoundingClientRect().left + host.clientLeft;
    return {
        left: (edge - ctm.e) / ctm.a,
        right: (edge + host.clientWidth - ctm.e) / ctm.a,
        labels: [...svg.querySelectorAll('.node > text')].map((t) => {
            const x = Number(t.getAttribute('x'));
            const half = t.getComputedTextLength() / 2;
            const box = t.getBBox();
            return {
                text: t.textContent, lo: x - half, hi: x + half,
                top: box.y, bottom: box.y + box.height,
                visible: getComputedStyle(t).visibility === 'visible',
                halo: parseFloat(getComputedStyle(t).strokeWidth),
            };
        }),
    };
}"""


def _halo_shortfalls(extents: dict[str, Any]) -> list[str]:
    """Every label closer to the inspector's padding edge, or to a VISIBLE
    label sharing its row, than the halo graph.css strokes round it.

    fitLabels keeps ``LABEL_CLEARANCE`` (4) on both, and the clearance exists
    to cover that halo (3), so this is exact: it never fails a correct fit on
    any font. Read from the stylesheet, not from graph.js, so mutating the
    clearance cannot move the bar with it. Rows are compared on the boxes'
    vertical spans, strictly: a pair that overlaps vertically at all is one
    fitLabels treats as sharing a row, at any clearance."""
    left, right, labels = extents["left"], extents["right"], extents["labels"]
    short = [f"{lab['text']!r}: {min(lab['lo'] - left, right - lab['hi']):.2f} from the edge"
             for lab in labels if min(lab["lo"] - left, right - lab["hi"]) < lab["halo"]]
    shown = [lab for lab in labels if lab["visible"]]
    for i, a in enumerate(shown):
        for b in shown[i + 1:]:
            if a["top"] < b["bottom"] and b["top"] < a["bottom"]:
                gap = max(a["lo"], b["lo"]) - min(a["hi"], b["hi"])
                if gap < max(a["halo"], b["halo"]):
                    short.append(f"{a['text']!r} and {b['text']!r}: {gap:.2f} apart")
    return short


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
def _graph_js_constant(name: str) -> int | None:
    """An integer ``const`` read from js/graph.js, or None if it is absent.

    A copy here would let the test and the module disagree silently; parsing
    the constant keeps the test aimed at whatever the module actually ships.
    """
    source = (static_dir() / "js" / "graph.js").read_text(encoding="utf-8")
    found = re.search(rf"const {name} = (\d+);", source)
    return int(found.group(1)) if found else None


def _label_threshold() -> int:
    """``MAX_LABELLED_NEIGHBOURS`` — one number, one place. Absent (the module
    predates the threshold) reads as 24, the server's cap, i.e. "always label"
    — which is exactly the behaviour being replaced."""
    found = _graph_js_constant("MAX_LABELLED_NEIGHBOURS")
    return 24 if found is None else found


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


#: Every drawn label's text, in drawing order.
_LABEL_TEXTS_JS = (
    "() => [...document.querySelectorAll('.local-graph text')].map((t) => t.textContent)"
)

#: The observer's callback runs in the frame after layout; two frames settle it.
#: Settle BEFORE a resize as well as after: observe() queues a first callback
#: of its own, and a resize made while it is pending is refitted by it, so a
#: test that does not settle first passes with no resize observation at all.
_TWO_FRAMES_JS = (
    "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
)


