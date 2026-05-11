"""Static smoke tests for the Phase 4.3 Explorer per-source month grouping.

Background
----------

P4.3 of the Wiki UX Overhaul adds per-source month grouping to the
Explorer for `_ingested/krisp/` and `_ingested/gmail/`. When the
client-side tree builder descends into either of those folders, the
direct file children are bucketed by `YYYY-MM` (parsed from the slug
date prefix) and each bucket renders newest-first as a header row
followed by the bucket's files (newest day first). Each file's link
text is rewritten to `<MMM D> · <Title>` so the meeting / thread date
is visible without expanding the doc.

Implementation lives entirely in
`quartz_overrides/quartz/components/scripts/explorer.inline.ts`
(no SSR-side `Explorer.tsx` override — keeps the upstream component
unforked, same pattern as P4.2's "Show ingested" toggle and the
`.folder-count` badge).

These tests are static-source only — the project doesn't run a JS /
Quartz toolchain in CI. Pattern matches `tests/test_explorer_filter.py`
and the other `test_quartz_*` static suites. End-to-end browser-driven
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


# ---------------------------------------------------------------------------
# explorer.inline.ts — month-grouping wiring contract
# ---------------------------------------------------------------------------


def test_inline_pins_month_grouped_folder_paths(
    explorer_inline_source: str,
) -> None:
    """`MONTH_GROUPED_FOLDERS` set carries the two activation paths.

    The two paths are simple-slug form (post-`simplifySlug`) so the
    `isMonthGroupedFolder` predicate matches what the trie produces
    for the `_ingested/krisp` and `_ingested/gmail` folder nodes.

    **Critical**: each literal MUST end with a trailing slash. Quartz's
    `simplifySlug("foo/bar/index")` returns `"foo/bar/"` (its internal
    `stripSlashes(s, true)` call only strips the leading slash; the
    trailing slash is preserved — verified by upstream
    `quartz/util/path.test.ts::simplifySlug`). Dropping the trailing
    slash here would silently turn off the gate in production.
    Both literals are pinned so a future folder rename forces the
    test to be updated in lockstep with the implementation.
    """
    assert "MONTH_GROUPED_FOLDERS" in explorer_inline_source, (
        "expected `MONTH_GROUPED_FOLDERS` constant set"
    )
    assert '"_ingested/krisp/"' in explorer_inline_source, (
        "expected literal `_ingested/krisp/` (with trailing slash) "
        "in the activation set — see docstring re: simplifySlug"
    )
    assert '"_ingested/gmail/"' in explorer_inline_source, (
        "expected literal `_ingested/gmail/` (with trailing slash) "
        "in the activation set — see docstring re: simplifySlug"
    )


def test_inline_activation_predicate_uses_simplify_slug(
    explorer_inline_source: str,
) -> None:
    """`isMonthGroupedFolder` consults `simplifySlug(folderPath)`.

    Without the `simplifySlug` call the comparison would test the
    raw `_ingested/krisp/index` slug against the simple-slug literals
    in `MONTH_GROUPED_FOLDERS` and never match — the gate would
    silently turn off in production.
    """
    assert "function isMonthGroupedFolder(" in explorer_inline_source, (
        "expected `isMonthGroupedFolder` predicate"
    )
    pattern = re.compile(
        r"function isMonthGroupedFolder\([^)]*\)[^{]*\{[^}]*"
        r"simplifySlug\(folderPath\)",
        re.DOTALL,
    )
    assert pattern.search(explorer_inline_source) is not None, (
        "expected `isMonthGroupedFolder` to call `simplifySlug(folderPath)`"
    )


def test_inline_parses_yyyy_mm_dd_prefix(explorer_inline_source: str) -> None:
    """`parseSlugDate` extracts `YYYY-MM-DD` from the slug segment.

    The regex is the canonical seam — anchor on its literal source
    so a refactor doesn't accidentally widen / narrow the date shape
    (e.g., dropping the leading anchor and matching `2026` mid-slug).
    """
    assert "SLUG_DATE_PREFIX_RE" in explorer_inline_source, (
        "expected `SLUG_DATE_PREFIX_RE` constant"
    )
    assert "/^(\\d{4})-(\\d{2})-(\\d{2})/" in explorer_inline_source, (
        "expected `/^(\\d{4})-(\\d{2})-(\\d{2})/` regex literal"
    )
    assert "function parseSlugDate(" in explorer_inline_source, (
        "expected `parseSlugDate(slugSegment)` helper"
    )
    # The bounds-check guards against a malformed slug like
    # `2026-13-99-...` yielding a phantom group.
    assert "month < 1 || month > 12" in explorer_inline_source, (
        "expected month bounds-check `1..12`"
    )
    assert "day < 1 || day > 31" in explorer_inline_source, (
        "expected day bounds-check `1..31`"
    )


def test_inline_month_key_is_yyyy_mm(explorer_inline_source: str) -> None:
    """`monthKey` returns `YYYY-MM` (zero-padded).

    Fixed-width zero-padded keys mean lexicographic sort === chronological
    sort — that's the property the descending `keys().sort().reverse()`
    pass relies on for newest-first ordering.
    """
    assert "function monthKey(" in explorer_inline_source, (
        "expected `monthKey(parsed)` helper"
    )
    pattern = re.compile(
        r"function monthKey\([^)]*\)[^{]*\{[^}]*"
        r'`\$\{parsed\.year\}-\$\{String\(parsed\.month\)\.padStart\(2, "0"\)\}`',
        re.DOTALL,
    )
    assert pattern.search(explorer_inline_source) is not None, (
        "expected `monthKey` to interpolate `${year}-${padStart(month, 2, '0')}`"
    )


def test_inline_month_labels_pinned(explorer_inline_source: str) -> None:
    """Month-name arrays carry both long and short forms.

    Pinned literals (rather than `Intl.DateTimeFormat`) so SSR-free
    rendering doesn't depend on the visitor's browser locale and so
    this test can anchor on them.
    """
    assert "MONTH_LABELS_LONG" in explorer_inline_source, (
        "expected `MONTH_LABELS_LONG` for group headers"
    )
    assert "MONTH_LABELS_SHORT" in explorer_inline_source, (
        "expected `MONTH_LABELS_SHORT` for per-file date prefixes"
    )
    # A few representative month names — anchor without listing all 12.
    assert '"January"' in explorer_inline_source, (
        "expected `January` in long labels"
    )
    assert '"December"' in explorer_inline_source, (
        "expected `December` in long labels"
    )
    assert '"Jan"' in explorer_inline_source, "expected `Jan` in short labels"
    assert '"Dec"' in explorer_inline_source, "expected `Dec` in short labels"


def test_inline_renders_month_header_with_class(
    explorer_inline_source: str,
) -> None:
    """Month-group header `<li>` carries the canonical class hook.

    `_explorer.scss` styles this exact class — drift between the script
    and the SCSS would render the header as an unstyled list item.
    `role="presentation"` is set so screen readers don't announce
    the synthetic separator as a navigable list-item.
    """
    assert '"brain-explorer-month-header"' in explorer_inline_source, (
        "expected `brain-explorer-month-header` class on the header `<li>`"
    )
    assert 'setAttribute("role", "presentation")' in explorer_inline_source, (
        "expected `role=presentation` on the synthetic month header"
    )


def test_inline_file_node_has_date_and_title_spans(
    explorer_inline_source: str,
) -> None:
    """`createFileNodeWithDatePrefix` injects two spans into the link.

    The `-date` span carries the `Apr 15` prefix; the `-title` span
    carries the document title. Two spans (rather than one combined
    text node) give SCSS independent typography hooks.
    """
    assert "function createFileNodeWithDatePrefix(" in explorer_inline_source, (
        "expected `createFileNodeWithDatePrefix(currentSlug, node, parsed)` helper"
    )
    assert '"brain-explorer-month-date"' in explorer_inline_source, (
        "expected `brain-explorer-month-date` class span"
    )
    assert '"brain-explorer-month-title"' in explorer_inline_source, (
        "expected `brain-explorer-month-title` class span"
    )


def test_inline_grouping_helper_iterates_node_children(
    explorer_inline_source: str,
) -> None:
    """`buildMonthGroupedChildren` enumerates `node.children` and routes them.

    The helper must be the integration seam called from
    `createFolderNode`. Anchor on the function signature plus the
    bucket-population loop body so a future refactor that replaces
    the `Map<string, Bucket>` shape with something else still
    surfaces here.
    """
    assert "function buildMonthGroupedChildren(" in explorer_inline_source, (
        "expected `buildMonthGroupedChildren` helper"
    )
    pattern = re.compile(
        r"function buildMonthGroupedChildren\([^)]*\)[^{]*\{.*?"
        r"for \(const child of node\.children\)",
        re.DOTALL,
    )
    assert pattern.search(explorer_inline_source) is not None, (
        "expected `buildMonthGroupedChildren` to loop over `node.children`"
    )


def test_inline_buckets_sort_newest_first(explorer_inline_source: str) -> None:
    """Month buckets render newest first.

    The `keys().sort().reverse()` pattern relies on `YYYY-MM` keys
    being fixed-width — pin both the sort and the reverse so a
    refactor doesn't accidentally break either half.
    """
    pattern = re.compile(
        r"\[\.\.\.buckets\.keys\(\)\]\.sort\(\)\.reverse\(\)",
    )
    assert pattern.search(explorer_inline_source) is not None, (
        "expected `[...buckets.keys()].sort().reverse()` for newest-first month order"
    )


def test_inline_within_month_sort_day_descending(
    explorer_inline_source: str,
) -> None:
    """Within a month, items sort by day descending.

    `b.parsed.day - a.parsed.day` is the canonical descending compare.
    Tie-break on slug-segment `localeCompare` keeps the order
    deterministic when two meetings share a date.
    """
    assert "b.parsed.day - a.parsed.day" in explorer_inline_source, (
        "expected `b.parsed.day - a.parsed.day` for day-descending sort"
    )
    assert "a.child.slugSegment.localeCompare(b.child.slugSegment)" in (
        explorer_inline_source
    ), "expected slug-segment tie-breaker for stable same-day ordering"


def test_inline_skips_empty_buckets_defensively(
    explorer_inline_source: str,
) -> None:
    """Empty bucket guard — defensive skip on `bucket.items.length === 0`.

    A bucket should only ever exist when at least one item populated
    it, but the explicit skip means a future hand-edit that
    pre-allocates buckets can't render an empty header row.
    """
    assert "bucket.items.length === 0" in explorer_inline_source, (
        "expected `bucket.items.length === 0` skip in the render loop"
    )


def test_inline_undated_files_appended_at_end(
    explorer_inline_source: str,
) -> None:
    """Files with no `YYYY-MM-DD` slug prefix render through `createFileNode`.

    Defensive — covers hand-dropped non-ingested files. The undated
    rows render through plain `createFileNode` (no date span override)
    so they look identical to a normal explorer row.
    """
    assert "undatedFiles" in explorer_inline_source, (
        "expected `undatedFiles` accumulator for slug-without-date rows"
    )
    pattern = re.compile(
        r"for \(const child of undatedFiles\)[^{]*\{[^}]*"
        r"createFileNode\(currentSlug, child\)",
        re.DOTALL,
    )
    assert pattern.search(explorer_inline_source) is not None, (
        "expected undated files to be rendered via plain `createFileNode`"
    )


def test_inline_groupfn_gated_inside_create_folder_node(
    explorer_inline_source: str,
) -> None:
    """The grouping branch is gated on `isMonthGroupedFolder(folderPath)`.

    The `else` branch is the original verbatim child loop — pin both
    so a future refactor can't accidentally drop the unchanged-folder
    path or invert the gate.
    """
    pattern = re.compile(
        r"if \(isMonthGroupedFolder\(folderPath\)\)\s*\{\s*"
        r"buildMonthGroupedChildren\(currentSlug, node, ul, opts\)\s*\}\s*"
        r"else\s*\{\s*"
        r"for \(const child of node\.children\)",
        re.DOTALL,
    )
    assert pattern.search(explorer_inline_source) is not None, (
        "expected `if (isMonthGroupedFolder(folderPath)) { build… } "
        "else { for (const child of node.children) … }` integration"
    )


def test_inline_month_separator_is_middle_dot(
    explorer_inline_source: str,
) -> None:
    """The visual separator between date and title is a middle dot.

    Pinned because the SCSS spacing (`min-width` on the date span +
    margin) is calibrated to this exact glyph. Swapping in an em-dash
    would visually feel cramped.
    """
    assert 'MONTH_DATE_SEPARATOR = " · "' in explorer_inline_source, (
        "expected ` · ` middle-dot separator between date prefix and title"
    )


# ---------------------------------------------------------------------------
# _explorer.scss — class hooks for the rendered grouping
# ---------------------------------------------------------------------------


def test_scss_declares_month_header_selector(
    explorer_scss_source: str,
) -> None:
    """The SCSS partial declares the month-header rule.

    Without this rule the `<li>` injected by the inline script would
    render as a default list item — the visual section break depends
    on the styling here.
    """
    assert ".brain-explorer-month-header" in explorer_scss_source, (
        "expected `.brain-explorer-month-header` selector in _explorer.scss"
    )


def test_scss_declares_month_date_and_title_selectors(
    explorer_scss_source: str,
) -> None:
    """Both per-file spans get distinct typography rules.

    Drift between the script's class names and the SCSS selectors
    would make the date prefix render in regular ink — defeating the
    visual hierarchy of "date is marginalia, title is the read".
    """
    assert ".brain-explorer-month-date" in explorer_scss_source, (
        "expected `.brain-explorer-month-date` selector"
    )
    assert ".brain-explorer-month-title" in explorer_scss_source, (
        "expected `.brain-explorer-month-title` selector"
    )


def test_scss_uses_tabular_numerals_for_dates(
    explorer_scss_source: str,
) -> None:
    """Date prefix uses tabular numerals for vertical alignment.

    `Mar 9` vs `Apr 15` would have different widths under proportional
    figures — tabular-nums force fixed-width digits so the row of
    dates aligns vertically in the explorer rail.
    """
    assert "tabular-nums" in explorer_scss_source, (
        "expected `font-variant-numeric: tabular-nums` on the date span"
    )


# ---------------------------------------------------------------------------
# Cross-file consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "class_name",
    [
        "brain-explorer-month-header",
        "brain-explorer-month-date",
        "brain-explorer-month-title",
    ],
)
def test_class_name_matches_between_inline_and_scss(
    class_name: str,
    explorer_inline_source: str,
    explorer_scss_source: str,
) -> None:
    """Each P4.3 class hook appears in BOTH the script and the SCSS.

    Drift between the two would render the affected element without
    its intended styling.
    """
    assert class_name in explorer_inline_source, (
        f"explorer.inline.ts must reference `{class_name}` (P4.3 class hook)"
    )
    assert class_name in explorer_scss_source, (
        f"_explorer.scss must declare a rule for `{class_name}` (P4.3 class hook)"
    )
