"""Static smoke tests for the Phase 3.2 custom Search component.

The brain repo's test image does not run a JS toolchain, so we cannot
build the Quartz output and exercise the component end-to-end here.
The closest existing pattern is
``tests/test_quartz_contentindex_draft_filter.py`` (regex-based static
checks against TS source). This file follows the same flavor, scoped
to the P3.2 contract:

- A ``Search.tsx`` override exists under
  ``quartz_overrides/quartz/components/`` and declares the source-icon
  vocabulary the chip rail expects.
- The inline script ships at
  ``quartz_overrides/quartz/components/scripts/search.inline.ts`` and
  references the per-slug body file under ``static/contentBodies/`` for
  lazy fetch, plus the localStorage key ``brain.search.activeSources``
  for chip persistence.
- The chip rail markup carries the right data hooks
  (``data-brain-source-icons`` JSON for icon lookup,
  ``data-brain-source`` per chip for the toggle handler).
- A new SCSS partial ``_search.scss`` exists, declares the expected
  class hooks, and is wired into ``custom.scss`` so the build picks
  it up.
- The Search component is exported via the components barrel
  (``quartz_overrides/quartz/components/index.ts``); the layout file
  binds ``Component.Search()`` so the layout slot picks up the
  override.

Limitations: this file only asserts the SOURCE shape. A full
end-to-end test would invoke ``npx quartz build`` against a fixture
vault and drive the popover with a browser. That needs a JS
toolchain not on the test image — flagged in the P3.2 DONE report.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_DIR = REPO_ROOT / "quartz_overrides" / "quartz" / "components"
SEARCH_TSX = COMPONENTS_DIR / "Search.tsx"
SEARCH_INLINE = COMPONENTS_DIR / "scripts" / "search.inline.ts"
COMPONENTS_INDEX = COMPONENTS_DIR / "index.ts"
STYLES_DIR = REPO_ROOT / "quartz_overrides" / "quartz" / "styles"
SEARCH_SCSS = STYLES_DIR / "brain" / "_search.scss"
CUSTOM_SCSS = STYLES_DIR / "custom.scss"
LAYOUT_TS = REPO_ROOT / "quartz_overrides" / "quartz.layout.ts"

EXPECTED_SOURCES = ("gmail", "krisp", "slack", "manual", "vault")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def search_tsx_source() -> str:
    """Read the Search.tsx override source once per module."""
    assert SEARCH_TSX.is_file(), f"missing component override at {SEARCH_TSX}"
    return SEARCH_TSX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def search_inline_source() -> str:
    """Read the inline search script once per module."""
    assert SEARCH_INLINE.is_file(), f"missing inline script at {SEARCH_INLINE}"
    return SEARCH_INLINE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def components_index_source() -> str:
    """Read the components barrel once per module."""
    assert COMPONENTS_INDEX.is_file(), f"missing barrel at {COMPONENTS_INDEX}"
    return COMPONENTS_INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def search_scss_source() -> str:
    """Read the new search SCSS partial once per module."""
    assert SEARCH_SCSS.is_file(), f"missing SCSS partial at {SEARCH_SCSS}"
    return SEARCH_SCSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def custom_scss_source() -> str:
    """Read the SCSS entry point once per module."""
    assert CUSTOM_SCSS.is_file(), f"missing custom.scss at {CUSTOM_SCSS}"
    return CUSTOM_SCSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def layout_ts_source() -> str:
    """Read the override quartz.layout.ts once per module."""
    assert LAYOUT_TS.is_file(), f"missing layout at {LAYOUT_TS}"
    return LAYOUT_TS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Search.tsx — component shape
# ---------------------------------------------------------------------------


def test_search_tsx_declares_full_source_icon_table(search_tsx_source: str) -> None:
    """The component constant ``SOURCE_ICONS`` lists all 5 keys.

    The chip rail renders icons keyed off this table; missing a source
    means missing a chip on the rail. We verify each key explicitly so
    a future drop (e.g. accidentally removing ``manual``) trips this
    test rather than only surfacing as a missing chip in production.
    """
    assert "const SOURCE_ICONS" in search_tsx_source, (
        "expected SOURCE_ICONS constant to anchor the source-icon table"
    )
    for key in EXPECTED_SOURCES:
        assert f"{key}:" in search_tsx_source, (
            f"SOURCE_ICONS missing key `{key}` — chip rail won't render its glyph"
        )


def test_search_tsx_renders_chip_rail_markup(search_tsx_source: str) -> None:
    """Component renders the ``brain-search-chips`` chip rail with data hooks.

    The inline script reads ``.brain-search-chips`` to wire chip
    handlers and reads ``data-brain-source-icons`` for the icon table.
    Each chip carries ``data-brain-source`` so the toggle handler can
    map clicks back to vocabulary values.
    """
    assert 'class="brain-search-chips"' in search_tsx_source, (
        "expected `.brain-search-chips` rail in the SSR markup"
    )
    assert "data-brain-source-icons" in search_tsx_source, (
        "expected `data-brain-source-icons` JSON attribute on the chip rail"
    )
    assert 'data-brain-source="__all__"' in search_tsx_source, (
        "expected an `All` pseudo-chip with `data-brain-source=__all__`"
    )
    for key in EXPECTED_SOURCES:
        assert "data-brain-source={value}" in search_tsx_source or (
            f'"{key}"' in search_tsx_source
        ), f"expected chip for source `{key}` in chip rail"


def test_search_tsx_uses_afterDOMLoaded_hook(search_tsx_source: str) -> None:
    """Component publishes the inline script via ``Search.afterDOMLoaded``.

    The Quartz pipeline injects ``afterDOMLoaded`` strings at
    ``</body>`` time. Without this binding the chip handlers, lazy
    fetch, and result-row rendering never run.
    """
    assert "Search.afterDOMLoaded" in search_tsx_source, (
        "expected `Search.afterDOMLoaded = script` binding"
    )
    # And the script must be imported from the per-component scripts dir.
    assert 'from "./scripts/search.inline"' in search_tsx_source, (
        "expected `./scripts/search.inline` import"
    )


def test_search_tsx_publishes_source_icons_as_json_attribute(
    search_tsx_source: str,
) -> None:
    """The icon table is JSON-stringified for the chip rail's data attribute.

    The inline script parses this JSON to avoid a duplicate hard-coded
    icon table. Asserting on the call (rather than a literal JSON
    string) keeps the test stable under whitespace/format edits.
    """
    assert "JSON.stringify(SOURCE_ICONS)" in search_tsx_source, (
        "expected icon table to be serialized via JSON.stringify(SOURCE_ICONS)"
    )


# ---------------------------------------------------------------------------
# search.inline.ts — runtime contract
# ---------------------------------------------------------------------------


def test_search_inline_lazy_fetches_content_bodies(search_inline_source: str) -> None:
    """The inline script lazy-fetches per-slug body files from contentBodies/.

    P3.1's emitter writes ``static/contentBodies/<slug>.json``; the
    Search component (P3.2) is the consumer. The contract is anchored
    on (a) the relative directory constant and (b) a literal call to
    ``fetch(`` inside the lazy-fetch helper.
    """
    assert (
        'CONTENT_BODIES_RELDIR = "static/contentBodies"' in search_inline_source
    ), "expected CONTENT_BODIES_RELDIR pin matching the P3.1 emitter path"
    assert "await fetch(url)" in search_inline_source, (
        "expected `await fetch(url)` call in the lazy-body helper"
    )
    assert "fetchBody" in search_inline_source, (
        "expected `fetchBody` helper that owns the lazy body fetch"
    )


def test_search_inline_persists_active_sources_in_localstorage(
    search_inline_source: str,
) -> None:
    """The chip filter set persists in localStorage under the contracted key.

    Without persistence, every page navigation would reset the filter
    to the default (everything visible) — defeating the chip rail's
    UX purpose. Anchoring on the literal storage key plus the
    ``localStorage.setItem`` call.
    """
    assert (
        'ACTIVE_SOURCES_KEY = "brain.search.activeSources"' in search_inline_source
    ), "expected `brain.search.activeSources` key constant"
    assert (
        "localStorage.setItem(ACTIVE_SOURCES_KEY" in search_inline_source
    ), "expected `localStorage.setItem(ACTIVE_SOURCES_KEY, ...)` write"
    assert (
        "localStorage.getItem(ACTIVE_SOURCES_KEY)" in search_inline_source
    ), "expected `localStorage.getItem(ACTIVE_SOURCES_KEY)` read"


def test_search_inline_emits_mark_in_snippet_highlight(
    search_inline_source: str,
) -> None:
    """The snippet highlighter wraps matches in ``<mark>``.

    The plan spec calls for ``<mark>`` (not the upstream
    ``<span class="highlight">``) on result rows so the brain accent
    rule in `_search.scss` paints per-result highlights. The preview
    pane separately uses `<span class="highlight">` for its in-DOM
    highlighter, so we tolerate both — but the snippet highlight
    column must produce `<mark>` tags.
    """
    assert "<mark>" in search_inline_source, (
        "expected `<mark>` tag in the snippet highlight helper"
    )


def test_search_inline_filters_results_by_active_sources(
    search_inline_source: str,
) -> None:
    """Results are filtered by chip selection before display.

    Anchoring on the helper name + activeSources reference. Without
    this filter the chips would be visual-only.
    """
    assert "passesChipFilter" in search_inline_source, (
        "expected `passesChipFilter` helper"
    )
    assert "activeSources" in search_inline_source, (
        "expected `activeSources` set referenced in the filter path"
    )


def test_search_inline_falls_back_to_snippet_on_fetch_failure(
    search_inline_source: str,
) -> None:
    """Preview pane falls back to ``details.snippet`` when the body fetch fails.

    The plan calls out a "snippet ?? content" fallback so the preview
    pane is never empty just because contentBodies/ wasn't deployed.
    Anchored on the brain-search-preview-fallback class the SCSS
    styles distinctly.
    """
    assert "brain-search-preview-fallback" in search_inline_source, (
        "expected fallback class hook surfaced when the lazy fetch fails"
    )


# ---------------------------------------------------------------------------
# Wiring — components barrel + layout file
# ---------------------------------------------------------------------------


def test_components_barrel_exports_search(components_index_source: str) -> None:
    """The components barrel imports + exports the Search override.

    The barrel is what `Component.Search()` in `quartz.layout.ts`
    resolves against; without the import the layout falls back to
    upstream's stock Search component.
    """
    assert 'import Search from "./Search"' in components_index_source, (
        "expected Search import in the components barrel"
    )
    # Also exported in the named-exports block.
    assert "Search," in components_index_source or "Search\n" in components_index_source, (
        "expected `Search` to appear in the named exports list"
    )


def test_layout_wires_search_component(layout_ts_source: str) -> None:
    """The layout binds ``Component.Search()`` so the override renders.

    The override flows through `Component.*` because the components
    barrel re-exports it under the same name. The layout file just
    needs to call `Component.Search()` somewhere; our default layout
    uses it inside the left-sidebar `Flex`.
    """
    assert "Component.Search()" in layout_ts_source, (
        "expected `Component.Search()` to be wired into the layout"
    )


# ---------------------------------------------------------------------------
# SCSS — partial exists + class hooks declared + import wired
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector",
    [
        ".brain-search-chips",
        ".brain-search-chip",
        ".brain-search-row",
        ".brain-search-icon",
        ".brain-search-title",
        ".brain-search-date",
        ".brain-search-snippet",
        ".brain-search-preview",
    ],
)
def test_search_scss_declares_expected_classes(
    search_scss_source: str, selector: str
) -> None:
    """Each expected class hook appears in the SCSS partial."""
    assert selector in search_scss_source, (
        f"expected selector `{selector}` declared in _search.scss"
    )


def test_search_scss_handles_active_chip_state(search_scss_source: str) -> None:
    """The chip's active state is keyed off ``[data-active="true"]``.

    The inline script flips this attribute. Without the rule, active
    chips would be visually indistinguishable from inactive ones.
    """
    assert '[data-active="true"]' in search_scss_source, (
        "expected `[data-active=\"true\"]` rule for active chips"
    )


def test_custom_scss_imports_search_partial(custom_scss_source: str) -> None:
    """The new ``_search.scss`` partial is imported from the SCSS entry point.

    Without this `@use` line, the partial sits on disk but never
    reaches the rendered CSS, which would render the chip rail
    unstyled.
    """
    assert "@use \"./brain/search\"" in custom_scss_source, (
        "expected `@use \"./brain/search\";` line in custom.scss"
    )


# ---------------------------------------------------------------------------
# Cross-file consistency
# ---------------------------------------------------------------------------


def test_source_icons_match_between_component_and_inline(
    search_tsx_source: str, search_inline_source: str
) -> None:
    """The 5 source keys appear in BOTH the component and the inline script.

    The inline script's `FALLBACK_SOURCE_ICONS` is the parse-failure
    safety net for the SSR'd JSON attribute. Both tables must list
    every source so a fall-through render doesn't drop a chip glyph.
    """
    for key in EXPECTED_SOURCES:
        assert f"{key}:" in search_tsx_source, (
            f"SOURCE_ICONS in Search.tsx missing `{key}`"
        )
        assert f"{key}:" in search_inline_source, (
            f"FALLBACK_SOURCE_ICONS in search.inline.ts missing `{key}`"
        )


def test_source_icons_json_in_component_parses(search_tsx_source: str) -> None:
    """The literal SOURCE_ICONS object in Search.tsx is parseable as JSON-ish.

    Static parse-and-shape check — extracts the object literal between
    the first ``= {`` and the matching closing brace, then tries to
    coerce it into a JSON string by quoting bare keys. The point isn't
    full TS parsing — it's catching a typo'd key/value pair early.
    """
    marker = "const SOURCE_ICONS: Record<string, string> = {"
    start = search_tsx_source.index(marker)
    body_start = start + len(marker)
    # Match braces to find the closing one.
    depth = 1
    i = body_start
    while i < len(search_tsx_source) and depth > 0:
        ch = search_tsx_source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    body = search_tsx_source[body_start : i - 1]
    # Quote bare keys. Drop trailing commas before parsing.
    import re

    quoted = re.sub(r"(\w+):", r'"\1":', body).strip()
    if quoted.endswith(","):
        quoted = quoted[:-1]
    json_text = "{" + quoted + "}"
    parsed = json.loads(json_text)
    assert set(parsed.keys()) == set(EXPECTED_SOURCES), (
        f"SOURCE_ICONS keys {set(parsed.keys())} != expected {set(EXPECTED_SOURCES)}"
    )
    for v in parsed.values():
        assert isinstance(v, str) and len(v) > 0, "icon glyph must be a non-empty string"
