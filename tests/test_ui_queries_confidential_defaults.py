"""The ``exclude_confidential`` DEFAULTS in ``brain.ui.queries`` are asserted.

Four functions there default ``exclude_confidential=True`` (fail-closed) and
``tag_counts`` defaults ``False``. That asymmetry is deliberate and heavily
documented -- but documentation is not a guard, and "one function whose default
points the other way from every neighbour" is exactly the shape that invites a
future tidy-up to make them uniform without reading why.

This file converts the asymmetry from DOCUMENTED to ASSERTED. Flipping any of
these defaults now reddens a test whose message carries the reason, so the next
person to consider it is handed the argument rather than left to rediscover it.

**Why the odd one out is not flipped here.** ``tag_counts`` is the SEARCH-scoped
answer -- corpus-wide by contract, including drafts and generated pages, because
it annotates a search result set that also includes them.
``tests/test_ui_queries_discovery.py::test_tag_counts_still_counts_confidential_documents``
pins that as the deliberate CONTRAST to ``browseable_tag_counts``, so that "the
two scopes differ" is a tested claim rather than an assumed one. Flipping the
default would force that test's expectation to change with no change in the
contract it describes -- which is editing a test to silence a red, and would
delete the only assertion proving the two scopes are not the same query.

**And the default is not what protects anything.** Both production callers pass
the flag explicitly -- ``routes_meta.facets`` and ``routes_discovery`` each pass
``exclude_confidential=strict`` -- so the default governs only unflagged calls.
That is asserted below too, because it is the load-bearing half: if a route ever
stopped passing the flag, uniform defaults would not have saved it and this is
the fact worth guarding.

This differs from the polarity hazard in ``mcp_server``'s link tools, and the
distinction is the reason this file argues rather than edits. There, two
conventions with OPPOSITE names and OPPOSITE polarity (``include_confidential``
vs ``exclude_confidential``) meet across a module boundary and are bridged by a
``not`` that can silently vanish while every test stays green. Here the name and
polarity are identical (``exclude_confidential``, ``True`` = strict) and only the
default differs -- there is no inversion to drop and no silent-green failure
mode. A differing default is a documented scope difference; an inverted polarity
is a leak waiting to ship.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from brain.ui import queries as ui_queries

#: Functions whose unflagged call must EXCLUDE confidential rows.
FAIL_CLOSED = (
    "browseable_tag_counts",
    "documents_for_tag",
    "iter_tree_rows",
    "recent_documents",
)

#: The single deliberate exception. See this module's docstring.
SEARCH_SCOPED = "tag_counts"


def _default_for(fn: Callable[..., Any]) -> Any:
    param = inspect.signature(fn).parameters.get("exclude_confidential")
    assert param is not None, f"{fn.__name__} has no exclude_confidential parameter"
    return param.default


def test_browse_surfaces_default_to_excluding_confidential_rows() -> None:
    """The four fail-closed defaults, asserted individually by name."""
    permissive = [
        name for name in FAIL_CLOSED if _default_for(getattr(ui_queries, name)) is not True
    ]
    assert not permissive, (
        f"these browse-scoped queries must default exclude_confidential=True: "
        f"{permissive}. They back surfaces that paint before the reader asks for "
        f"anything, so an unflagged call must fail closed."
    )


def test_tag_counts_default_is_deliberately_permissive() -> None:
    """The odd one out, and the reason, in the failure message.

    If you are reading this because you flipped it: the change you want is
    probably a new browse-scoped function (``browseable_tag_counts`` already
    exists), not a narrowing of this one.
    """
    assert _default_for(getattr(ui_queries, SEARCH_SCOPED)) is False, (
        "tag_counts must keep defaulting exclude_confidential=False. It is the "
        "SEARCH-scoped count -- corpus-wide by contract -- and "
        "test_tag_counts_still_counts_confidential_documents pins that as the "
        "deliberate contrast to browseable_tag_counts. Flipping this default "
        "changes a documented contract and deletes the only assertion proving "
        "the two scopes differ. If you need a browse-scoped count, call "
        "browseable_tag_counts instead."
    )


def test_both_production_callers_pass_the_flag_explicitly() -> None:
    """The load-bearing half: no route relies on any of these defaults.

    Asserted from source rather than by calling the routes, because the claim is
    about the CALL SITES -- a route that silently stopped passing the flag is
    the failure this guards, and executing it would not reveal that.
    """
    import inspect as _inspect

    from brain.ui import routes_discovery, routes_meta

    # Whitespace-insensitive: collapse runs of space/newline so a reformat of
    # the call site does not read as a missing flag.
    def _flat(mod: Any) -> str:
        return " ".join(_inspect.getsource(mod).split())

    meta_src = _flat(routes_meta)
    disc_src = _flat(routes_discovery)

    assert "tag_counts( conn, min_doc_count=1, exclude_confidential=strict )" in meta_src, (
        "routes_meta.facets must pass exclude_confidential explicitly; the "
        "permissive default of tag_counts is not a protection"
    )
    assert "browseable_tag_counts( conn, exclude_confidential=strict )" in disc_src, (
        "routes_discovery must pass exclude_confidential explicitly"
    )
