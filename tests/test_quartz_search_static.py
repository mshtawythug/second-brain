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

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS_DIR = REPO_ROOT / "src" / "brain" / "quartz_overrides" / "quartz" / "components"
SEARCH_TSX = COMPONENTS_DIR / "Search.tsx"
SEARCH_INLINE = COMPONENTS_DIR / "scripts" / "search.inline.ts"
COMPONENTS_INDEX = COMPONENTS_DIR / "index.ts"
STYLES_DIR = REPO_ROOT / "src" / "brain" / "quartz_overrides" / "quartz" / "styles"
SEARCH_SCSS = STYLES_DIR / "brain" / "_search.scss"
CUSTOM_SCSS = STYLES_DIR / "custom.scss"
LAYOUT_TS = REPO_ROOT / "src" / "brain" / "quartz_overrides" / "quartz.layout.ts"

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


def test_search_tsx_imports_shared_source_icons(search_tsx_source: str) -> None:
    """``Search.tsx`` imports ``SOURCE_ICONS`` + ``SOURCE_CHIP_ORDER`` from the shared util.

    P3.6 fix-4 consolidated the source-icon table: previously this
    file declared its own ``const SOURCE_ICONS = {...}`` duplicated
    against ``util/sourceIcons.ts`` and ``search.inline.ts``. The
    canonical table now lives in the util; the component imports it.
    Anchored on the literal import line so a future revert (re-
    declaring the table inline) trips the test.
    """
    assert (
        'from "../util/sourceIcons"' in search_tsx_source
    ), "expected import from `../util/sourceIcons`"
    assert "SOURCE_ICONS" in search_tsx_source, (
        "expected `SOURCE_ICONS` symbol referenced (imported from util)"
    )
    assert "SOURCE_CHIP_ORDER" in search_tsx_source, (
        "expected `SOURCE_CHIP_ORDER` symbol referenced (imported from util)"
    )


def test_search_tsx_does_not_redeclare_source_icons(search_tsx_source: str) -> None:
    """``Search.tsx`` no longer declares a local ``SOURCE_ICONS`` constant.

    P3.6 fix-4: the component-local ``const SOURCE_ICONS: Record<...>``
    was deleted in favour of the shared util import. Re-introducing it
    would resurrect the duplication the fix closed.
    """
    assert (
        "const SOURCE_ICONS: Record<string, string> = {" not in search_tsx_source
    ), "Search.tsx must NOT redeclare a local SOURCE_ICONS constant — import from util"


def test_search_tsx_renders_chip_rail_markup(search_tsx_source: str) -> None:
    """Component renders the ``brain-search-chips`` chip rail with data hooks.

    The inline script reads ``.brain-search-chips`` to wire chip
    handlers and reads ``data-brain-source-icons`` for the icon table.
    Each chip carries ``data-brain-source`` so the toggle handler can
    map clicks back to vocabulary values.

    Per-key chip presence is enforced by
    ``test_source_icons_util_exports_full_vocab`` in
    ``test_quartz_tag_content_static.py`` — the chip values are
    derived from ``SOURCE_CHIP_ORDER`` so any drop in the shared util
    surfaces there.
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
    # The chip values come from the imported `SOURCE_CHIP_ORDER`;
    # a per-chip JSX uses `data-brain-source={value}` driven by the map.
    assert "data-brain-source={value}" in search_tsx_source, (
        "expected `data-brain-source={value}` interpolation across the chip map"
    )


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


def test_search_scss_hides_container_until_active(search_scss_source: str) -> None:
    """The SSR'd search popover container is hidden until JS marks it active.

    The Search override does not ship Quartz's stock component CSS, so
    the brain search partial must preserve the upstream closed-state
    contract itself. Without ``display: none`` on ``.search-container``,
    the SSR'd chips/input/results render inline in the left sidebar.
    """
    assert ".search > .search-container {" in search_scss_source, (
        "expected _search.scss to declare the base search-container shell"
    )
    assert "display: none;" in search_scss_source, (
        "expected .search-container to be hidden by default"
    )
    assert ".search > .search-container.active" in search_scss_source, (
        "expected active selector to reopen the search-container"
    )


def test_search_scss_pins_search_bar_layout(search_scss_source: str) -> None:
    """The popover input has explicit width + background rules.

    The brain ``Search.tsx`` override drops the upstream
    ``Search.css = style`` binding (the stock component shipped its CSS
    via that property; our override only ships ``afterDOMLoaded``), so
    the ``& > .search-space > input { box-sizing: border-box; width:
    100%; padding: 0.5em 1em; ... }`` rule from
    ``quartz/components/styles/search.scss`` never loads. Without that
    rule, the input collapses to the UA-default ~177px width (visible
    gap on the right of the chip rail + result rows) and reads as
    transparent against the darkened popover scrim. ``_sidebar.scss``
    paints the input's background+border for theme parity, but never
    the layout primitives. This test pins both the layout primitives
    AND a defense-in-depth background restate inside ``_search.scss``
    so a future ``_sidebar.scss`` refactor cannot reintroduce the
    regression silently.

    User-visible regression report:
    ``~/Desktop/Screenshot 2026-05-04 at 9.31.42 PM.png``.
    """
    # Selector pins the rule scope so a refactor that drops the
    # `.search-space > .search-bar` chain trips the test.
    assert (
        ".search > .search-container > .search-space > .search-bar" in search_scss_source
    ), "expected explicit `.search-bar` rule scoped under `.search-space`"
    # The three layout primitives the upstream rule provided.
    assert "width: 100%" in search_scss_source, (
        "expected `width: 100%` on the search input (stops UA-default ~177px collapse)"
    )
    assert "box-sizing: border-box" in search_scss_source, (
        "expected `box-sizing: border-box` on the search input"
    )
    # Background restate keeps the regression locked even if `_sidebar.scss`
    # changes its `.search-space > input` rule.
    assert "background: var(--surface-1, var(--light))" in search_scss_source, (
        "expected explicit background restate on the search input "
        "(defense-in-depth against `_sidebar.scss` refactors)"
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


def test_source_icons_canonical_in_shared_util(search_inline_source: str) -> None:
    """The shared util is the single source of truth for source icons (P3.6 fix-4).

    Both the SSR component (``Search.tsx``) and the inline script
    (``search.inline.ts``) now import ``SOURCE_ICONS`` from
    ``util/sourceIcons.ts``. Verify that the inline script imports
    from the shared util AND that it carries the minimum-viable
    fallback for the parse-failure path (a single `vault` entry).
    """
    assert (
        'from "../../util/sourceIcons"' in search_inline_source
    ), "expected inline script to import SOURCE_ICONS from `../../util/sourceIcons`"
    assert "SOURCE_ICONS" in search_inline_source, (
        "expected SOURCE_ICONS symbol referenced in the inline script"
    )
    # The inline script keeps a tiny `vault`-only fallback for the
    # "data attribute missing" branch — assert it exists so a future
    # refactor doesn't accidentally leave the function returning {}.
    assert "FALLBACK_SOURCE_ICONS" in search_inline_source, (
        "expected `FALLBACK_SOURCE_ICONS` parse-failure fallback (single key)"
    )
    assert 'vault: SOURCE_ICONS["vault"]' in search_inline_source, (
        "expected single-key fallback `vault: SOURCE_ICONS[\"vault\"]`"
    )


def test_inline_does_not_redeclare_inferSource(search_inline_source: str) -> None:
    """The inline script no longer carries its own ``inferSource`` implementation.

    P3.6 fix-4: ``inferSource`` lives in ``util/sourceIcons.ts``; the
    inline script aliases the imported helper under the prior local
    name. Re-introducing a `function inferSource(...)` declaration
    would resurrect the duplication.
    """
    assert (
        "function inferSource(slug: string, source: string | undefined)"
        not in search_inline_source
    ), (
        "inline script must not redeclare `inferSource` — alias the "
        "import from `util/sourceIcons` instead"
    )


# ---------------------------------------------------------------------------
# P3.3 cross-check — Search inline still consumes the date column
# ---------------------------------------------------------------------------


def test_search_inline_reads_entry_date(search_inline_source: str) -> None:
    """Inline script reads ``entry.date`` from the loaded contentIndex.

    P3.3 Part B added a ``date`` field to ``contentIndex.json`` entries.
    Smoke-check that Search.tsx's inline runtime still references it
    when building the per-row date label — without this field the
    column shows blank for every row.
    """
    assert "entry.date" in search_inline_source, (
        "search.inline.ts must read `entry.date` from the loaded contentIndex"
    )


def test_search_inline_renders_date_column(search_inline_source: str) -> None:
    """The per-row markup carries a ``brain-search-date`` slot for the date label.

    Anchors the markup hook the SCSS partial styles. Without the slot
    the date label has nowhere to render and the column collapses.
    """
    assert "brain-search-date" in search_inline_source, (
        "search.inline.ts must render the `brain-search-date` slot"
    )


# ---------------------------------------------------------------------------
# P3.6 fix-2 — XSS hardening on result-row title/snippet
# ---------------------------------------------------------------------------


def test_highlight_escapes_body_text_before_substitution(
    search_inline_source: str,
) -> None:
    """``highlight()`` escapes the body text BEFORE the regex substitution loop.

    Previously the helper interpolated raw `tok` into innerHTML, so a
    title like `Re: <script>alert(1)</script>` would execute. The fix
    runs every token through `escapeHtml(...)` first, then wraps
    matches in `<mark>` against the escaped string. Anchored on the
    literal ``escapeHtml(tok)`` call inside the slice loop AND the
    helper signature remaining intact.
    """
    assert "function highlight(searchTerm: string, text: string" in search_inline_source, (
        "expected `highlight()` helper signature unchanged"
    )
    assert "const escapedTok = escapeHtml(tok)" in search_inline_source, (
        "expected `escapeHtml(tok)` to run before regex substitution"
    )
    assert "escapedTok.replace(regex, `<mark>$&</mark>`)" in search_inline_source, (
        "expected substitution to operate on the escaped token (not raw)"
    )


def test_highlight_escapes_query_token_before_compiling_regex(
    search_inline_source: str,
) -> None:
    """Query tokens are escaped BEFORE compilation into a `RegExp`.

    Without `escapeRegExp`, a query like `(.*)` would compile as a
    capturing group; without `escapeHtml`, a query containing `<`
    would not match the escaped body. The fix combines both so the
    regex matches the escaped form of the token.
    """
    assert "function escapeRegExp" in search_inline_source, (
        "expected `escapeRegExp` helper for regex-metachar escaping"
    )
    assert "escapeHtml(escapeRegExp(searchTok.toLowerCase()))" in search_inline_source, (
        "expected query token escaped via `escapeHtml(escapeRegExp(...))`"
    )


def test_format_for_display_escapes_tag_search_title(
    search_inline_source: str,
) -> None:
    """The tag-search branch of `formatForDisplay` escapes the title.

    The basic-search branch flows through `highlight()` (which now
    escapes). The tag-search branch previously passed the raw title
    straight to innerHTML — closing that hole here.
    """
    assert 'escapeHtml(entry.title ?? "")' in search_inline_source, (
        'expected `escapeHtml(entry.title ?? "")` in the tag-search title branch'
    )


def test_search_inline_protection_inventory_comment_present(
    search_inline_source: str,
) -> None:
    """The escapeHtml comment block is now an accurate protection inventory.

    Previously the comment claimed protection in places the code
    didn't deliver — a misleading invariant. The fix updates the
    block to enumerate every innerHTML interpolation and the helper
    that escapes it.
    """
    assert "protection inventory" in search_inline_source, (
        "expected an explicit `protection inventory` comment block "
        "documenting every innerHTML interpolation"
    )


# ---------------------------------------------------------------------------
# P3.6 fix-3 — Slug allowlist on the fetch side
# ---------------------------------------------------------------------------


def test_search_inline_pins_safe_slug_re(search_inline_source: str) -> None:
    """The inline script declares the same ``SAFE_SLUG_RE`` constant as the emitter.

    Defense in depth: a stale ``contentIndex.json`` carrying an unsafe
    slug would otherwise feed straight into `fetch(url)`. The fix
    rejects unsafe slugs before the URL is built.
    """
    assert "const SAFE_SLUG_RE = /^[a-zA-Z0-9._/,:-]+$/" in search_inline_source, (
        "expected `SAFE_SLUG_RE` allowlist regex constant matching the emitter "
        "(includes `,` and `:` for live-vault slug shapes)"
    )
    # Guard runs inside `fetchBody` before constructing the URL.
    assert "if (!SAFE_SLUG_RE.test(slug))" in search_inline_source, (
        "expected `SAFE_SLUG_RE` guard inside `fetchBody`"
    )


# ---------------------------------------------------------------------------
# P3.6 fix-5 — Synthetic event cast cleanup
# ---------------------------------------------------------------------------


def test_chip_handler_no_synthetic_event_cast(search_inline_source: str) -> None:
    """The chip-toggle handler no longer synthesises a fake `InputEvent`.

    Previously the handler called ``onType({target: searchBar} as
    unknown as InputEvent)``. The fix refactors `onType` to read
    `searchBar.value` directly (no event arg), so the chip handler
    just calls `void onType()`.
    """
    assert (
        "as unknown as InputEvent" not in search_inline_source
    ), "expected no `as unknown as InputEvent` cast in the chip handler"
    assert "void onType()" in search_inline_source, (
        "expected chip handler to call `void onType()` with no args"
    )


def test_on_type_no_event_arg(search_inline_source: str) -> None:
    """`onType` is declared without an event parameter.

    Anchored on the new signature so a future regression that adds the
    `InputEvent` arg back surfaces here.
    """
    assert "async function onType()" in search_inline_source, (
        "expected `async function onType()` (no event parameter) signature"
    )
    assert "currentSearchTerm = searchBar.value" in search_inline_source, (
        "expected `onType` to read `searchBar.value` directly"
    )
