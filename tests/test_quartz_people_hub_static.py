"""Static checks for the People Hub Quartz overlay (Phase C).

The People Hub adds two pieces of overlay content:

  * A `sortFn` in `quartz_overrides/quartz.layout.ts` that pins the
    `people/` directory ahead of every other folder in the Explorer
    tree, so the per-person hub pages are reachable in one click
    from anywhere on the site.
  * A `_people_hub.scss` partial that gives `kind: people` pages a
    distinct page-kind treatment (accent rail under the H1, bordered
    primary-email block, heavier landing-page H1 on the index).

These tests pin the surface so a future refactor cannot accidentally
drop the pin or unwire the SCSS partial without test failures
flagging it.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _layout_text() -> str:
    return (
        REPO_ROOT / "src" / "brain" / "quartz_overrides" / "quartz.layout.ts"
    ).read_text(encoding="utf-8")


def _custom_scss_text() -> str:
    return (
        REPO_ROOT / "src" / "brain" / "quartz_overrides" / "quartz" / "styles" / "custom.scss"
    ).read_text(encoding="utf-8")


def _people_hub_scss_text() -> str:
    return (
        REPO_ROOT
        / "src" / "brain" / "quartz_overrides"
        / "quartz"
        / "styles"
        / "brain"
        / "_people_hub.scss"
    ).read_text(encoding="utf-8")


def test_explorer_pins_people_folder_to_top_of_tree() -> None:
    """The layout's Explorer config carries a sortFn that pins ``people/``.

    A future refactor that drops the override (e.g. by reverting to
    bare ``Component.Explorer()``) would silently lose the pin — the
    folder would still render, just buried alphabetically. Pin the
    name and the slug-segment constant so both halves stay in sync.
    """
    text = _layout_text()
    # The slug-segment constant the sortFn keys off of.
    assert 'PINNED_EXPLORER_FOLDER_SLUG = "people"' in text
    # Both Explorer slots (default + list pages) opt into the pin.
    assert text.count(
        "Component.Explorer({ sortFn: explorerSortPinningPeople })"
    ) >= 2
    # No bare Explorer() calls remain — every Explorer in the layout
    # must use the pinning sortFn or the pin is incomplete.
    assert "Component.Explorer()" not in text


def test_explorer_sortfn_inlines_slug_literal_for_browser_serialization() -> None:
    """The comparator body must use the literal slug, not the const.

    Quartz serializes ``sortFn`` via ``.toString()`` and re-evaluates the
    function in the browser, where module-level identifiers are out of
    scope. Referencing ``PINNED_EXPLORER_FOLDER_SLUG`` inside the body
    raises ``ReferenceError`` at sort time and breaks the Explorer.
    Pin both branches to the literal so the regression can't recur.
    """
    text = _layout_text()
    # Capture just the comparator body so we don't pick up the const
    # declaration above it.
    start = text.index("function explorerSortPinningPeople")
    end = text.index("\n}\n", start)
    body = text[start:end]
    assert 'a.slugSegment === "people"' in body
    assert 'b.slugSegment === "people"' in body
    # The const reference must NOT appear inside the comparator — that's
    # exactly the closure-over-module-scope bug this test guards.
    assert "PINNED_EXPLORER_FOLDER_SLUG" not in body


def test_people_hub_scss_partial_targets_kind_people_pages() -> None:
    """``_people_hub.scss`` exists and targets the right slug prefixes.

    The partial drives the page-kind styling. Frontmatter ``kind:
    people`` is a marker we set in ``people.py``, but Quartz
    doesn't expose arbitrary frontmatter as ``data-*`` attributes, so
    the partial keys off the slug-prefix instead. Pin the selector
    shape so a refactor that, say, renames the directory from
    ``people/`` to ``contacts/`` triggers test breakage that points
    the reader to the corresponding rename in
    ``src/brain/people.py``.
    """
    text = _people_hub_scss_text()
    # Per-person pages — slug prefix selector.
    assert 'body[data-slug^="people/"]' in text
    # Index page — exact slug selector for the landing-page rule.
    assert 'body[data-slug="people/index"]' in text
    # Distinct H1 treatment — the visual signal the page-kind override
    # exists in the first place. ``border-left`` on the per-person H1
    # OR ``border-bottom`` on the index H1 keeps the assertion tolerant
    # of future visual tuning while still pinning that the partial
    # paints *something* on the H1.
    assert "h1:first-of-type" in text


def test_custom_scss_imports_people_hub_partial() -> None:
    """``custom.scss`` ``@use``s the new partial.

    Without the import, the partial is dead code — dart-sass only
    compiles partials that are explicitly used. The import must live
    in the brain block (after ``base.scss``) so its rules cascade over
    Quartz defaults.
    """
    text = _custom_scss_text()
    assert '@use "./brain/people_hub"' in text


def test_people_hub_scss_documents_emit_contract_dependency() -> None:
    """The SCSS comment block points the reader at the emit module.

    Cross-file coordination is fragile — a reader looking at the SCSS
    in isolation has no way to know the slug prefix is set by Python.
    Pin the breadcrumb so a future migration that moves the emit
    contract knows to update the partial too.
    """
    text = _people_hub_scss_text()
    assert "people.py" in text
