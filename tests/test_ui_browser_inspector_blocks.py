"""The ORDER of the three blocks after the note in ``#inspector``, EXECUTED.

Three modules draw a block into ``#inspector`` after the note's own children:
``js/marginalia.js`` (``aside.marginalia``), ``js/graph.js``
(``figure.local-graph``) and ``js/related.js`` (``nav.related-rail``). Each
re-renders when ITS OWN fetch resolves, so with a bare ``host.appendChild``
the order on screen was the order the network answered in — a late ``/links``
response put the marginalia below the graph.

The order is now defined once, in ``js/dom.js`` (``placeInspectorBlock``):
marginalia -> graph -> related (each only if present), all after the note's
head and body. This file exercises EVERY arrival order of the three fetches, by
parking each response (``_HOLD``) and releasing them in turn. The refresh after
a save is ``tests/test_ui_browser_refresh.py``.

Same construction and the same harness as the graph and related suites
(``tests/ui_graph_harness.py``); everything is reached through ``boot()``.
Every id, title and snippet is synthetic. Run it by path:

    pytest tests/test_ui_browser_inspector_blocks.py -m browser --no-cov
"""
from __future__ import annotations

import itertools
from typing import Any

import pytest

from tests.ui_graph_harness import (
    _ERRORS,
    _HOLD,
    KINDS,
    ROOT_ID,
    _dispatch,
    _page_fixture,  # noqa: F401 — registers the `page` fixture
    _serve_related_fixture,  # noqa: F401 — registers `serve_related`
    _static_origin_fixture,  # noqa: F401 — registers `static_origin`, which `page` uses
    assert_canonical_blocks,
    inspector_children,
    release_held,
    start_open,
    wait_for_held,
)

pytestmark = [pytest.mark.browser, pytest.mark.usefixtures("serve_related")]


# ---------------------------------------------------------- one order (B) --


@pytest.mark.parametrize(
    "order", list(itertools.permutations(KINDS)), ids=lambda o: "-".join(o),
)
def test_blocks_keep_one_order_whatever_order_their_fetches_land(
    page: Any, order: tuple[str, ...],
) -> None:
    """All six arrival orders end in marginalia -> graph -> related."""
    _HOLD.update(KINDS)
    start_open(page, ROOT_ID)
    page.wait_for_selector(".note-body")
    wait_for_held(page, len(KINDS))

    release_held(page, list(order))
    page.wait_for_selector("#inspector > nav.related-rail")
    page.wait_for_selector("#inspector > .marginalia .backlinks-rail a")
    assert_canonical_blocks(inspector_children(page), f"after arrival order {order}")

    # And a plain dispatch re-renders from the caches into the same order.
    _dispatch(page, "{}")
    assert_canonical_blocks(inspector_children(page), f"after a dispatch following {order}")
    assert _ERRORS == [], f"uncaught errors: {_ERRORS}"
