"""Static regression tests for Brain's Quartz graph label placement.

The graph renderer is a vendored Quartz inline script copied into the
user's Quartz workspace by the overlay step. The repo does not compile
that TypeScript directly, so these tests follow the same static-source
pattern as ``tests/test_graph_chip_toggle.py`` while also mirroring the
small placement calculation in Python.

Contract:

- Labels must not use the old above-node anchor (`anchor.y = 1.2`).
- Placement must be explicit in a helper, not an incidental anchor side effect.
- The animation loop must call that helper with the node center.
- Small/non-hub labels sit under the circle; large hub labels can center in it
  only when their text bounds fit inside the circle diameter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_INLINE = (
    REPO_ROOT
    / "src" / "brain" / "quartz_overrides"
    / "quartz"
    / "components"
    / "scripts"
    / "graph.inline.ts"
)

LABEL_NODE_GAP = 3


@pytest.fixture(scope="module")
def graph_inline_source() -> str:
    """Read graph.inline.ts once per module."""
    assert GRAPH_INLINE.is_file(), f"missing inline script at {GRAPH_INLINE}"
    return GRAPH_INLINE.read_text(encoding="utf-8")


def test_label_anchor_is_not_above_node(graph_inline_source: str) -> None:
    """Labels should not be anchored above the node center anymore."""
    assert "anchor: { x: 0.5, y: 1.2 }" not in graph_inline_source, (
        "old anchor placed labels above the circle instead of on or under it"
    )
    assert "anchor: { x: 0.5, y: 0.5 }" in graph_inline_source, (
        "expected neutral centered anchor at label creation"
    )


def test_label_placement_helper_is_explicit(graph_inline_source: str) -> None:
    """The renderer owns label placement in one helper."""
    assert "const LABEL_NODE_GAP = 3" in graph_inline_source
    pattern = re.compile(
        r"function\s+placeNodeLabel\(\s*"
        r"label:\s*Text,\s*"
        r"node:\s*NodeRenderData,\s*"
        r"x:\s*number,\s*"
        r"y:\s*number,\s*"
        r"\)",
        re.MULTILINE,
    )
    assert pattern.search(graph_inline_source) is not None, (
        "expected a `placeNodeLabel(label, node, x, y)` helper"
    )
    assert "label.anchor.set(0.5, 0.5)" in graph_inline_source, (
        "large hub labels should be center-anchored inside the circle"
    )
    assert "label.anchor.set(0.5, 0)" in graph_inline_source, (
        "small labels should be top-anchored below the circle"
    )
    assert "labelSize.height <= diameter" in graph_inline_source
    assert "labelSize.width <= diameter" in graph_inline_source


def test_animation_loop_uses_label_placement_helper(graph_inline_source: str) -> None:
    """The per-frame position update must call the helper."""
    pattern = re.compile(
        r"placeNodeLabel\(\s*"
        r"n\.label,\s*"
        r"n,\s*"
        r"x\s*\+\s*width\s*/\s*2,\s*"
        r"y\s*\+\s*height\s*/\s*2\s*"
        r"\)",
        re.MULTILINE,
    )
    assert pattern.search(graph_inline_source) is not None, (
        "expected animate() to place labels through placeNodeLabel"
    )
    assert (
        "n.label.position.set(x + width / 2, y + height / 2)"
        not in graph_inline_source
    ), "raw node-center label placement regresses the bug"


@dataclass(frozen=True)
class _PlacedLabel:
    anchor_y: float
    offset_y: float


def _place_label(
    *,
    radius: float,
    label_width: float,
    label_height: float,
    is_hub: bool,
) -> _PlacedLabel:
    """Parity model of ``placeNodeLabel`` for placement edge cases."""
    diameter = radius * 2
    if is_hub and label_height <= diameter and label_width <= diameter:
        return _PlacedLabel(anchor_y=0.5, offset_y=0)
    return _PlacedLabel(anchor_y=0, offset_y=radius + LABEL_NODE_GAP)


def test_small_label_sits_under_circle() -> None:
    """Leaf labels belong under the circle, not floating above or centered in it."""
    placed = _place_label(radius=8, label_width=12, label_height=9, is_hub=False)
    assert placed.anchor_y == 0
    assert placed.offset_y == 11


def test_large_hub_label_centers_inside_circle() -> None:
    """A large hub circle can carry a label centered inside the mark."""
    placed = _place_label(radius=18, label_width=30, label_height=15, is_hub=True)
    assert placed.anchor_y == 0.5
    assert placed.offset_y == 0


def test_over_wide_hub_label_falls_under_circle() -> None:
    """A long hub title should not be centered through a too-small circle."""
    placed = _place_label(radius=18, label_width=80, label_height=15, is_hub=True)
    assert placed.anchor_y == 0
    assert placed.offset_y == 21


def test_expanded_hub_label_falls_under_circle_when_too_tall() -> None:
    """Hover-expanded text should fall under the node if it no longer fits."""
    placed = _place_label(radius=8, label_width=14, label_height=19, is_hub=True)
    assert placed.anchor_y == 0
    assert placed.offset_y == 11
