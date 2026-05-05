"""Static regression tests for the Phase 4.5 graph-chip dimension-scoped toggle.

Background
----------

P4.5 of the Wiki UX Overhaul (`docs/plans/2026-05-03-wiki-ux-overhaul.md`
lines 264-271) addresses the bug "clicking Tier ``vault`` flips Source
``ALL``". The root cause hypothesis in the plan was a flat
``activeChipFilters: Set<string>`` that all dimensions share, where a
toggle of any chip mutated the same set used to compute the per-row
ALL state.

Status at HEAD
^^^^^^^^^^^^^^

The data structure in
``quartz_overrides/quartz/components/scripts/graph.inline.ts`` is
already shaped per-dimension::

    const activeChipFilters: { tier: Set<string>; source: Set<string> } = {
      tier: new Set<string>(chipVocabularies.tier),
      source: new Set<string>(chipVocabularies.source),
    }

…and the chip click handlers are dimension-scoped via the for-loop's
``const dimension`` closure, so the spec's diagnosis no longer matches
the code (likely fixed during an earlier graph cleanup; see commit
``3be5a1e`` "drop tier/source chips from local graph").

These tests therefore lock in the correct shape so a future hand edit
that flattens the structure or drops the dimension scope (for example,
naively migrating to a ``Map<string, Set<string>>`` and forgetting the
key in the toggle handler) fails immediately at the source level. A
true behavioural test would require a JS test runner driving jsdom;
the project's CI image does not ship one, so we anchor on the same
static-source pattern used by ``tests/test_quartz_search_static.py``
and ``tests/test_explorer_filter.py``.

The contract these tests pin
----------------------------

- ``activeChipFilters`` is an object with separate per-dimension
  ``Set<string>`` fields for ``tier`` and ``source`` (NOT a single
  flat ``Set<string>`` shared across dimensions).
- The "all" chip's active state is computed per dimension from
  ``activeChipFilters[dimension].size === chipVocabularies[dimension].length``.
- Clicking a non-``all`` chip toggles only ``activeChipFilters[dimension]``;
  no handler ever calls ``activeChipFilters.add`` /
  ``activeChipFilters.delete`` /  ``activeChipFilters.has`` directly
  (which would imply flat-set semantics).
- Clicking the "all" chip resets only ``activeChipFilters[dimension]``
  to a fresh ``Set(chipVocabularies[dimension])`` — never touches a
  sibling dimension's set.
- The post-render filter pass (the loop that drops nodes whose
  tier/source isn't in the active filter) reads ``.tier`` and
  ``.source`` independently on the per-dimension shape.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_INLINE = (
    REPO_ROOT
    / "quartz_overrides"
    / "quartz"
    / "components"
    / "scripts"
    / "graph.inline.ts"
)


# ---------------------------------------------------------------------------
# Fixtures — read the inline script once per module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph_inline_source() -> str:
    """Read graph.inline.ts once per module."""
    assert GRAPH_INLINE.is_file(), f"missing inline script at {GRAPH_INLINE}"
    return GRAPH_INLINE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Data structure — `activeChipFilters` MUST be per-dimension
# ---------------------------------------------------------------------------


def test_active_chip_filters_is_per_dimension_object(graph_inline_source: str) -> None:
    """``activeChipFilters`` is declared as ``{ tier: Set; source: Set }``.

    The original bug hypothesis was a flat ``Set<string>`` shared
    across dimensions. The fix shape is a typed object with two
    separately-allocated ``Set<string>`` fields. The TS declaration is
    the canonical anchor — losing the type means a future refactor
    could silently flatten the structure.
    """
    pattern = re.compile(
        r"const\s+activeChipFilters\s*:\s*\{\s*"
        r"tier\s*:\s*Set<string>\s*;\s*"
        r"source\s*:\s*Set<string>\s*;?\s*\}\s*=\s*\{",
        re.MULTILINE,
    )
    assert pattern.search(graph_inline_source) is not None, (
        "expected `const activeChipFilters: { tier: Set<string>; "
        "source: Set<string> } = { ... }` declaration"
    )


def test_active_chip_filters_initialized_per_dimension(graph_inline_source: str) -> None:
    """Both ``tier`` and ``source`` initialise to a fresh full-vocabulary set.

    Default-on means "everything visible", which is the documented
    UX. Re-using the same Set instance across dimensions (e.g.
    ``tier: vocab; source: vocab``) would couple them — assert on the
    distinct ``new Set<string>(chipVocabularies.<dim>)`` constructor
    calls.
    """
    assert (
        "tier: new Set<string>(chipVocabularies.tier)" in graph_inline_source
    ), "expected `tier: new Set<string>(chipVocabularies.tier)` initialiser"
    assert (
        "source: new Set<string>(chipVocabularies.source)" in graph_inline_source
    ), "expected `source: new Set<string>(chipVocabularies.source)` initialiser"


def test_chip_vocabularies_pinned(graph_inline_source: str) -> None:
    """``chipVocabularies`` declares the tier + source value lists.

    The toggle handler iterates over ``chipVocabularies[dimension]``;
    if that table's shape changes (e.g. drops the ``tier`` key) the
    handler would silently render an empty row. Pin the contract.
    """
    assert "const chipVocabularies = {" in graph_inline_source, (
        "expected `const chipVocabularies = { ... }` table"
    )
    # The two dimensions referenced by the toggle handler must both
    # be present in the vocabulary table.
    assert re.search(
        r"tier\s*:\s*\[[^\]]*\"vault\"[^\]]*\"ingested\"[^\]]*\]",
        graph_inline_source,
    ), "expected `tier: [\"vault\", \"ingested\"]` in chipVocabularies"
    assert re.search(
        r"source\s*:\s*\[[^\]]*\"krisp\"[^\]]*\"slack\"[^\]]*"
        r"\"gmail\"[^\]]*\"manual\"[^\]]*\]",
        graph_inline_source,
    ), (
        "expected `source: [\"krisp\", \"slack\", \"gmail\", \"manual\"]` "
        "in chipVocabularies"
    )


# ---------------------------------------------------------------------------
# ALL chip — per-dimension active computation + per-dimension reset
# ---------------------------------------------------------------------------


def test_all_chip_active_is_dimension_scoped(graph_inline_source: str) -> None:
    """The ``all`` chip's active state is computed per dimension.

    Spec: "Each dimension's ``ALL`` chip is computed from
    ``activeChipFilters[dimension].size === vocabulary.size``." A flat
    structure would compute against a single ``activeChipFilters.size``
    that flips for every chip, which is the original bug.
    """
    pattern = re.compile(
        r"activeChipFilters\[dimension\]\.size\s*===\s*"
        r"chipVocabularies\[dimension\]\.length",
    )
    assert pattern.search(graph_inline_source) is not None, (
        "expected per-dimension ALL-active check "
        "`activeChipFilters[dimension].size === chipVocabularies[dimension].length`"
    )


def test_all_chip_click_resets_only_its_dimension(graph_inline_source: str) -> None:
    """Clicking ``all`` reassigns ``activeChipFilters[dimension]`` only.

    Spec: "Clicking ``ALL`` resets that dimension to its full
    vocabulary." The handler must rebuild ``activeChipFilters[dimension]``
    with ``new Set(chipVocabularies[dimension])`` and never touch any
    other dimension's set.
    """
    pattern = re.compile(
        r"activeChipFilters\[dimension\]\s*=\s*new Set<string>\(\s*"
        r"chipVocabularies\[dimension\]\s*,?\s*\)",
        re.DOTALL,
    )
    assert pattern.search(graph_inline_source) is not None, (
        "expected `activeChipFilters[dimension] = new Set<string>("
        "chipVocabularies[dimension])` in the ALL-chip click handler"
    )


# ---------------------------------------------------------------------------
# Non-ALL chip — toggles only inside its own dimension's set
# ---------------------------------------------------------------------------


def test_non_all_chip_click_uses_per_dimension_set(graph_inline_source: str) -> None:
    """Non-``all`` chip handler grabs ``activeChipFilters[dimension]`` first.

    Spec: "Clicking a non-``ALL`` chip toggles that chip in its
    dimension's set; never touches another dimension." The canonical
    handler shape is::

        const set = activeChipFilters[dimension]
        if (set.has(value)) {
          set.delete(value)
        } else {
          set.add(value)
        }

    The ``const set = ...`` line is the single source of truth for
    the dimension scoping; without it a stray ``activeChipFilters.add``
    would slip past review.
    """
    assert (
        "const set = activeChipFilters[dimension]" in graph_inline_source
    ), (
        "expected `const set = activeChipFilters[dimension]` binding "
        "inside the non-ALL chip click handler"
    )
    # And the toggle proper — has/delete/add against the per-dimension
    # set, NOT against `activeChipFilters` directly.
    pattern = re.compile(
        r"const set\s*=\s*activeChipFilters\[dimension\].*?"
        r"set\.has\(value\).*?"
        r"set\.delete\(value\).*?"
        r"set\.add\(value\)",
        re.DOTALL,
    )
    assert pattern.search(graph_inline_source) is not None, (
        "expected non-ALL chip click handler to bind "
        "`set = activeChipFilters[dimension]` then has/delete/add `value`"
    )


def test_no_flat_set_method_calls_on_active_chip_filters(
    graph_inline_source: str,
) -> None:
    """No code calls Set methods directly on ``activeChipFilters``.

    A bug regression would look like
    ``activeChipFilters.has(value)`` /
    ``activeChipFilters.add(value)`` /
    ``activeChipFilters.delete(value)`` — which only makes sense if
    ``activeChipFilters`` is itself a ``Set`` (the ORIGINAL bug
    shape). With the per-dimension object shape every Set call MUST
    go through ``activeChipFilters.<dim>`` or
    ``activeChipFilters[dimension]``. Anchor that.
    """
    forbidden = [
        # Direct method calls on the bare object — would only compile
        # if the type were `Set<string>`.
        "activeChipFilters.has(",
        "activeChipFilters.add(",
        "activeChipFilters.delete(",
        # And the assignment-of-membership variant.
        "activeChipFilters = new Set",
    ]
    for snippet in forbidden:
        assert snippet not in graph_inline_source, (
            f"forbidden flat-set method call `{snippet}` found — "
            "activeChipFilters must always be accessed per-dimension"
        )


# ---------------------------------------------------------------------------
# Render-time filter pass — `.tier` and `.source` are read independently
# ---------------------------------------------------------------------------


def test_render_filter_reads_tier_and_source_independently(
    graph_inline_source: str,
) -> None:
    """The post-render filter pass references both dimensions independently.

    The dimension-scoped data structure is only useful if the consumer
    also reads it per-dimension. The filter pass that drops nodes whose
    ``tier``/``source`` isn't in the active filter must hit
    ``activeChipFilters.tier`` AND ``activeChipFilters.source`` (NOT
    a flat ``activeChipFilters.has``).
    """
    assert "activeChipFilters.tier.size" in graph_inline_source, (
        "expected `activeChipFilters.tier.size` guard in node-filter pass"
    )
    assert "activeChipFilters.tier.has(nodeTier)" in graph_inline_source, (
        "expected `activeChipFilters.tier.has(nodeTier)` membership check"
    )
    assert "activeChipFilters.source.size" in graph_inline_source, (
        "expected `activeChipFilters.source.size` guard in node-filter pass"
    )
    assert "activeChipFilters.source.has(nodeSource)" in graph_inline_source, (
        "expected `activeChipFilters.source.has(nodeSource)` membership check"
    )


# ---------------------------------------------------------------------------
# End-to-end shape: the chip rendering loop is per-dimension
# ---------------------------------------------------------------------------


def test_chip_render_loop_iterates_filter_chips_list(
    graph_inline_source: str,
) -> None:
    """The chip rail is rendered by iterating ``filterChipsList``.

    The plan's "for each dimension's row" rendering pattern requires a
    loop over the configured dimensions. Without the loop the second
    dimension's row would be missing entirely.
    """
    assert "for (const dimension of filterChipsList)" in graph_inline_source, (
        "expected `for (const dimension of filterChipsList)` chip render loop"
    )


def test_chip_row_data_dimension_attribute_pinned(
    graph_inline_source: str,
) -> None:
    """Each rendered chip row carries ``data-dimension`` for QA hooks.

    Lets Playwright/MCP browser tests target a specific dimension's
    row without relying on label text. Lock the attribute name so a
    rename can't quietly break downstream selectors.
    """
    assert 'row.dataset["dimension"] = dimension' in graph_inline_source, (
        "expected `row.dataset[\"dimension\"] = dimension` on each chip row"
    )


# ---------------------------------------------------------------------------
# Behavioural simulation — replay the toggle logic on the per-dimension shape
# ---------------------------------------------------------------------------


class _DimensionScopedFilter:
    """Pure-Python re-implementation of the TS toggle contract.

    The TS handler shape is::

        // ALL chip click
        activeChipFilters[dimension] = new Set<string>(
          chipVocabularies[dimension],
        )

        // non-ALL chip click
        const set = activeChipFilters[dimension]
        if (set.has(value)) set.delete(value)
        else                 set.add(value)

    This class mirrors the same operations against a Python dict of
    sets so we can drive a deterministic toggle sequence and assert
    "clicking Tier ``vault`` does NOT flip Source ``ALL``" — the exact
    user-observed bug from the plan.

    NOT a monkey-patch. NOT a mock. A small parity model that lets us
    exercise the logic deterministically in CI without a JS runner.
    """

    def __init__(self) -> None:
        self.vocab: dict[str, list[str]] = {
            "tier": ["vault", "ingested"],
            "source": ["krisp", "slack", "gmail", "manual"],
        }
        self.state: dict[str, set[str]] = {
            "tier": set(self.vocab["tier"]),
            "source": set(self.vocab["source"]),
        }

    def click_value(self, dimension: str, value: str) -> None:
        """Mirror of the non-ALL chip click handler."""
        chip_set = self.state[dimension]
        if value in chip_set:
            chip_set.remove(value)
        else:
            chip_set.add(value)

    def click_all(self, dimension: str) -> None:
        """Mirror of the ALL chip click handler."""
        self.state[dimension] = set(self.vocab[dimension])

    def is_all_active(self, dimension: str) -> bool:
        """Mirror of the per-dimension ALL-active computation."""
        return len(self.state[dimension]) == len(self.vocab[dimension])


def test_clicking_tier_does_not_flip_source_all() -> None:
    """The exact user-observed bug: clicking Tier ``vault`` MUST keep Source ``ALL`` on.

    Reproduces the original report from the plan. With the
    dimension-scoped structure this assertion holds; with a flat set
    it would fail.
    """
    f = _DimensionScopedFilter()
    assert f.is_all_active("tier") is True
    assert f.is_all_active("source") is True

    f.click_value("tier", "vault")

    assert "vault" not in f.state["tier"], "vault should be deselected in tier"
    assert f.is_all_active("tier") is False, "tier ALL should now read inactive"
    # The bug is here — Source ALL must stay active.
    assert f.is_all_active("source") is True, (
        "Source ALL should remain active after a Tier toggle"
    )
    assert f.state["source"] == {"krisp", "slack", "gmail", "manual"}, (
        "Source set must be untouched by a Tier click"
    )


def test_clicking_source_krisp_does_not_flip_tier_all() -> None:
    """Mirror direction: a Source toggle MUST NOT touch Tier state."""
    f = _DimensionScopedFilter()
    f.click_value("source", "krisp")

    assert "krisp" not in f.state["source"]
    assert f.is_all_active("source") is False
    assert f.is_all_active("tier") is True, (
        "Tier ALL should remain active after a Source toggle"
    )
    assert f.state["tier"] == {"vault", "ingested"}


def test_clicking_tier_all_only_resets_tier() -> None:
    """ALL click resets only its own dimension; sibling state is untouched."""
    f = _DimensionScopedFilter()
    # Knock both dimensions off-ALL.
    f.click_value("tier", "vault")
    f.click_value("source", "krisp")
    assert f.is_all_active("tier") is False
    assert f.is_all_active("source") is False

    # Click Tier ALL: tier resets, source stays partial.
    f.click_all("tier")
    assert f.is_all_active("tier") is True
    assert f.state["tier"] == {"vault", "ingested"}
    assert f.is_all_active("source") is False, (
        "Source must remain partial after Tier ALL reset"
    )
    assert "krisp" not in f.state["source"]


def test_clicking_source_all_only_resets_source() -> None:
    """Mirror: clicking Source ALL leaves Tier partial state intact."""
    f = _DimensionScopedFilter()
    f.click_value("tier", "vault")
    f.click_value("source", "krisp")

    f.click_all("source")
    assert f.is_all_active("source") is True
    assert f.state["source"] == {"krisp", "slack", "gmail", "manual"}
    assert f.is_all_active("tier") is False
    assert "vault" not in f.state["tier"]


def test_clicking_chip_value_twice_re_adds_it() -> None:
    """Toggle is symmetric — second click on the same chip re-adds the value.

    Locks the has/delete/add (NOT delete-only) shape from the spec.
    """
    f = _DimensionScopedFilter()
    f.click_value("tier", "vault")
    assert "vault" not in f.state["tier"]

    f.click_value("tier", "vault")
    assert "vault" in f.state["tier"]
    assert f.is_all_active("tier") is True
