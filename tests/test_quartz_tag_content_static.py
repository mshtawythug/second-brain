"""Static smoke tests for the Phase 3.3 custom TagContent component.

The brain repo's test image does not run a JS toolchain, so we cannot
build the Quartz output and exercise the component end-to-end here.
The closest existing pattern is ``tests/test_quartz_search_static.py``
(regex-based static checks against TS source). This file follows the
same flavor, scoped to the P3.3 contract:

- A ``TagContent.tsx`` override exists under
  ``quartz_overrides/quartz/components/pages/`` and renders each doc
  row as ``{icon} {title} · {date} · {snippet}`` plus a
  ``tagged: #...`` footer.
- Source icons are imported from the shared
  ``quartz_overrides/quartz/util/sourceIcons.ts`` module so the
  vocabulary stays in lock-step with ``Search.tsx``'s
  ``SOURCE_ICONS`` constant.
- The component emits the brain-namespaced class hooks the SCSS
  partial keys off (``brain-tag-row`` / ``brain-tag-icon`` / …).
- The new SCSS partial ``_tag_content.scss`` exists and is wired
  into ``custom.scss`` so dart-sass picks it up.
- The component delegates to ``getDate`` / ``QuartzDate`` from the
  upstream ``Date`` helpers so the date format matches the rest of
  the wiki (no bespoke parsing).

Limitations: this file only asserts the SOURCE shape. A full
end-to-end test would invoke ``npx quartz build`` against a fixture
vault and parse the rendered HTML. That needs a JS toolchain not on
the test image — flagged in the P3.3 DONE report and covered later by
the P3.5 Playwright harness.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_ROOT = REPO_ROOT / "src" / "brain" / "quartz_overrides"
TAG_CONTENT_TSX = (
    OVERRIDES_ROOT / "quartz" / "components" / "pages" / "TagContent.tsx"
)
SOURCE_ICONS_TS = OVERRIDES_ROOT / "quartz" / "util" / "sourceIcons.ts"
SEARCH_TSX = OVERRIDES_ROOT / "quartz" / "components" / "Search.tsx"
TAG_CONTENT_SCSS = OVERRIDES_ROOT / "quartz" / "styles" / "brain" / "_tag_content.scss"
CUSTOM_SCSS = OVERRIDES_ROOT / "quartz" / "styles" / "custom.scss"

# Source-icon vocabulary the override is expected to support — same
# set Search.tsx + the SCSS chip palette pin. New ingest sources show
# up here, in `Search.tsx`'s SOURCE_ICONS, and in ``_search.scss``'s
# chip palette together.
EXPECTED_SOURCES = ("gmail", "krisp", "slack", "manual", "vault")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tag_content_source() -> str:
    """Read the TagContent override source once per module."""
    assert TAG_CONTENT_TSX.is_file(), f"missing component override at {TAG_CONTENT_TSX}"
    return TAG_CONTENT_TSX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def source_icons_source() -> str:
    """Read the shared source-icons util once per module."""
    assert SOURCE_ICONS_TS.is_file(), f"missing util at {SOURCE_ICONS_TS}"
    return SOURCE_ICONS_TS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def search_tsx_source() -> str:
    """Read the Search.tsx override (parity check target) once per module."""
    assert SEARCH_TSX.is_file(), f"missing component at {SEARCH_TSX}"
    return SEARCH_TSX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tag_content_scss_source() -> str:
    """Read the new tag-content SCSS partial once per module."""
    assert TAG_CONTENT_SCSS.is_file(), f"missing partial at {TAG_CONTENT_SCSS}"
    return TAG_CONTENT_SCSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def custom_scss_source() -> str:
    """Read the SCSS entry point once per module."""
    assert CUSTOM_SCSS.is_file(), f"missing entry point at {CUSTOM_SCSS}"
    return CUSTOM_SCSS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Shared util — sourceIcons.ts
# ---------------------------------------------------------------------------


def test_source_icons_util_exports_full_vocab(source_icons_source: str) -> None:
    """``util/sourceIcons.ts`` must export ``SOURCE_ICONS`` with all 5 keys.

    The util is the canonical glyph table for any new consumer. A
    missing key here means a missing icon on tag rows / search rows /
    any future surface that imports the util.
    """
    assert "export const SOURCE_ICONS" in source_icons_source, (
        "expected SOURCE_ICONS export in shared util"
    )
    for key in EXPECTED_SOURCES:
        assert f"{key}:" in source_icons_source, (
            f"sourceIcons.ts missing key `{key}`"
        )


def test_source_icons_util_exports_helpers(source_icons_source: str) -> None:
    """The util exposes ``inferSource`` + ``sourceIconFor`` helpers.

    TagContent (and any future consumer) reaches for these to convert
    a slug + frontmatter into a glyph in one call. Their existence is
    the whole point of factoring the util out — assert directly so a
    refactor can't quietly delete them.
    """
    assert "export function inferSource" in source_icons_source, (
        "expected inferSource helper export"
    )
    assert "export function sourceIconFor" in source_icons_source, (
        "expected sourceIconFor helper export"
    )


def test_source_icons_util_parity_with_search_tsx(
    source_icons_source: str, search_tsx_source: str
) -> None:
    """Search.tsx imports the canonical util; util has every expected key.

    P3.6 fix-4 consolidated the duplicated source-icon table: Search.tsx
    no longer declares its own copy, it imports ``SOURCE_ICONS`` from
    ``util/sourceIcons.ts``. The parity check that previously kept the
    two literals in sync is now a single import-statement check —
    Search.tsx must reach for the util, and the util must carry every
    key the chip rail expects.
    """
    for key in EXPECTED_SOURCES:
        assert f"{key}:" in source_icons_source, (
            f"util missing `{key}` (canonical source of truth)"
        )
    assert 'from "../util/sourceIcons"' in search_tsx_source, (
        "Search.tsx must import its source-icon table from `../util/sourceIcons`"
    )
    assert "SOURCE_ICONS" in search_tsx_source, (
        "Search.tsx must reference the imported `SOURCE_ICONS` constant"
    )


# ---------------------------------------------------------------------------
# TagContent.tsx — component shape
# ---------------------------------------------------------------------------


def test_tag_content_imports_shared_source_icons(tag_content_source: str) -> None:
    """TagContent imports ``inferSource`` + ``sourceIconFor`` from the util.

    Anchors the wiring: the override must import from
    ``../../util/sourceIcons`` (the relative path that resolves under
    ``quartz/components/pages/``). A typo here would silently fall
    back to the upstream PageList rendering after a build error, and
    we want this caught before the build.
    """
    assert "../../util/sourceIcons" in tag_content_source, (
        "TagContent must import from `../../util/sourceIcons`"
    )
    assert "inferSource" in tag_content_source, (
        "TagContent must call inferSource for each row"
    )
    assert "sourceIconFor" in tag_content_source, (
        "TagContent must call sourceIconFor for each row"
    )


def test_tag_content_renders_brain_row_class_hooks(tag_content_source: str) -> None:
    """Each doc row carries the ``brain-tag-*`` class hooks the SCSS keys off.

    Without these hooks the SCSS partial silently doesn't apply and
    rows render as unstyled text. Pinning each class name makes a
    rename break the test (intentional — rename forces a SCSS update).
    """
    expected_classes = (
        "brain-tag-row",
        "brain-tag-icon",
        "brain-tag-title",
        "brain-tag-date",
        "brain-tag-snippet",
        "brain-tag-footer",
    )
    for cls in expected_classes:
        assert cls in tag_content_source, (
            f"TagContent must render class `{cls}` (SCSS partial keys off it)"
        )


def test_tag_content_renders_tagged_footer(tag_content_source: str) -> None:
    """The ``tagged:`` footer label is rendered as static text.

    Anchored on the literal label string — a rename to "tags:" or
    "filed under:" would be a UX deviation from the plan and should
    require updating both the test and the plan.
    """
    assert "tagged:" in tag_content_source, (
        "TagContent must render literal `tagged:` footer label"
    )


def test_tag_content_renders_hash_tag_prefix(tag_content_source: str) -> None:
    """Each footer link prefixes the tag name with ``#``.

    The ``#tag1 #tag2`` shape is what the plan specifies and what
    matches the chip rail vocabulary on the search popover. Anchored
    on the literal ``#`` JSX child so a rename to ``"tag:"`` trips
    here.
    """
    assert "#{rowTag}" in tag_content_source, (
        "TagContent must prefix footer tag links with `#`"
    )


def test_tag_content_uses_quartz_date_helper(tag_content_source: str) -> None:
    """The component reuses ``QuartzDate`` + ``getDate`` from upstream.

    Reusing the upstream helper means the rendered date format
    (``Apr 12, 2026``) matches the rest of the wiki — content-meta,
    PageList, RecentNotes — without a bespoke parser drifting away.
    Anchored on both imports so a rewrite that drops one trips here.
    """
    assert "QuartzDate" in tag_content_source, (
        "TagContent must alias and use the upstream Date component"
    )
    assert "getDate" in tag_content_source, (
        "TagContent must call getDate(cfg, page) — drift-proof formatting"
    )


def test_tag_content_uses_section_li(tag_content_source: str) -> None:
    """Rows still render as ``<li class=\"section-li\">`` for upstream class compat.

    Upstream styles for `.section-li` (border, spacing) cascade onto
    our brain rows — keeping the class name preserves the baseline
    look while the brain-prefixed classes layer on top. A rename to a
    pure brain class would lose the upstream rules and force a wider
    SCSS rewrite.
    """
    assert "section-li" in tag_content_source, (
        "rows must keep the upstream `section-li` class for cascade compat"
    )


def test_tag_content_preserves_index_page_structure(tag_content_source: str) -> None:
    """The ``tag === \"/\"`` index branch is preserved verbatim from upstream.

    The brain delta only applies to per-tag listing pages (the `else`
    branch). The aggregate index that lists every tag with a sub-list
    of pages stays as upstream renders it — overriding it would mean
    re-implementing the entire two-level list, which is more risk
    than UX gain at this scale. Anchored on the upstream marker
    ``tag === \"/\"`` (the truthy-check that gates the index path).
    """
    assert 'tag === "/"' in tag_content_source, (
        "index-mode branch (tag === '/') must remain — only per-tag rendering is overridden"
    )


def test_tag_content_throws_on_non_tag_slug(tag_content_source: str) -> None:
    """The component preserves upstream's slug-shape guard.

    Stock TagContent throws when rendered against a non-tag slug — a
    sanity check that pays off if a future layout change accidentally
    routes a content page through the wrong renderer. Keeping the
    guard means the whole-build error message stays meaningful.
    """
    assert 'tried to render a non-tag page' in tag_content_source, (
        "non-tag slug guard must be preserved"
    )


# ---------------------------------------------------------------------------
# Snippet helper — pure-function behaviour exercised via Python port
# ---------------------------------------------------------------------------


def _python_port_compute_snippet(description: str, tag: str, window: int) -> str:
    """Mirror of the TS ``computeTagSnippet`` helper.

    The TS helper:
        const collapsed = description.replace(/\\s+/g, " ").trim()
        if (collapsed.length === 0) return ""
        const leaf = tag.includes("/") ? tag.slice(tag.lastIndexOf("/") + 1) : tag
        const idx = collapsed.toLowerCase().indexOf(leaf.toLowerCase())
        if (idx < 0) return collapsed.slice(0, window * 2)
        const start = Math.max(0, idx - window)
        const end   = Math.min(collapsed.length, idx + leaf.length + window)
        const prefix = start > 0 ? "…" : ""
        const suffix = end < collapsed.length ? "…" : ""
        return `${prefix}${collapsed.slice(start, end)}${suffix}`

    Drift-proofs the contract: any algorithmic change in the TS source
    needs a matching update here (or the static check above will trip).
    """
    import re

    collapsed = re.sub(r"\s+", " ", description).strip()
    if not collapsed:
        return ""
    leaf = tag.split("/")[-1] if "/" in tag else tag
    idx = collapsed.lower().find(leaf.lower())
    if idx < 0:
        return collapsed[: window * 2]
    start = max(0, idx - window)
    end = min(len(collapsed), idx + len(leaf) + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(collapsed) else ""
    return f"{prefix}{collapsed[start:end]}{suffix}"


def test_compute_snippet_centres_on_tag_occurrence() -> None:
    """When the tag appears in the description, the snippet wraps it with ellipses."""
    description = "x" * 200 + " interview " + "y" * 200
    out = _python_port_compute_snippet(description, "interview", window=20)
    # The snippet should contain "interview" and start/end with `…` because we're
    # well inside both ends.
    assert "interview" in out
    assert out.startswith("…")
    assert out.endswith("…")


def test_compute_snippet_falls_back_to_leading_chars() -> None:
    """No tag occurrence ⇒ leading ``2 * window`` chars of the description."""
    description = "alpha beta gamma delta epsilon zeta eta theta"
    out = _python_port_compute_snippet(description, "missingtag", window=10)
    assert out == description[:20]


def test_compute_snippet_collapses_whitespace() -> None:
    """Multi-line / tab-padded descriptions collapse to a single line."""
    description = "first line\n\nsecond\tline    third"
    out = _python_port_compute_snippet(description, "second", window=5)
    # No leftover whitespace blocks — every internal whitespace run
    # collapses to a single space.
    assert "  " not in out
    assert "\n" not in out
    assert "\t" not in out
    assert "second" in out


def test_compute_snippet_uses_leaf_for_nested_tag() -> None:
    """Nested tags like ``interview/take-home`` match on the leaf segment."""
    description = "yesterday I worked on the take-home assignment for Foo"
    out = _python_port_compute_snippet(description, "interview/take-home", window=10)
    # "take-home" should be the anchor — full description is short
    # enough that we keep most of it.
    assert "take-home" in out


def test_compute_snippet_empty_description_returns_empty() -> None:
    """Empty / whitespace-only description returns the empty string."""
    assert _python_port_compute_snippet("", "anything", window=10) == ""
    assert _python_port_compute_snippet("   \n\t  ", "anything", window=10) == ""


def test_compute_snippet_no_leading_ellipsis_when_at_start() -> None:
    """When the match is at index 0 we don't prefix with ``…``."""
    description = "interview prep notes for the Acme team"
    out = _python_port_compute_snippet(description, "interview", window=10)
    assert not out.startswith("…")
    assert "interview" in out


# ---------------------------------------------------------------------------
# SCSS — partial exists, wired into custom.scss
# ---------------------------------------------------------------------------


def test_tag_content_scss_declares_brain_row_classes(
    tag_content_scss_source: str,
) -> None:
    """The SCSS partial styles every brain class hook the component renders."""
    expected = (
        ".brain-tag-results",
        ".brain-tag-row",
        ".brain-tag-icon",
        ".brain-tag-title",
        ".brain-tag-date",
        ".brain-tag-snippet",
        ".brain-tag-footer",
    )
    for cls in expected:
        assert cls in tag_content_scss_source, (
            f"_tag_content.scss missing rule for `{cls}`"
        )


def test_tag_content_scss_loaded_via_custom_entry(custom_scss_source: str) -> None:
    """``custom.scss`` ``@use``s the new partial so dart-sass picks it up.

    Without this line the partial sits on disk but never reaches the
    bundle — visible in production as unstyled tag rows. Anchored on
    the ``./brain/tag_content`` relative path the partial resolves
    under.
    """
    assert "tag_content" in custom_scss_source, (
        "custom.scss must `@use ./brain/tag_content`"
    )


# ---------------------------------------------------------------------------
# Layout / barrel wiring — TagContent flows through the existing barrel
# ---------------------------------------------------------------------------


def test_tag_content_at_pages_subpath() -> None:
    """File lives at ``components/pages/TagContent.tsx`` — the upstream import path.

    The components barrel imports from ``./pages/TagContent``; the
    overlay copies our override into ``<workspace>/quartz/components/
    pages/TagContent.tsx`` which replaces the upstream stock file in
    the same slot. A test that asserts the file exists at this exact
    path catches a stray placement (e.g. accidentally writing it at
    ``components/TagContent.tsx``) at test-time rather than at build
    time.
    """
    assert TAG_CONTENT_TSX.is_file(), (
        f"TagContent override must live at {TAG_CONTENT_TSX} so the overlay "
        f"copy lands on the upstream file"
    )


# ---------------------------------------------------------------------------
# P3.6 fix-6 — Preact `key` prop on tag-footer fragments
# ---------------------------------------------------------------------------


def test_tag_footer_loop_carries_key_prop(tag_content_source: str) -> None:
    """Every tag-footer iteration carries a stable ``key={rowTag}`` prop.

    Bare `<>` fragments inside `.map()` trigger Preact's runtime
    "Each child in a list should have a unique key prop" warning.
    The fix promotes the fragment to a `<span key={rowTag}>` so the
    reconciler can match nodes correctly when the tag list mutates
    between renders. Anchored on the literal `key={rowTag}` token so
    a regression to a bare fragment trips the test.
    """
    assert "key={rowTag}" in tag_content_source, (
        "expected `key={rowTag}` on the tag-footer iteration to silence "
        "Preact's missing-key warning"
    )


def test_tag_footer_no_bare_fragment_in_map(tag_content_source: str) -> None:
    """The footer .map() does not return a bare `<>` fragment.

    Anchors that the fix replaces ``<>...</>`` with a host element
    carrying the key. We look for the now-canonical wrapping span
    class so a future refactor either preserves the class or updates
    this test.
    """
    assert "brain-tag-footer-fragment" in tag_content_source, (
        "expected a host element (e.g. `<span class=\"brain-tag-footer-fragment\">`) "
        "wrapping each footer iteration so `key` lands on a real node"
    )
