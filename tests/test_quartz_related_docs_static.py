"""Static checks for the Phase 5.2 RelatedDocs sidebar component."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_ROOT = REPO_ROOT / "src" / "brain" / "quartz_overrides" / "quartz"
COMPONENTS_DIR = OVERRIDES_ROOT / "components"
RELATED_TSX = COMPONENTS_DIR / "RelatedDocs.tsx"
RELATED_INLINE = COMPONENTS_DIR / "scripts" / "relatedDocs.inline.ts"
COMPONENTS_INDEX = COMPONENTS_DIR / "index.ts"
LAYOUT_TS = REPO_ROOT / "src" / "brain" / "quartz_overrides" / "quartz.layout.ts"
RELATED_SCSS = OVERRIDES_ROOT / "styles" / "brain" / "_related_docs.scss"
CUSTOM_SCSS = OVERRIDES_ROOT / "styles" / "custom.scss"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing expected file: {path}"
    return path.read_text(encoding="utf-8")


def test_related_docs_component_exposes_sidebar_dom_contract() -> None:
    text = _read(RELATED_TSX)

    assert 'from "./scripts/relatedDocs.inline"' in text
    assert "RelatedDocs.afterDOMLoaded = script" in text
    assert 'classNames(displayClass, "brain-related-docs")' in text
    assert 'data-brain-related-slug={fileData.slug}' in text
    assert 'aria-label="Related notes"' in text
    assert 'class="brain-related-docs-list"' in text


def test_related_docs_runtime_fetches_per_slug_related_json() -> None:
    text = _read(RELATED_INLINE)

    assert 'RELATED_DOCS_RELDIR = "static/related"' in text
    assert "pathToRoot(currentSlug)" in text
    assert "fetch(url)" in text
    assert "sourceIconFor" in text
    assert "inferSource" in text
    assert ".brain-related-docs" in text
    assert ".brain-related-docs-list" in text
    assert ".brain-related-docs-empty" in text
    assert "document.addEventListener(\"nav\"" in text
    assert "panelStillMatchesSlug" in text


def test_related_docs_is_exported_and_wired_into_right_sidebar() -> None:
    index_text = _read(COMPONENTS_INDEX)
    layout_text = _read(LAYOUT_TS)

    assert 'import RelatedDocs from "./RelatedDocs"' in index_text
    assert "RelatedDocs," in index_text
    assert "Component.RelatedDocs()" in layout_text
    # brain (2026-05-06): right-sidebar order is graph → toc → related
    # → backlinks. ToC sits above RelatedDocs because page-utility
    # outranks exploration; previously RelatedDocs ran above the ToC
    # and crowded the flex column on hub pages with many related notes,
    # squeezing the ToC's inner scroll list to ~0px and making its
    # entries unreachable.
    assert layout_text.index("Component.Graph(") < layout_text.index(
        "Component.DesktopOnly(Component.TableOfContents())"
    )
    assert layout_text.index(
        "Component.DesktopOnly(Component.TableOfContents())"
    ) < layout_text.index("Component.RelatedDocs()")


def test_related_docs_styles_are_loaded() -> None:
    text = _read(RELATED_SCSS)
    custom = _read(CUSTOM_SCSS)

    for selector in (
        ".brain-related-docs",
        ".brain-related-docs-list",
        ".brain-related-docs-item",
        ".brain-related-docs-source",
        ".brain-related-docs-empty",
    ):
        assert selector in text
    assert '@use "./brain/related_docs"' in custom


def test_related_docs_section_contains_inner_list_via_flex_column() -> None:
    """Regression: prevent the inner <ol> from spilling onto Backlinks.

    Bug (2026-05-06): when the right-sidebar flex column squeezed
    `.brain-related-docs` (which has `min-height: 0`), the inner list
    kept its hardcoded `max-height: 32vh` and rendered past the section
    box (default `overflow: visible`) — visually overlapping the
    Backlinks panel below it. Measured: section box was 146px tall but
    the list rendered at 288px, spilling 152px onto Backlinks at y=772.

    Fix: section becomes a flex column itself + the list grows
    `min-height: 0` so the section's flex layout shrinks the list (and
    its `overflow-y: auto` provides internal scroll) instead of letting
    it overflow the section box.
    """
    text = _read(RELATED_SCSS)

    # The section must be a flex column so the inner list participates
    # in flex sizing (and shrinks within the section's allocated height).
    assert "display: flex" in text
    assert "flex-direction: column" in text

    # The inner list must allow flex-shrink (min-height: 0) and provide
    # its own internal scroll (overflow-y: auto). Without min-height: 0,
    # flex children refuse to shrink below content and the list
    # overflows the section box.
    list_block_start = text.index(".brain-related-docs-list")
    list_block_end = text.index("}", list_block_start + 1)
    # Find the closing brace of the *outer* list block (skip nested
    # &::-webkit-scrollbar rules); search for the next selector instead.
    list_block_end = text.index(".brain-related-docs-item", list_block_start)
    list_block = text[list_block_start:list_block_end]
    assert "min-height: 0" in list_block, (
        "list needs min-height: 0 so flex layout can shrink it within "
        "the section box (otherwise it overflows onto Backlinks)"
    )
    assert "overflow-y: auto" in list_block, (
        "list needs internal scroll so users can still see all related "
        "items when the section is squeezed by the parent flex column"
    )
    assert "max-height: 32vh" in list_block, (
        "list keeps its 32vh cap so a packed related set leaves room "
        "for Backlinks below in the natural-height case"
    )
