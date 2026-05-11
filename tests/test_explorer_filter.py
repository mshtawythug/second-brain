"""Static smoke tests for the Phase 4.2 Explorer "Show ingested" toggle.

Background
----------

P4.2 of the Wiki UX Overhaul adds a top-level toggle button to the
Explorer that hides the ``_ingested/`` folder (and its descendants)
from the rendered tree by default. State persists in localStorage
under ``brain.explorer.showIngested`` (boolean). Click flips state +
re-renders.

Implementation lives entirely in
``quartz_overrides/quartz/components/scripts/explorer.inline.ts``
(no SSR-side Explorer.tsx override — keeps the upstream component
unforked, same pattern as the existing folder-count badge).

These tests are static-source only — the project doesn't run a JS /
Quartz toolchain in CI. Pattern matches
``tests/test_quartz_search_static.py`` and
``tests/test_quartz_tags_static.py``. End-to-end browser-driven
verification happens via the MCP browser tools as part of the manual
verification gate at commit time.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPLORER_INLINE = (
    REPO_ROOT
    / "src" / "brain" / "quartz_overrides"
    / "quartz"
    / "components"
    / "scripts"
    / "explorer.inline.ts"
)
EXPLORER_SCSS = (
    REPO_ROOT
    / "src" / "brain" / "quartz_overrides"
    / "quartz"
    / "styles"
    / "brain"
    / "_explorer.scss"
)
CUSTOM_SCSS = (
    REPO_ROOT
    / "src" / "brain" / "quartz_overrides"
    / "quartz"
    / "styles"
    / "custom.scss"
)


# ---------------------------------------------------------------------------
# Fixtures — read each source file once per module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def explorer_inline_source() -> str:
    """Read the explorer inline script once per module."""
    assert EXPLORER_INLINE.is_file(), f"missing inline script at {EXPLORER_INLINE}"
    return EXPLORER_INLINE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def explorer_scss_source() -> str:
    """Read the explorer SCSS partial once per module."""
    assert EXPLORER_SCSS.is_file(), f"missing SCSS partial at {EXPLORER_SCSS}"
    return EXPLORER_SCSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def custom_scss_source() -> str:
    """Read the SCSS entry point once per module."""
    assert CUSTOM_SCSS.is_file(), f"missing custom.scss at {CUSTOM_SCSS}"
    return CUSTOM_SCSS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# explorer.inline.ts — toggle wiring contract
# ---------------------------------------------------------------------------


def test_inline_pins_localstorage_key(explorer_inline_source: str) -> None:
    """Persistence key is ``brain.explorer.showIngested`` (constant + literal).

    Pinned both as a named constant (so future code reads from the
    constant rather than re-typing the string) AND as the exact
    literal in source so a typo in either drift would surface here.
    """
    assert (
        'SHOW_INGESTED_KEY = "brain.explorer.showIngested"' in explorer_inline_source
    ), "expected `SHOW_INGESTED_KEY = \"brain.explorer.showIngested\"` constant"
    assert (
        'localStorage.setItem(SHOW_INGESTED_KEY' in explorer_inline_source
    ), "expected `localStorage.setItem(SHOW_INGESTED_KEY, ...)` write"
    assert (
        'localStorage.getItem(SHOW_INGESTED_KEY)' in explorer_inline_source
    ), "expected `localStorage.getItem(SHOW_INGESTED_KEY)` read"


def test_inline_default_state_is_off(explorer_inline_source: str) -> None:
    """``loadShowIngested()`` returns ``false`` when the key is absent.

    Regression guard for the spec: "Default = false (OFF, hides
    `_ingested/`)". The implementation's no-key branch must return
    `false` literally — not throw, not return `null` (which would be
    truthy-coerced upstream and silently flip the default).
    """
    assert "function loadShowIngested(): boolean" in explorer_inline_source, (
        "expected `loadShowIngested()` helper"
    )
    # The early-return path for `localStorage === undefined` and
    # `raw === null` both return `false`. Anchor on the structure: the
    # helper must include `return false` early in its body.
    helper_start = explorer_inline_source.index(
        "function loadShowIngested(): boolean"
    )
    helper_body = explorer_inline_source[helper_start : helper_start + 600]
    assert "return false" in helper_body, (
        "expected `loadShowIngested` to default to `false` when key absent"
    )


def test_inline_filters_ingested_folder_segment(explorer_inline_source: str) -> None:
    """The trie-filter pass drops the ``_ingested`` top-level folder.

    Two anchors:
      1. The path-form constant ``INGESTED_FOLDER_SEGMENT = "_ingested"``
         is the single source of truth.
      2. The filter call uses the constant and is gated on
         ``!loadShowIngested()`` so a true preference leaves the trie
         intact.
    """
    assert (
        'INGESTED_FOLDER_SEGMENT = "_ingested"' in explorer_inline_source
    ), "expected `INGESTED_FOLDER_SEGMENT = \"_ingested\"` constant"
    # Filter predicate uses the constant against `node.slugSegment`.
    assert (
        "node.slugSegment !== INGESTED_FOLDER_SEGMENT" in explorer_inline_source
    ), "expected filter predicate `node.slugSegment !== INGESTED_FOLDER_SEGMENT`"
    # The conditional gate — only filter when toggle is OFF.
    assert "if (!loadShowIngested())" in explorer_inline_source, (
        "expected `if (!loadShowIngested())` gate around the filter"
    )


def test_inline_injects_brain_explorer_ingested_toggle_button(
    explorer_inline_source: str,
) -> None:
    """The toggle button is created with class ``brain-explorer-ingested-toggle``.

    Anchored on the className string and the
    ``ensureIngestedToggle`` helper that owns the injection.
    """
    assert (
        '"brain-explorer-ingested-toggle"' in explorer_inline_source
    ), "expected class `brain-explorer-ingested-toggle` on the toggle button"
    assert "function ensureIngestedToggle(" in explorer_inline_source, (
        "expected `ensureIngestedToggle` helper that owns the injection"
    )


def test_inline_toggle_labels_pinned(explorer_inline_source: str) -> None:
    """The label strings ``Show ingested`` / ``Hide ingested`` are pinned.

    Factored into a constant table so a future i18n pass has a
    single seam, AND so tests / a11y audits can assert against the
    canonical strings.
    """
    assert "SHOW_INGESTED_LABELS" in explorer_inline_source, (
        "expected `SHOW_INGESTED_LABELS` constant for button text"
    )
    assert '"Show ingested"' in explorer_inline_source, (
        "expected literal `Show ingested` label"
    )
    assert '"Hide ingested"' in explorer_inline_source, (
        "expected literal `Hide ingested` label"
    )


def test_inline_button_a11y_aria_pressed(explorer_inline_source: str) -> None:
    """The toggle button reflects state via ``aria-pressed``.

    A toggle button SHOULD use ``aria-pressed`` (per WAI-ARIA's
    "button" role pressed-state pattern) so screen readers announce
    on/off transitions. The state-refresh helper must update this
    attribute every time it runs.
    """
    assert 'setAttribute("aria-pressed"' in explorer_inline_source, (
        "expected `setAttribute(\"aria-pressed\", ...)` on the toggle button"
    )


def test_inline_clears_explorer_ul_before_re_render(
    explorer_inline_source: str,
) -> None:
    """The re-render path clears stale `<li>` children, preserving overflow-end.

    The toggle's re-render fires WITHOUT an SPA nav, so the
    ``explorer-ul`` still carries the previous render's tree
    children. Without a clearing pass the second render would double
    the visible tree. The clear must skip `.overflow-end` because
    `OverflowList.tsx`'s gradient observer relies on that sentinel.
    """
    # Anchor on the loop that walks `explorerUl.children` and removes
    # everything that isn't `.overflow-end`.
    assert (
        'classList.contains("overflow-end")' in explorer_inline_source
    ), "expected the re-render clear to preserve `.overflow-end` sentinel"
    # And the actual `.remove()` call must be present so the loop
    # functions as a clear, not a no-op.
    assert "child.remove()" in explorer_inline_source, (
        "expected `child.remove()` in the re-render clearing loop"
    )


def test_inline_event_propagation_stopped_on_toggle(
    explorer_inline_source: str,
) -> None:
    """The toggle handler calls ``event.stopPropagation()``.

    The toggle button sits inside ``.explorer-content``, which is a
    descendant of the desktop title button (whose click handler
    collapses the entire explorer). Without `stopPropagation`, every
    toggle click would also collapse the explorer — defeating the
    purpose of the chip.
    """
    # Look for `event.stopPropagation()` near the toggle handler.
    helper_idx = explorer_inline_source.index("function ensureIngestedToggle(")
    helper_window = explorer_inline_source[helper_idx : helper_idx + 1500]
    assert "stopPropagation()" in helper_window, (
        "expected `event.stopPropagation()` in the toggle click handler"
    )


def test_inline_toggle_persists_then_renders(explorer_inline_source: str) -> None:
    """The toggle handler order is: read → flip → persist → refresh → render.

    Pinning the call sequence so a future hand edit can't accidentally
    swap the order such that the re-render reads stale state.
    """
    # Anchor on the click body. The click callback in `setupExplorer`
    # passes a closure to `ensureIngestedToggle` that:
    #   const next = !loadShowIngested()
    #   persistShowIngested(next)
    #   refreshIngestedToggleState(button, next)
    #   renderExplorerTree(explorer, opts, currentSlug, data)
    pattern = re.compile(
        r"const next = !loadShowIngested\(\).*?"
        r"persistShowIngested\(next\).*?"
        r"refreshIngestedToggleState\(button, next\).*?"
        r"renderExplorerTree\(",
        re.DOTALL,
    )
    assert pattern.search(explorer_inline_source) is not None, (
        "expected toggle click handler to: read → flip → persist → "
        "refresh → re-render (in that order)"
    )


# ---------------------------------------------------------------------------
# _explorer.scss — class hooks
# ---------------------------------------------------------------------------


def test_scss_declares_toggle_button_selector(
    explorer_scss_source: str,
) -> None:
    """The SCSS partial declares the toggle button selector.

    The inline script applies the class hook; the partial is what
    paints the visible chip. Without this rule the toggle would
    render as a default button with no brand styling.
    """
    assert ".brain-explorer-ingested-toggle" in explorer_scss_source, (
        "expected `.brain-explorer-ingested-toggle` selector in _explorer.scss"
    )


def test_scss_handles_active_state(explorer_scss_source: str) -> None:
    """The active state is keyed off ``[data-active="true"]``.

    Mirrors the chip-state convention used by the P3.2 search chips
    and the graph filter chips — same attribute, same on/off semantics.
    The inline script flips this attribute on toggle.
    """
    assert '[data-active="true"]' in explorer_scss_source, (
        "expected `[data-active=\"true\"]` rule for the active toggle state"
    )


def test_scss_uses_lowercase_chip_text(explorer_scss_source: str) -> None:
    """The toggle pill is NOT uppercased.

    Same P3.4 contract as the tag pills — explicitly pin
    ``text-transform: none`` so a future global default-flip can't
    silently re-uppercase the chip text.
    """
    assert "text-transform: none" in explorer_scss_source, (
        "expected `text-transform: none` on the toggle pill"
    )


def test_custom_scss_imports_explorer_partial(custom_scss_source: str) -> None:
    """The new ``_explorer.scss`` partial is wired from ``custom.scss``.

    Without this `@use` line, the partial sits on disk but never
    reaches the rendered CSS, which would render the toggle as an
    unstyled default button.
    """
    assert "@use \"./brain/explorer\"" in custom_scss_source, (
        "expected `@use \"./brain/explorer\";` line in custom.scss"
    )


# ---------------------------------------------------------------------------
# Cross-file consistency
# ---------------------------------------------------------------------------


def test_class_name_matches_between_inline_and_scss(
    explorer_inline_source: str, explorer_scss_source: str
) -> None:
    """Toggle button class name appears in BOTH the script and the SCSS.

    Drift between the two would render the toggle without styling.
    """
    class_name = "brain-explorer-ingested-toggle"
    assert class_name in explorer_inline_source, (
        f"explorer.inline.ts must reference `{class_name}` (button class)"
    )
    assert class_name in explorer_scss_source, (
        f"_explorer.scss must declare a rule for `{class_name}`"
    )
