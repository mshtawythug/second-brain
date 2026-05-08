"""Static checks for brain Quartz sidebar layout overrides."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SIDEBAR_SCSS = (
    REPO_ROOT / "quartz_overrides" / "quartz" / "styles" / "brain" / "_sidebar.scss"
)


def test_sidebar_padding_overrides_stock_quartz_spacing() -> None:
    """The brain UI keeps sidebar padding tighter than stock Quartz."""
    text = SIDEBAR_SCSS.read_text(encoding="utf-8")
    assert ".page > #quartz-body .sidebar" in text
    assert "padding: 2rem;" in text


def test_sidebar_right_is_scrollable_when_contents_exceed_viewport() -> None:
    """Regression: the right sidebar must paint its own vertical scrollbar.

    Bug (2026-05-08): on long pages with a 22-entry TOC the right
    sidebar filled to the viewport bottom and the user could not reach
    `brain-related-docs` or Backlinks underneath. Upstream Quartz's
    `base.scss` paints `.sidebar { height: 100vh; position: sticky;
    display: flex }` with NO `overflow-y` declared, so the fixed
    viewport-tall box silently clips anything below. Inner widget
    self-scroll (the TOC's `<ul.toc-content.overflow>`) only scrolls
    its own slice — never the panels stacked below it.

    Fix: the brain overlay declares `overflow-y: auto` on
    `.sidebar.right` so the entire panel column gets a single,
    themed scrollbar that reaches every child. `overscroll-behavior:
    contain` keeps the page underneath from scrolling when the user
    rubber-bands the inner column. Scrollbar width / colors track the
    `--lightgray` token so the scrollbar reads as part of the
    sidebar chrome rather than the OS default.
    """
    text = SIDEBAR_SCSS.read_text(encoding="utf-8")
    assert "overflow-y: auto;" in text, (
        "expected `.sidebar.right` to declare `overflow-y: auto` so the "
        "right sidebar can scroll when its contents exceed 100vh"
    )
    assert "overscroll-behavior: contain;" in text, (
        "expected `overscroll-behavior: contain` on the right sidebar "
        "so over-scroll doesn't bleed into the page underneath"
    )
    assert "scrollbar-width: thin;" in text, (
        "expected themed `scrollbar-width: thin` so the brain scrollbar "
        "doesn't fall back to the OS default chrome"
    )


def test_right_sidebar_children_hold_natural_size_via_flex_zero_zero_auto() -> None:
    """Regression: the four right-sidebar children must not flex-shrink-fight.

    Bug (2026-05-08): stock Quartz paints `.toc { flex: 0 0.5 auto }`
    while sibling panels default to `flex: 0 1 auto`. Items below the
    ToC got shrunk twice as aggressively, and `.brain-related-docs`
    (which carries `min-height: 0` so a packed related list can shrink
    inside its own section) collapsed all the way down to ~19px —
    DevTools confirmed `256 × 19px` rendering on the live affected
    page, i.e. only the section header was visible and every related
    row was hidden.

    Fix: pin every direct child of `.sidebar.right` (graph, toc,
    related-docs, backlinks) to `flex: 0 0 auto` so each holds its
    natural size. Combined with `.sidebar.right { overflow-y: auto }`
    above, the sidebar handles overflow via its own scrollbar instead
    of squeezing children to fit a fixed viewport box.
    """
    text = SIDEBAR_SCSS.read_text(encoding="utf-8")
    # All four direct-child selectors must appear in a single rule
    # group so flex-shrink behavior is uniform — the child-combinator
    # `>` is intentional to scope only to direct children of the right
    # sidebar (avoids accidentally hitting nested ToC widgets if any).
    assert ".sidebar.right > .graph," in text, (
        "expected `.sidebar.right > .graph` selector — without it the "
        "graph card can flex-shrink and trigger the original bug"
    )
    assert ".sidebar.right > .toc," in text, (
        "expected `.sidebar.right > .toc` selector to override stock "
        "`flex: 0 0.5 auto`; otherwise ToC keeps its 0.5 shrink ratio "
        "and squeezes siblings below it"
    )
    assert ".sidebar.right > .brain-related-docs," in text, (
        "expected `.sidebar.right > .brain-related-docs` selector — "
        "without `flex: 0 0 auto` the panel collapsed to ~19px"
    )
    assert ".sidebar.right > .backlinks" in text, (
        "expected `.sidebar.right > .backlinks` selector — without it "
        "Backlinks defaults to `flex: 0 1 auto` and shrinks under "
        "pressure from a long ToC"
    )
    assert "flex: 0 0 auto;" in text, (
        "expected `flex: 0 0 auto` declaration so right-sidebar "
        "children all hold their natural size"
    )


def test_toc_capped_at_40vh_in_right_sidebar() -> None:
    """Regression: the right-sidebar ToC must not exceed 40vh.

    Pairs with `.brain-related-docs-list { max-height: 32vh }` (see
    `_related_docs.scss`). On a tall page the ToC could otherwise grow
    to fit 22+ entries inline and push every panel below it into the
    sidebar's scroll-overflow region. Capping at 40vh keeps the ToC
    inside roughly the top half of the viewport even on hub pages —
    the upstream `.toc { overflow-y: hidden }` plus the inner
    `<ul.toc-content.overflow>`'s own `max-height: calc(100% - 2rem)`
    self-scrolling means an over-tall ToC scrolls inside that 40vh
    cap, not by stealing space from RelatedDocs / Backlinks.

    Combined with the `40vh` ToC cap and the `32vh` RelatedDocs cap,
    on a typical viewport the whole right-sidebar stack fits at a
    glance: a user no longer has to scroll through TOC entries to
    discover that RelatedDocs / Backlinks even exist.
    """
    text = SIDEBAR_SCSS.read_text(encoding="utf-8")
    assert ".sidebar.right > .toc" in text, (
        "expected `.sidebar.right > .toc` selector — child-combinator "
        "scopes the cap to the top-level ToC only (avoids nested toc widgets)"
    )
    assert "max-height: 40vh;" in text, (
        "expected `max-height: 40vh` on the right-sidebar ToC; without "
        "the cap a 22-entry TOC fills the sidebar and starves "
        "RelatedDocs / Backlinks beneath it"
    )
