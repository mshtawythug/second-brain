"""The `brain ui` front end, EXECUTED in a real browser.

This is the instrument the Pre-0(b) review loop concluded it was missing. Two
of its three documented gaps were blocked on the same absence, not on two
separate problems:

* the whole ARIA layer — ``role``, ``aria-selected``, ``aria-level``,
  ``aria-current`` and above all ``aria-owns`` — was verified only as *strings
  present in a source file*, because nothing here could observe an
  accessibility tree;
* the DOM half of the keyboard fix (``renderTree``, ``buildBranch``,
  ``applyRovingTabindex``, ``focusTreeItem``, ``onTreeKeydown``) was verified
  only structurally, because nothing here *executed* it.

Rendering the rail is what produces the tree, so one harness reaches both.

``aria-owns`` is tested first and deliberately: it is the largest unverified
claim in the change set, because unlike every other ARIA attribute it **changed
the shape of the accessibility tree** — re-parenting each folder's ``role=group``
under its treeitem, which ``<li role="none">`` had severed.

WHY IT IS HERMETIC. The API is stubbed at the network layer rather than served
by a real app: no Postgres, no Ollama, no uvicorn, and — importantly on this box
— no contention for the machine-wide test-database lock. What runs is the REAL
``index.html``, the four ``css/`` files, the eight ``js/`` modules and
``tree_nav.js`` off disk.

That paragraph was FALSE when first written, and is now true by construction
rather than by assertion. ``tests/conftest.py`` has two session-scoped autouse
fixtures — ``_exclusive_test_database`` and ``_ensure_test_db_initialized`` —
which fire for *any* selection, so ``-m browser`` opened a real connection,
reset the schema and took the machine-wide advisory lock before a single test
ran. Pointed at an unreachable database, all 26 ERRORed in setup; while another
suite held the lock, the session refused to start.

Both fixtures now consult ``_session_touches_the_database`` and skip when every
collected item carries the ``browser`` marker. MEASURED both directions:

* ``TEST_DATABASE_URL=…@127.0.0.1:1/does_not_exist pytest tests/test_ui_browser.py
  -m browser`` -> **26 passed**;
* the same unreachable URL on a NON-browser selection -> still ERRORs at setup,
  so the opt-out has not quietly disabled the schema reset for everyone else.

This matters beyond tidiness: it is the difference between a CI gate that needs
a cached chromium and one that needs a live Postgres *and* a cached chromium.

WHAT ARRIVED WITH THE JS SPLIT. ``app.js`` was split into ``js/``, which broke
every guard in ``tests/test_ui_static_behaviour.py`` that read it as a source
STRING. Those were not repaired at their new addresses — a substring assertion
whose only oracle is the implementation it reads is a change-detector, and
re-anchoring one is how a dead guard survives a refactor looking healthy. Each
was instead asked "what would the user notice if this were wrong?", and where
that question had an answer the answer is a test in the LAST section of this
file. Where it had none — the CSP shape, the single-``.innerHTML`` rule, the
no-external-URL rule, and the anti-erosion invariants the harness provably
cannot express — the source guard stayed, re-homed to its new module.

HOW IT IS GATED. It needs the ``browser`` extra and a cached chromium, so in the
DEFAULT suite it would skip — and a skip reads exactly like a pass. ``addopts``
therefore deselects the marker, and a dedicated ``browser`` job in
.github/workflows/ci.yml installs both and runs it on every PR:

    pytest tests/test_ui_browser*.py -m browser --no-cov

**The path is part of the command, not decoration.** A bare ``pytest -m browser``
still COLLECTS the whole suite, and marker deselection happens after collection —
so ``tests/test_restore_gate.py`` and ``tests/test_restore_swap.py``, which open
a database connection at IMPORT time, fail collection before any marker is
consulted:

    ERROR collecting tests/test_restore_gate.py — psycopg.OperationalError
    Interrupted: 2 errors during collection

That makes the "no Postgres" claim below true only when the path is named. The
session-level opt-out in ``conftest._session_touches_the_database`` cannot help:
it is a fixture, and fixtures run long after import. Two prior verifications of
this harness passed because both happened to name the file.

The CI job names a GLOB rather than this file, so a browser suite added later is
picked up without editing the workflow. ``tests/test_ci_workflow.py`` pins that:
it discovers every module applying the ``browser`` marker — by marker, not by
filename, since a filename oracle would agree with the workflow's own glob and
both would go blind together — and fails if any of them sits outside the CI
selection. So this harness is now **gate-enforced, not merely verified on
demand**; the two were different claims, and the gap between them was the point.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from brain.ui.app import static_dir

pytestmark = pytest.mark.browser

#: A vault with the shape that matters: a CLOSED folder, an OPEN folder holding
#: a nested folder and two notes, and a note at the root. Synthetic throughout —
#: no real titles, paths or ids.
#: T16 EXTENSION — every node now carries the tier-split counts
#: ``TreeNode.to_payload`` emits (``vault_count + ingested_count ==
#: note_count`` at every node, recursive over descendants), and an
#: ``_ingested/krisp`` feed folder.
#:
#: ``_ingested`` is FIRST because the server sorts folders case-insensitively
#: and ``_`` (0x5F) precedes ``a`` (0x61) — the seed must match the order the
#: real payload arrives in, or the rail's index-based tests would be asserting
#: against a shape the server never produces.
#:
#: **The ingested subtree is invisible by default**, because the "show ingested"
#: toggle defaults OFF. That is what lets this seed grow without disturbing the
#: tree tests written before it: they see exactly the tree they always did.
#:
#: The krisp titles sort DIFFERENTLY from their dates — alphabetically Budget,
#: Roadmap, Vendor; by date Vendor (Mar 15), Roadmap (Mar 3), Budget (Feb 20).
#: The server sends notes in title order, so a month grouping that failed to
#: reorder would be caught rather than accidentally agreeing.
TREE: dict[str, Any] = {
    "count": 9,
    "name": "",
    "path": "",
    "empty_hint": "Nothing here yet.",
    "note_count": 9, "vault_count": 5, "ingested_count": 4,
    "children": [
        {
            "name": "_ingested", "path": "_ingested",
            "note_count": 3, "vault_count": 0, "ingested_count": 3,
            "children": [
                {
                    "name": "krisp", "path": "_ingested/krisp", "children": [],
                    "note_count": 3, "vault_count": 0, "ingested_count": 3,
                    "notes": [
                        {"id": "k-budget", "title": "Budget Check",
                         "path": "_ingested/krisp/2026-02-20-budget.md",
                         "draft": False, "tier": "ingested",
                         "date": "2026-02-20"},
                        {"id": "k-roadmap", "title": "Roadmap Review",
                         "path": "_ingested/krisp/2026-03-03-roadmap.md",
                         "draft": False, "tier": "ingested",
                         "date": "2026-03-03"},
                        {"id": "k-vendor", "title": "Vendor Sync",
                         "path": "_ingested/krisp/2026-03-15-vendor.md",
                         "draft": False, "tier": "ingested",
                         "date": "2026-03-15"},
                    ],
                },
            ],
            "notes": [],
        },
        {
            "name": "archive", "path": "archive", "children": [],
            "note_count": 1, "vault_count": 1, "ingested_count": 0,
            "notes": [{"id": "n-buried", "title": "Buried Note",
                       "path": "archive/buried.md", "draft": False,
                       "tier": "vault", "date": "2026-01-02"}],
        },
        {
            # THE DISCRIMINATING FOLDER. "projects" holds BOTH tiers, so it
            # SURVIVES the ingested filter while its count must still change:
            # 4 with ingested shown, 3 without. Every other folder here is
            # single-tier and therefore either vanishes whole or never moves —
            # against those, a badge wired to the unfiltered `note_count` would
            # read correctly in both modes and the declared mutation could not
            # redden. One mixed folder is what makes the oracle bite.
            "name": "projects", "path": "projects",
            "note_count": 4, "vault_count": 3, "ingested_count": 1,
            "children": [
                {"name": "q3", "path": "projects/q3", "children": [],
                 "note_count": 1, "vault_count": 1, "ingested_count": 0,
                 "notes": [{"id": "n-deep", "title": "Deep Note",
                            "path": "projects/q3/deep.md", "draft": False,
                            "tier": "vault", "date": "2026-01-03"}]},
            ],
            "notes": [
                {"id": "n-alpha", "title": "Alpha Note",
                 "path": "projects/alpha.md", "draft": False,
                 "tier": "vault", "date": "2026-01-04"},
                {"id": "n-beta", "title": "Beta Note",
                 "path": "projects/beta.md", "draft": True,
                 "tier": "vault", "date": "2026-01-05"},
                {"id": "p-filed", "title": "Filed Transcript",
                 "path": "projects/filed.md", "draft": False,
                 "tier": "ingested", "date": "2026-01-07"},
            ],
        },
    ],
    "notes": [{"id": "n-root", "title": "Root Note", "path": "root.md",
               "draft": False, "tier": "vault", "date": "2026-01-06"}],
}

#: Two ledger rows, enough to assert "exactly ONE row is aria-current". Both ids
#: exist in TREE, so selecting one is also observable in the rail.
SEARCH: dict[str, Any] = {
    "results": [
        {"id": "n-root", "title": "Root Note", "snippet": "a synthetic snippet",
         "source_kind": "manual", "date": "2026-01-06", "tags": ["alpha", "beta"]},
        {"id": "n-alpha", "title": "Alpha Note", "snippet": "another snippet",
         "source_kind": "krisp", "date": "2026-01-04", "tags": []},
    ],
    "total_documents": 2,
    "timing_ms": {"total": 120, "embed": 40, "sql": 80},
    "session_id": "s-synthetic",
}

_STUBS: dict[str, Any] = {
    "/api/health": {"status": "ok", "read_only": False, "notices": []},
    "/api/tree": TREE,
    "/api/facets": {"sources": [], "content_types": [], "tags": []},
    "/api/search": SEARCH,
}


@pytest.fixture(scope="module")
def static_origin() -> Iterator[str]:
    """Serve the REAL static directory over HTTP.

    Over http:// rather than file:// because ES module imports are subject to
    CORS and a file:// origin cannot satisfy them — the app would silently fail
    to boot, which is precisely the failure this harness exists to catch.
    """
    # Serve the PACKAGE dir, not the static dir: index.html references its
    # assets as absolute `/static/...` paths (that is what the real server
    # mounts), so serving static/ at the root 404s every one of them and the
    # page boots blank — which is exactly how this failed the first time.
    handler = partial(
        SimpleHTTPRequestHandler, directory=str(static_dir().parent)
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def page(static_origin: str) -> Iterator[Any]:
    """A booted app page with every API call stubbed."""
    sync_api = pytest.importorskip(
        "playwright.sync_api",
        reason=(
            "Playwright not installed — `pip install -e \".[browser]\"` "
            "(browsers cache outside the venv, so this is a small install)"
        ),
    )

    def route_api(route: Any) -> None:
        path = route.request.url.split("127.0.0.1:")[-1].split("/", 1)[-1]
        body = _STUBS.get("/" + path.split("?")[0])
        if body is None:
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        route.fulfill(
            status=200, body=json.dumps(body), content_type="application/json"
        )

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page()
            pg.route("**/api/**", route_api)
            pg.goto(f"{static_origin}/static/index.html")
            # The rail is rendered by an async boot; wait for a real treeitem
            # rather than a timeout.
            # Short timeout on purpose: if the app did not boot, that is a
            # blank page and waiting 30s to learn it wastes a run.
            pg.wait_for_selector('[role="treeitem"]', timeout=8000)
            yield pg
        finally:
            browser.close()


def _expand_first_folder(page: Any) -> Any:
    """Open a folder and return it. ``aria-owns`` only exists once open.

    That is correct product behaviour — a collapsed folder has no rendered
    ``role="group"`` to own — and the first version of these tests asserted
    ``aria-owns`` on a freshly booted rail where nothing was expanded, so they
    failed against correct code.
    """
    folder = page.locator('[role="treeitem"][aria-expanded="false"]').first
    folder.focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_selector('[role="treeitem"][aria-expanded="true"]')
    return page.locator('[role="treeitem"][aria-expanded="true"]').first


def _focused_label(page: Any) -> str:
    """The focused item's label WITHOUT the twisty glyph or the count badge.

    The glyph flips when a folder opens, and a re-render replaces the DOM node
    entirely — so neither raw ``textContent`` nor a data-attribute marker
    survives as an identity. The label text does.

    THE COUNT BADGE IS EXCLUDED FOR THE SAME REASON, and T16 is what added it.
    ``.folder-count`` is an adornment INSIDE the treeitem, so raw
    ``textContent`` reads ``"projects3"`` and every identity comparison here
    would be asserting the count as well as the name —
    ``test_arrow_down_skips_over_a_collapsed_folders_children`` failed exactly
    that way, on ``'projects3' == 'projects'``, when the badge landed.

    Stripping it is not papering over the change. The badge carries
    ``aria-hidden="true"``, so it is ALREADY absent from the element's
    accessible name, and that name is what this helper stands in for. The node
    is CLONED before the badge is removed, so the live tree the assertions run
    against is never modified.
    """
    return page.evaluate(
        """() => {
            const clone = document.activeElement.cloneNode(true);
            clone.querySelectorAll(".folder-count").forEach((n) => n.remove());
            return clone.textContent.replace(/[\u25b8\u25be]/g, "").trim();
        }"""
    )


# ------------------------------------------------------------- aria-owns --


def test_the_app_boots_at_all(page: Any) -> None:
    """Guard the guard: every assertion below is vacuous if boot failed.

    A mistyped ES module specifier renders a blank page with no error, so this
    asserts the rail actually populated before anything else reads it.
    """
    assert page.locator('[role="treeitem"]').count() >= 3
    assert page.locator('[role="tree"]').count() == 1


def test_folder_treeitems_own_their_subtree(page: Any) -> None:
    """THE largest unverified claim: ``aria-owns`` re-parents the group.

    ``<li role="none">`` strips containment, so without ``aria-owns`` a folder's
    ``role="group"`` is a SIBLING of its treeitem in the accessibility tree and
    the children announce set position across two levels. This asserts the
    IDREF resolves to a real element that is the folder's own group.
    """
    _expand_first_folder(page)
    owners = page.locator('[role="treeitem"][aria-owns]')
    assert owners.count() >= 1, "an OPEN folder treeitem declares no aria-owns"

    for i in range(owners.count()):
        owner = owners.nth(i)
        owned_id = owner.get_attribute("aria-owns")
        target = page.locator(f'[id="{owned_id}"]')
        assert target.count() == 1, (
            f"aria-owns={owned_id!r} resolves to {target.count()} elements; an "
            "IDREF that matches 0 announces nothing and one that matches 2+ "
            "makes the SECOND folder claim the FIRST folder's subtree"
        )
        assert target.get_attribute("role") == "group", (
            "aria-owns must point at the nested role=group, not at some other node"
        )


def test_every_aria_owns_id_is_unique_in_the_document(page: Any) -> None:
    """The collision case, asserted against the real DOM.

    The ids were once slugified from the folder path, so ``q3-planning`` and
    ``q3 planning`` collapsed onto one id and two non-ASCII names both became
    the empty stem. Duplicate ids are legal HTML and silently resolve to the
    first match — a *false* parent/child, worse than the missing one.
    """
    _expand_first_folder(page)
    ids = page.eval_on_selector_all(
        "[aria-owns]", "els => els.map(e => e.getAttribute('aria-owns'))"
    )
    assert ids, "no aria-owns attributes found at all"
    assert len(ids) == len(set(ids)), f"duplicate aria-owns IDREFs: {ids}"

    all_ids = page.eval_on_selector_all(
        "[id]", "els => els.map(e => e.id)"
    )
    dupes = {i for i in all_ids if all_ids.count(i) > 1}
    assert not dupes, f"duplicate element ids in the document: {dupes}"


def test_the_computed_aria_tree_nests_notes_under_their_folder(page: Any) -> None:
    """The aria-owns PAYOFF, asserted as NESTING rather than presence.

    An earlier version of this test asserted only that a ``group`` appeared
    somewhere in the snapshot — and it PASSED with ``aria-owns`` deleted from
    the product, because ``<li role="none">`` still leaves the group in the tree;
    what ``aria-owns`` changes is its PARENT. Presence was never the property.

    With the re-parenting, Playwright's computed snapshot indents the child
    under its folder::

        - treeitem "▾ archive" [expanded] [level=1]:
          - group:
            - treeitem "Buried Note" [level=2]

    Without it, that child sits at the folder's own depth and a screen reader
    announces set position across two levels.
    """
    _expand_first_folder(page)
    snapshot = page.locator('[role="tree"]').aria_snapshot()
    lines = snapshot.splitlines()

    def indent(line: str) -> int:
        return len(line) - len(line.lstrip())

    opened = next((i for i, ln in enumerate(lines) if "[expanded]" in ln), None)
    assert opened is not None, f"no expanded treeitem in the tree:\n{snapshot}"

    # Playwright appends ":" only to a node that HAS children. Measured both
    # ways: with aria-owns the folder line ends ":" and the group sits at a
    # deeper indent; without it the folder line has no ":" and the group is a
    # SIBLING at the same indent. An earlier version of this test looked for any
    # deeper treeitem — which is true in BOTH cases, because the child is nested
    # under the sibling group. Containment, not depth, is the property.
    assert lines[opened].rstrip().endswith(":"), (
        "the expanded folder has NO children in the COMPUTED tree — its group "
        "is a sibling, so aria-owns is not re-parenting it and children "
        f"announce set position across two levels:\n{snapshot}"
    )
    nested_group = [
        ln for ln in lines[opened + 1:]
        if indent(ln) > indent(lines[opened]) and "group" in ln
    ]
    assert nested_group, f"no group nested under the folder:\n{snapshot}"


# --------------------------------------------------- the DOM half of Fix 1 --


def test_exactly_one_treeitem_is_tabbable(page: Any) -> None:
    """The roving tabindex, measured on real elements.

    Zero tabbable items is the original WCAG 2.1.1 trap; more than one defeats
    the point of the pattern.
    """
    tabbables = page.eval_on_selector_all(
        '[role="treeitem"]', "els => els.filter(e => e.tabIndex === 0).length"
    )
    assert tabbables == 1, f"{tabbables} treeitems are tabbable, expected exactly 1"


def test_arrow_down_moves_focus_between_visible_items(page: Any) -> None:
    """ArrowDown actually moves DOM focus — not just returns an action object."""
    first = page.locator('[role="treeitem"]').first
    first.focus()
    before = page.evaluate("document.activeElement.textContent")
    page.keyboard.press("ArrowDown")
    after = page.evaluate("document.activeElement.textContent")
    assert after != before, "ArrowDown did not move focus"
    assert page.evaluate(
        "document.activeElement.getAttribute('role')"
    ) == "treeitem", "focus left the tree"


def test_arrow_right_expands_and_focus_survives_that_rerender(page: Any) -> None:
    """APG behaviour AND H-1 focus survival, in one real interaction.

    Expanding calls ``setFolderOpen`` -> ``renderTree``, which wipes the host
    and rebuilds every node. So this asserts two things at once: focus does not
    move to the child (the APG rule), and focus is not lost to ``<body>`` by the
    re-render that opening causes.

    Identity is the label text with the twisty stripped — the glyph flips on
    open, and the DOM node itself is replaced, so neither raw text nor a
    data-attribute marker survives.
    """
    folder = page.locator('[role="treeitem"][aria-expanded="false"]').first
    folder.focus()
    before = _focused_label(page)
    page.keyboard.press("ArrowRight")
    page.wait_for_selector('[role="treeitem"][aria-expanded="true"]')

    assert _focused_label(page) == before, (
        "focus left the folder on ArrowRight — either it descended (the APG "
        "says a second press does that) or the re-render dropped it"
    )
    assert page.evaluate(
        "document.activeElement.getAttribute('aria-expanded')"
    ) == "true", "focus is not on the now-open folder"


def test_arrow_left_collapses_and_focus_survives_that_rerender(page: Any) -> None:
    """The same property in the other direction, through a second re-render."""
    folder = _expand_first_folder(page)
    folder.focus()
    before = _focused_label(page)
    page.keyboard.press("ArrowLeft")
    page.wait_for_selector('[role="treeitem"][aria-expanded="false"]')

    assert _focused_label(page) == before, (
        "focus did not survive the collapse re-render"
    )


def test_modified_arrows_are_left_to_the_browser(page: Any) -> None:
    """Cmd/Ctrl/Alt + Arrow belongs to the OS, not the tree."""
    first = page.locator('[role="treeitem"]').first
    first.focus()
    before = page.evaluate("document.activeElement.textContent")
    page.keyboard.press("Meta+ArrowDown")
    assert page.evaluate("document.activeElement.textContent") == before, (
        "Cmd+ArrowDown moved tree focus; it should scroll the page instead"
    )


def test_aria_selected_is_present_on_every_treeitem(page: Any) -> None:
    """On role=tree, a missing aria-selected reads as 'not selectable'."""
    missing = page.eval_on_selector_all(
        '[role="treeitem"]',
        "els => els.filter(e => e.getAttribute('aria-selected') === null).length",
    )
    assert missing == 0, f"{missing} treeitems carry no aria-selected"


# ------------------------------------------- CSS behaviour, as real oracles --
#
# These replace source-string guards with measurements. Each was previously
# asserted by grepping app.css; here the browser resolves the cascade and we
# read what the user actually gets. They are also the before/after oracle for
# the CSS split: identical numbers before and after mean the refactor is
# behaviour-preserving in the only sense that matters.


def _open_editor(page: Any) -> Any:
    """Open a note and switch to edit mode, returning the textarea."""
    page.route(
        "**/api/notes/**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": "n-root", "title": "Root Note", "tier": "vault",
                "content_type": "note", "draft": False, "tags": [],
                "source_kind": "manual", "vault_path": "root.md",
                "ingested_at": None, "editable": True, "movable": True,
                "body": "line\n", "body_hash": "sha256:x",
                "html": "<p>line</p>",
            }),
        ),
    )
    page.locator('[role="treeitem"]', has_text="Root Note").first.click()
    page.wait_for_selector(".note-bar")
    page.locator("button", has_text="Edit").first.click()
    page.wait_for_selector("textarea.editor")
    return page.locator("textarea.editor")


def test_the_reading_measure_stays_in_the_classical_band(page: Any) -> None:
    """`--measure: 48ch` must resolve to 45-75 characters, not 92-101.

    The old guard asserted the literal string `48ch`. This measures the rendered
    column against the font actually in use, which is the property that was
    wrong at 68ch.
    """
    width = page.evaluate("""() => {
        const probe = document.createElement('div');
        probe.className = 'note-body';
        probe.style.cssText = 'position:absolute;visibility:hidden';
        probe.textContent = 'x';
        document.body.appendChild(probe);
        const px = parseFloat(getComputedStyle(probe).maxWidth);
        const cs = getComputedStyle(probe);
        // Average advance of running prose ~0.5em is too crude; measure a real
        // lowercase alphabet instead.
        const c = document.createElement('canvas').getContext('2d');
        c.font = `${cs.fontSize} ${cs.fontFamily}`;
        const avg = c.measureText('abcdefghijklmnopqrstuvwxyz ').width / 27;
        probe.remove();
        return px / avg;
    }""")
    # 75, not 80. The docstring and the message below both cite the classical
    # 45-75 band while the assertion used to accept up to 80 — a test whose
    # stated claim and enforced claim differed by five characters. MEASURED at
    # 48ch: **60.18 characters**, so the honest bound passes with 15 to spare
    # and the loose one was buying nothing.
    assert 45 <= width <= 75, (
        f"the reading column renders ~{width:.0f} characters; the classical "
        "band is 45-75 and 68ch measured 92-101"
    )


def test_color_scheme_is_declared_so_native_furniture_follows(page: Any) -> None:
    """Scrollbars, date pickers and the caret come from `color-scheme`."""
    scheme = page.evaluate(
        "getComputedStyle(document.documentElement).colorScheme"
    )
    assert scheme in {"light", "dark"}, (
        f"color-scheme resolves to {scheme!r}; native furniture will be painted "
        "for the OS theme rather than the app's"
    )


def test_the_editor_fills_the_pane_and_a_resize_drag_wins(page: Any) -> None:
    """Fix 6 + N2, measured — the pair that source guards could not settle.

    `flex: 1 1 auto` made `resize: vertical` inert because an explicit height is
    only the flex BASE size. Grid honours it. This asserts both halves.
    """
    area = _open_editor(page)
    filled = area.bounding_box()["height"]
    assert filled > 300, f"the editor does not fill the pane ({filled:.0f}px)"

    # ABOVE the 26rem floor. `min-height: 26rem` computes to 416px and is
    # deliberate, so a drag to 180px is clamped BY DESIGN — the first version of
    # this test asserted against the product's intended behaviour and failed on
    # correct code.
    page.evaluate("document.querySelector('textarea.editor').style.height='600px'")
    dragged = area.bounding_box()["height"]
    assert abs(dragged - 600) < 2, (
        f"a resize to 600px rendered {dragged:.0f}px — the drag was overridden, "
        "which is what flex-grow did before the grid layout"
    )

    # And the floor itself still holds, which is the other half of the contract.
    page.evaluate("document.querySelector('textarea.editor').style.height='100px'")
    floored = area.bounding_box()["height"]
    assert abs(floored - 416) < 2, (
        f"a drag below the 26rem floor rendered {floored:.0f}px; min-height "
        "should clamp it to 416px"
    )


def test_the_withheld_notice_does_not_stretch(page: Any) -> None:
    """N8, measured: a one-line notice must not become a full-height block."""
    page.route(
        "**/api/notes/**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "id": "n-root", "title": "Root Note", "tier": "vault",
                "content_type": "note", "draft": False, "tags": [],
                "source_kind": "manual", "vault_path": "root.md",
                "ingested_at": None, "editable": False, "movable": False,
                "body": None, "html": "",
                "withheld": "body withheld: sensitivity=confidential",
            }),
        ),
    )
    page.locator('[role="treeitem"]', has_text="Root Note").first.click()
    page.wait_for_selector(".withheld")
    box = page.locator(".withheld").bounding_box()
    assert box["height"] < 120, (
        f"the withheld notice rendered {box['height']:.0f}px tall — it is "
        "stretching to the 1fr track instead of taking its natural height"
    )


def test_quiet_text_meets_AA_against_its_own_background(page: Any) -> None:
    """FIX 9, measured as CONTRAST rather than as a token name.

    The old guard asserted which CSS variable each rule referenced. This
    computes the actual ratio from the resolved colours, so it stays true if the
    palette is re-cut.
    """
    ratio = page.evaluate("""() => {
        const el = document.querySelector('.meta-sub') || document.querySelector('.gutter');
        if (!el) return null;
        // getComputedStyle returns `oklch(0.48 0.01 60)` here, NOT rgb(). Parsing
        // it as three rgb numbers yielded a 1.06:1 ratio from garbage. Painting
        // into a canvas makes the browser do the colour-space conversion.
        const parse = css => {
            const c = document.createElement('canvas').getContext('2d');
            c.fillStyle = css; c.fillRect(0, 0, 1, 1);
            return Array.from(c.getImageData(0, 0, 1, 1).data).slice(0, 3);
        };
        const lum = ([r,g,b]) => {
            const f = v => { v/=255; return v<=0.03928 ? v/12.92 : ((v+0.055)/1.055)**2.4; };
            return 0.2126*f(r) + 0.7152*f(g) + 0.0722*f(b);
        };
        let bgEl = el, bg = 'rgba(0, 0, 0, 0)';
        while (bgEl && (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent')) {
            bg = getComputedStyle(bgEl).backgroundColor; bgEl = bgEl.parentElement;
        }
        const L1 = lum(parse(getComputedStyle(el).color));
        const L2 = lum(parse(bg));
        const [hi, lo] = L1 > L2 ? [L1, L2] : [L2, L1];
        return (hi + 0.05) / (lo + 0.05);
    }""")
    assert ratio is not None, "no quiet-text element found to measure"
    assert ratio >= 4.5, (
        f"quiet text renders at {ratio:.2f}:1 against its own background; "
        "WCAG AA for body text is 4.5:1"
    )


# ------------------------------- the guards the JS split converted, EXECUTED --
#
# Each test below replaces a source-string guard that read `app.js` and could
# not survive the split into `js/`. They are grouped by the guard they retire so
# the trade is auditable rather than a matter of trust; the guards that had NO
# behavioural expression are named in test_ui_static_behaviour.py and stayed
# there as source assertions.


#: The note payload `_open_editor` and the caret tests share. Synthetic.
NOTE: dict[str, Any] = {
    "id": "n-root", "title": "Root Note", "tier": "vault",
    "content_type": "note", "draft": False, "tags": [],
    "source_kind": "manual", "vault_path": "root.md",
    "ingested_at": None, "editable": True, "movable": True,
    "body": "line\n", "body_hash": "sha256:x",
    "html": "<p>line</p>",
}


def _open_editor_with_save(page: Any, save_status: int, save_body: dict) -> Any:
    """Open the editor with a PUT that fails, so the FAILED-save path is real.

    One handler dispatching on method, rather than two overlapping routes and a
    `route.fallback()`: the ordering rules for overlapping Playwright routes are
    exactly the kind of thing that makes a test pass for the wrong reason.
    """
    def route_note(route: Any) -> None:
        if route.request.method == "PUT":
            route.fulfill(status=save_status, content_type="application/json",
                          body=json.dumps(save_body))
            return
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(NOTE))

    page.route("**/api/notes/**", route_note)
    page.locator('[role="treeitem"]', has_text="Root Note").first.click()
    page.wait_for_selector(".note-bar")
    page.locator("button", has_text="Edit").first.click()
    page.wait_for_selector("textarea.editor")
    return page.locator("textarea.editor")


def _caret(page: Any) -> int:
    return page.evaluate(
        "document.querySelector('textarea.editor').selectionStart"
    )


def _type_at(page: Any, offset: int, text: str) -> None:
    """Put the caret at `offset` and type — the only way to observe a rebuild.

    A rebuilt <textarea> is re-created from `state.draftBody` and focused, which
    drops the caret at the END. So a caret that is still mid-document after a
    keystroke is proof the element survived; asserting on the VALUE alone could
    not tell the two apart, because the text is identical either way.
    """
    page.evaluate(
        f"""() => {{
            const t = document.querySelector('textarea.editor');
            t.focus(); t.setSelectionRange({offset}, {offset});
        }}"""
    )
    page.keyboard.type(text)


# retires: check_typing_does_not_rebuild_the_editor  (FIX 5 / L-3)
def test_typing_does_not_rebuild_the_editor_and_lose_the_caret(page: Any) -> None:
    """The first keystroke after opening a note used to destroy the caret.

    `renderInspector` replaces the <textarea> and calls focus() on the new one.
    The input handler therefore must NOT dispatch — it updates `state.draftBody`
    and calls `setSaveStatus`. The old guard asserted `"dispatch(" not in
    handler`; this observes the consequence.
    """
    _open_editor_with_save(page, 200, {"body_hash": "sha256:y", "html": "<p>x</p>"})
    _type_at(page, 2, "X")

    assert _caret(page) == 3, (
        f"the caret landed at {_caret(page)} after typing at offset 2; the "
        "textarea was rebuilt and focus() dropped the caret at the end"
    )
    assert page.locator("textarea.editor").input_value() == "liXne\n"
    # And the indicator still moved, which is the half a caret check cannot see.
    assert page.locator(".save-state").get_attribute("data-state") == "dirty"


# retires: check_save_status_does_not_rebuild_the_editor  (L-3, conflict half)
def test_a_conflicting_save_keeps_the_editor_and_the_caret(page: Any) -> None:
    """`saving` and `conflict` both leave `editing` true — so neither may dispatch.

    This is the worst moment to move a user's caret: the save failed and they
    are still holding text that is not on disk. `setSaveStatus` exists for
    exactly these transitions.
    """
    _open_editor_with_save(
        page, 409,
        {"error": {"code": "stale_write", "message": "changed on disk"}},
    )
    _type_at(page, 2, "X")
    before = _caret(page)

    page.locator("button", has_text="Save").first.click()
    page.wait_for_selector('.save-state[data-state="conflict"]')

    assert page.locator("textarea.editor").count() == 1, (
        "the editor was torn down by a FAILED save — the user's unsaved text "
        "is gone"
    )
    assert page.locator("textarea.editor").input_value() == "liXne\n"
    assert _caret(page) == before, (
        f"the caret moved from {before} to {_caret(page)} on a failed save; a "
        "dispatch rebuilt the textarea"
    )


# retires: check_save_status_does_not_rebuild_the_editor  (L-3, error half)
def test_a_failed_save_keeps_the_editor_and_the_caret(page: Any) -> None:
    """The non-409 failure path takes a different branch and must behave the same."""
    _open_editor_with_save(
        page, 500,
        {"error": {"code": "internal", "message": "boom"}},
    )
    _type_at(page, 2, "X")
    before = _caret(page)

    page.locator("button", has_text="Save").first.click()
    page.wait_for_selector('.save-state[data-state="error"]')

    assert page.locator("textarea.editor").count() == 1
    assert _caret(page) == before, (
        f"the caret moved from {before} to {_caret(page)} on a failed save"
    )


# retires: check_roving_tabindex (the renderTree half)
def test_exactly_one_treeitem_is_tabbable_after_a_rerender(page: Any) -> None:
    """The roving tabindex must be REAPPLIED, not just applied once at boot.

    `test_exactly_one_treeitem_is_tabbable` measures a freshly booted rail.
    Expanding a folder wipes and rebuilds every node, so this is the assertion
    that `renderTree` calls `applyRovingTabindex` rather than leaving the new
    nodes at their default tabindex — where every <a> would be tabbable and the
    rail would become a hundred tab stops.
    """
    _expand_first_folder(page)
    tabbables = page.eval_on_selector_all(
        '[role="treeitem"]', "els => els.filter(e => e.tabIndex === 0).length"
    )
    assert tabbables == 1, (
        f"{tabbables} treeitems are tabbable after a re-render, expected 1"
    )


# retires: check_tree_key_handling (the `item.activate()` half)
def test_enter_activates_a_note(page: Any) -> None:
    """`treeKeyAction` returns {type:"activate"} for Enter; something must honour it.

    Every other action type was covered by an arrow test. Activation was not:
    the handler could have dropped straight through to `return` and no test
    would have noticed a tree whose Enter key did nothing — which is exactly the
    WCAG 2.1.1 complaint that started all of this.
    """
    page.locator('[role="treeitem"]', has_text="Root Note").first.focus()
    page.keyboard.press("Enter")
    page.wait_for_function("document.body.dataset.view === 'note'")


# retires: check_visible_items_come_from_the_pure_module
def test_arrow_down_skips_over_a_collapsed_folders_children(page: Any) -> None:
    """The invariant `flattenVisible` exists to make inexpressible, EXECUTED.

    "archive" is collapsed and holds "Buried Note". If the visible list were
    assembled as a side effect of the recursive DOM build again — a `push` above
    an `if (open)` — navigation would step INTO the collapsed folder. Stepping
    down from "archive" must reach "projects".
    """
    archive = page.locator('[role="treeitem"]', has_text="archive").first
    archive.focus()
    page.keyboard.press("ArrowDown")

    landed = _focused_label(page)
    assert "Buried" not in landed, (
        f"ArrowDown from a COLLAPSED folder landed on {landed!r} — navigation "
        "is walking into children that are not on screen"
    )
    assert landed == "projects", f"expected 'projects', got {landed!r}"


# retires: check_aria_owns_ids_are_unique (the counter-RESET half)
def test_subtree_ids_do_not_grow_across_renders(page: Any) -> None:
    """`treeGroupSeq` is reset every render, so ids do not climb forever.

    Without the reset the counter survives each rebuild and a long session mints
    `tree-group-4000`. Presence and uniqueness are covered above; this is the
    reset specifically — the same open folder must mint the SAME id twice.
    """
    folder = _expand_first_folder(page)
    first = folder.get_attribute("aria-owns")

    page.keyboard.press("ArrowLeft")
    page.wait_for_selector('[role="treeitem"][aria-expanded="false"]')
    reopened = _expand_first_folder(page)
    second = reopened.get_attribute("aria-owns")

    assert first == second == "tree-group-0", (
        f"subtree id went {first!r} -> {second!r}; the per-render reset of "
        "treeGroupSeq is gone and ids grow without bound"
    )


# retires: check_tree_focus_survives_rerender (the NOT-STOLEN half)
def test_a_rerender_does_not_steal_focus_from_the_search_box(page: Any) -> None:
    """The other half of H-1, and the more dangerous one.

    `renderTree` restores focus only `if (previousId !== null)` — i.e. only when
    the RAIL held it. Restoring unconditionally would yank focus out of the
    search box on every dispatch, and since searching dispatches on every
    keystroke, the app would fight the user mid-word.

    MEASURED, because the obvious mutation does NOT turn this red and a reader
    would reasonably conclude the test is vacuous:

    * `if (previousId !== null)` -> `if (true)` — **still green**. `restoreIndex`
      returns -1 for a null `focusedId`, and the `if (target !== -1)` gate then
      declines to focus anything. The property has TWO independent guards.
    * replacing the whole restore block with `focusTreeItem(0)` — **red**. That
      is the defect the comment in tree.js actually describes.

    So neither guard alone is load-bearing, and this test fails only when both
    are gone. That is the honest scope of what it covers: do not delete either
    one and read a green suite as permission.
    """
    page.click("#q")
    page.keyboard.type("note")
    page.wait_for_selector(".result")

    focused = page.evaluate("document.activeElement.id")
    assert focused == "q", (
        f"focus moved to {focused!r} while typing a search — a re-render stole "
        "it from the search box"
    )


# retires: check_aria_current
def test_exactly_one_ledger_row_is_aria_current(page: Any) -> None:
    """`aria-current` marks the OPEN row, and only it.

    The old guard counted `setAttribute("aria-current"` occurrences in a slice
    of source. This counts rendered rows carrying the attribute, which is the
    property — and would catch an unconditional set that source-slicing missed.
    """
    page.click("#q")
    page.keyboard.type("note")
    page.wait_for_selector(".result")

    assert page.locator(".result[aria-current]").count() == 0, (
        "a row claims to be current before anything was opened"
    )

    page.locator(".result").first.click()
    page.wait_for_selector(".result[aria-current]")

    current = page.locator(".result[aria-current]")
    assert current.count() == 1, (
        f"{current.count()} ledger rows carry aria-current, expected exactly 1"
    )
    assert "Root Note" in current.first.inner_text()


# retires: check_ledger_shows_a_date
def test_the_ledger_gutter_shows_a_date_not_a_truncated_uuid(page: Any) -> None:
    """FIX 4, as rendered text rather than as an absent `.slice(0, 8)`."""
    page.click("#q")
    page.keyboard.type("note")
    page.wait_for_selector(".result")

    gutter = page.locator(".result").first.locator(".gutter").inner_text()
    assert "2026-01-06" in gutter, f"no date in the ledger gutter: {gutter!r}"
    # The id stays reachable, one hover away, rather than occupying the column.
    assert page.locator(".result").first.get_attribute("title") == "n-root"


# retires: check_aria_selected (the VALUE half — presence is covered above)
def test_aria_selected_tracks_the_open_note(page: Any) -> None:
    """Presence is not the property; being TRUE on the selected item is.

    `test_aria_selected_is_present_on_every_treeitem` passes with the value
    hardcoded to "false" everywhere, which announces "nothing is selected" for
    the whole session.
    """
    assert page.locator('[role="treeitem"][aria-selected="true"]').count() == 0

    page.locator('[role="treeitem"]', has_text="Root Note").first.click()
    page.wait_for_selector('[role="treeitem"][aria-selected="true"]')

    selected = page.locator('[role="treeitem"][aria-selected="true"]')
    assert selected.count() == 1, (
        f"{selected.count()} treeitems claim to be selected, expected 1"
    )
    assert "Root Note" in selected.first.inner_text()


# ------------------------------------------------------- A7: save conflicts --
#
# What a 409 actually did, MEASURED in this harness before anything was
# changed: it did NOT discard the edit. The text survived the conflict and
# survived retries. What destroyed it was the reopen the toast recommended —
# `openNote` dispatches `draftBody: note.body`, overwriting the draft.
#
# So the defect was worse than the spec's account of it. An immediate discard
# is at least honest; this showed the user their text, told them it could not
# be saved, and offered exactly one way forward, which deleted it.

_CONFLICT_NOTE = {
    "id": "n-root", "title": "Root Note", "tier": "vault",
    "content_type": "note", "draft": False, "tags": [],
    "source_kind": "manual", "vault_path": "root.md", "ingested_at": None,
    "editable": True, "movable": True,
    "body": "SERVER LINE\n", "body_hash": "sha256:server",
    "html": "<p>SERVER LINE</p>",
}
_TYPED = "MY UNSAVED WORK"
#: Typed DURING the overwrite's GET round trip, while no modal is up. Must
#: differ from `_TYPED`: the F2 defect sent the pre-GET text, so a value equal
#: to `_TYPED` would be satisfied by the broken version too.
_LATE = "MY UNSAVED WORK PLUS LATE KEYSTROKES"


def _editor_in_conflict(page: Any) -> dict[str, Any]:
    """Drive the app to a real 409 and return the captured PUT bodies.

    Returns a dict whose ``puts`` list grows as saves are attempted, so a test
    can assert what was actually SENT rather than only what was displayed.
    """
    seen: dict[str, Any] = {"puts": [], "conflict": True, "gets": 0}

    def route(r: Any) -> None:
        if r.request.method == "PUT":
            seen["puts"].append(json.loads(r.request.post_data or "{}"))
            if seen["conflict"]:
                r.fulfill(status=409, content_type="application/json",
                          body=json.dumps({"error": {"code": "stale_write",
                                                     "message": "changed on disk"}}))
            else:
                r.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"id": "n-root",
                                           "fields_changed": ["body"],
                                           "rechunked": True,
                                           "body_hash": "sha256:new",
                                           "html": "<p>ok</p>"}))
            return
        # VARY BY CALL, NOT BY LITERAL — and that distinction is the entire
        # discrimination.
        #
        # The FIRST GET is the initial note load: it populates the editor, so
        # it must return what the editor is supposed to be holding
        # (`sha256:server`, `root.md`). Only the SECOND GET — the re-fetch
        # inside `overwriteOnDisk` — returns fresh values. A test can then tell
        # "the code re-fetched" from "the code re-sent what it already had".
        #
        # A previous repair changed WHAT the stub returned without changing
        # WHEN: every GET, including the initial load, returned
        # `sha256:FRESH` / `moved/fresh.md`. So `state.note.body_hash` was
        # ALREADY fresh before a conflict existed, and both assertions were
        # satisfied by the stale path and the fresh path alike. Measured: the
        # mutation `fresh.body_hash -> note.body_hash` stayed GREEN, as did
        # `fresh.vault_path || note.vault_path -> note.vault_path`.
        #
        # THE MECHANISM, stated because it is sharper than the fix: the
        # original claim failed NOT because the values were the same. They did
        # differ from `_CONFLICT_NOTE`'s literals. They did not differ from
        # what the EDITOR already held — and that is the only comparison the
        # assertion makes. The stub varied by literal; the discrimination lives
        # in the *when*.
        #
        # Not gated on `seen["conflict"]`: one test never sets it False, so
        # that gate would not fire there. A `seen["puts"]` gate would also work
        # and is order-robust; the counter is kept because "first GET is the
        # initial load" is the plainer statement of the scenario, and clause
        # (a) — all six A7 tests unmutated — was run to confirm it does not
        # disturb the two tests that reopen a note and fire a second GET.
        seen["gets"] += 1
        first_load = seen["gets"] == 1
        r.fulfill(status=200, content_type="application/json",
                  body=json.dumps(_CONFLICT_NOTE if first_load else {
                      **_CONFLICT_NOTE,
                      "body_hash": "sha256:FRESH",
                      "vault_path": "moved/fresh.md",
                  }))

    page.route("**/api/notes/**", route)
    page.locator('[role="treeitem"]', has_text="Root Note").first.click()
    page.wait_for_selector(".note-bar")
    page.locator("button", has_text="Edit").first.click()
    page.wait_for_selector("textarea.editor")
    page.locator("textarea.editor").fill(_TYPED)
    page.locator("button", has_text="Save").first.click()
    page.wait_for_selector("button.conflict-action")
    return seen


def test_a_conflict_keeps_the_editor_and_the_users_text(page: Any) -> None:
    """The measured baseline, pinned so a future change cannot quietly lose it.

    This half was already correct before A7 — `saveNote`'s catch deliberately
    avoids `dispatch`, which would rebuild the textarea and throw the caret to
    the end. Asserted here because nothing else asserts it, and because the
    rest of A7 is only meaningful if the text is still there to rescue.
    """
    _editor_in_conflict(page)

    assert page.locator("textarea.editor").input_value() == _TYPED
    assert "Conflict" in page.locator(".save-state").inner_text()


def test_reopening_with_unsaved_changes_asks_before_discarding(page: Any) -> None:
    """A7's core: the reopen that used to destroy the work now needs consent.

    Cancelling must leave the text exactly where it was — a prompt that asks
    and then discards anyway would be worse than no prompt.
    """
    _editor_in_conflict(page)

    page.locator('[role="treeitem"]', has_text="Root Note").first.click()
    page.wait_for_selector("#confirm-dialog[open]")
    assert "Discard" in page.locator("#confirm-title").inner_text()

    page.locator("#confirm-dialog button[value='cancel']").click()
    page.wait_for_selector("#confirm-dialog[open]", state="detached")
    assert page.locator("textarea.editor").input_value() == _TYPED


def test_opening_a_note_with_NO_unsaved_changes_does_not_prompt(page: Any) -> None:
    """The over-reach guard for A.

    A version that prompted on EVERY reopen would satisfy the test above
    perfectly and make the application unusable — every click on the tree would
    demand confirmation. The guard compares the draft against the server body,
    so a clean editor, or no editor at all, passes straight through.
    """
    page.route("**/api/notes/**", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(_CONFLICT_NOTE)))
    page.locator('[role="treeitem"]', has_text="Root Note").first.click()
    page.wait_for_selector(".note-bar")
    page.locator('[role="treeitem"]', has_text="Root Note").first.click()
    page.wait_for_timeout(300)

    assert page.locator("#confirm-dialog[open]").count() == 0


def test_replace_on_disk_sends_the_users_text_with_a_fresh_hash(page: Any) -> None:
    """B, and the assertion that matters is on the REQUEST, not the indicator.

    The plausible wrong version refreshes `body_hash` and re-sends whatever the
    note object holds — which is the SERVER's body, not the user's. That saves
    nothing, reports success, and silently discards the work it claimed to
    rescue. Only inspecting the PUT payload can tell the two apart.
    """
    seen = _editor_in_conflict(page)
    seen["conflict"] = False

    page.locator("button.conflict-action").click()
    page.wait_for_selector("#confirm-dialog[open]")
    page.locator("#confirm-dialog button[value='ok']").click()
    page.wait_for_selector("textarea.editor", state="detached")

    last = seen["puts"][-1]
    assert last["body"] == _TYPED, (
        "the overwrite sent something other than the user's text — a refresh "
        "that re-sends the server's own body saves nothing and reports success"
    )
    assert last["body_hash"] == "sha256:FRESH", (
        "the overwrite re-sent the STALE hash — it skipped the GET, so the two "
        "round trips the design depends on collapsed to one and the PUT would "
        "409 again in production"
    )
    assert "Saved" in page.locator(".save-state").inner_text()


def test_typing_DURING_the_overwrite_round_trip_is_not_discarded(page: Any) -> None:
    """F2: the fix reproducing its own defect — an interface reporting success
    while destroying work.

    `overwriteOnDisk` used to capture ``const mine = state.draftBody`` **before**
    ``await api(GET)``. No modal is open across that round trip, so the textarea
    is live and the user can keep typing — and every keystroke made in that
    window was sent as the PRE-GET text, saved, and reported as "Saved". The fix
    moved the read to PUT time, inside the close handler after the
    ``returnValue !== "ok"`` guard.

    **WHY THE TYPING IS DONE IN ONE `page.evaluate` AND NOT WITH `fill()`.**
    Doing both in one evaluated function puts them in the SAME synchronous task.
    `overwriteOnDisk` is async and has suspended at its ``await``; nothing after
    that ``await`` — including ``showModal()`` — has run yet. The window is
    entered **by construction**, with no race to lose.

    A `fill()` issued after `click()` is instead a second round trip into the
    browser, and whether it lands before the GET resolves is a RACE between the
    driver and the stubbed response. **MEASURED, and the result was not the one
    predicted:** with the fix reverted, the `fill()` variant on this machine ALSO
    went red — the route stub is slow enough here that the window stayed open
    long enough for `fill()` to land inside it. So the honest claim is not "the
    naive version passes against unfixed code"; it is that the naive version's
    verdict **depends on machine timing**, and a test whose correctness rests on
    losing a race is one that reports a pass on a faster box. That failure mode
    has already cost this phase a run. The evaluate has no race to lose, which
    is the reason to prefer it — not a measured difference in verdict on THIS
    box.

    ``dialogOpen`` is returned and asserted for that reason. If a future change
    ever opens the modal synchronously the window stops existing, and this test
    must fail loudly rather than keep passing while measuring nothing.

    MUTATION THAT MUST GO RED: hoist ``const mine = state.draftBody`` back above
    the ``await``. The PUT then carries `_TYPED` instead of `_LATE` and the
    **body** assertion fails — not a status, not the save indicator, both of
    which say "Saved" in the broken version too. That is the defect's signature:
    everything the user can see reports success.
    """
    seen = _editor_in_conflict(page)
    seen["conflict"] = False

    window = page.evaluate("""(late) => {
        const area = document.querySelector("textarea.editor");
        document.querySelector("button.conflict-action").click();
        /* Same synchronous task. overwriteOnDisk has suspended at its await;
           the modal does not exist yet and the textarea is still live. */
        const dialogOpen = document.getElementById("confirm-dialog").open;
        area.value = late;
        area.dispatchEvent(new Event("input", { bubbles: true }));
        return { dialogOpen, typed: area.value };
    }""", _LATE)

    assert window["dialogOpen"] is False, (
        "the confirm modal was already open when the keystrokes landed — the "
        "textarea was inert, so this measured a window in which the defect "
        "cannot occur and would pass against unfixed code"
    )
    assert window["typed"] == _LATE, "the late keystrokes never reached the editor"

    page.wait_for_selector("#confirm-dialog[open]")
    page.locator("#confirm-dialog button[value='ok']").click()
    page.wait_for_selector("textarea.editor", state="detached")

    last = seen["puts"][-1]
    assert last["body"] == _LATE, (
        "the overwrite sent the PRE-GET text: everything typed while the GET "
        "was in flight was silently discarded, and the UI reported 'Saved'. "
        f"Sent {last['body']!r}, expected {_LATE!r}"
    )


def test_the_replace_confirm_names_the_file_and_what_is_lost(page: Any) -> None:
    """The confirm must let the user tell what they are discarding.

    "Are you sure?" is not consent when the thing being replaced is invisible.
    The other version is usually the watcher or brain-mcp having written
    something real, so the wording names the file and states the loss without
    implying that version was junk.
    """
    _editor_in_conflict(page)
    page.locator("button.conflict-action").click()
    page.wait_for_selector("#confirm-dialog[open]")

    text = page.locator("#confirm-text").inner_text()
    assert "moved/fresh.md" in text, (
        "the confirm named the stale path. `root.md` was unfalsifiable here — "
        "`overwriteOnDisk` falls back to `note.vault_path`, which is also "
        "root.md, so the assertion passed whether or not the GET happened"
    )
    assert "lost" in text.lower()
    assert page.locator("#confirm-input").is_visible() is False


def test_the_delete_dialog_STILL_shows_its_typed_title_field(page: Any) -> None:
    """The negative row for `.field[hidden]`, and it guards a real control.

    The hidden-direction assertion above passes if the field is hidden ALWAYS —
    `configureConfirm` ignoring `gateOnTitle`, or the CSS becoming
    `.field { display: none }`. Either mutation keeps the suite green while
    destroying delete's typed-title gate, which is the confirmation standing
    between a click and permanently deleting a note, its chunks and its file.

    Every mutation table needs a row that must come back negative; this is that
    row for this rule.
    """
    page.route("**/api/notes/**", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(_CONFLICT_NOTE)))
    page.locator('[role="treeitem"]', has_text="Root Note").first.click()
    page.wait_for_selector(".note-bar")
    page.locator("button", has_text="Delete").first.click()
    page.wait_for_selector("#confirm-dialog[open]")

    assert page.locator("#confirm-input").is_visible() is True, (
        "delete's typed-title field is hidden — the gate is gone"
    )
    assert page.locator("#confirm-ok").is_disabled() is True


# --------------------------------------------------- T16: the explorer rail --
#
# Three features, one shared invariant: the rendered tree, the folder counts and
# the arrow-key order are all derived from ONE prepared tree in js/tree.js. That
# is the whole defence against the defect the overlay's own comment records —
# "the count is computed from the post-filter trie" — where the tree is filtered
# and the badges are not. It would break navigation the same way: flattenVisible
# would hand back descriptors for items the DOM never drew.
#
# THESE TESTS MOUNT THE TOGGLE THEMSELVES via page.evaluate, because index.html
# and js/main.js were integrator-owned while they were written. **They therefore
# prove the module, NOT that the shipped app serves it** — that distinction is
# permanent and is why the sentence stays.
#
# REACHABILITY IS CLOSED, AND ELSEWHERE. `js/main.js` calls
# `wireIngestedToggle()` from boot(), and
# `check_the_ingested_toggle_is_wired_into_boot` in
# tests/test_ui_static_behaviour.py asserts it, with the mutation entry
# `ingested-toggle-wired-in-boot`.
# So the gap these tests cannot see is covered by a guard that can, and this
# comment names the guard rather than the gap.
#
# It previously ended "Reachability needs the boot call in main.js, reported
# unapplied on the task" — true when written, false once the wiring landed, and
# nothing failed when it went stale because a comment cannot. Re-derived
# 2026-08-14 before this edit rather than trusting the line numbers in the
# report that raised it: they had already drifted by one and by sixty-five.


def _mount_toggle(page: Any) -> None:
    """Call the REAL wireIngestedToggle(); idempotent, so boot() may also."""
    page.evaluate(
        """async () => {
            const tree = await import("/static/js/tree.js");
            tree.wireIngestedToggle();
        }"""
    )


def _folder_counts(page: Any) -> dict[str, str]:
    """Every visible folder's name -> its rendered badge text."""
    return page.evaluate(
        """() => Object.fromEntries(
            [...document.querySelectorAll(".tree-label .folder-count")].map((b) => {
                const label = b.closest(".tree-label");
                const clone = label.cloneNode(true);
                clone.querySelectorAll(".folder-count, .twisty").forEach(
                    (n) => n.remove());
                return [clone.textContent.trim(), b.textContent];
            })
        )"""
    )


def _folder_names(page: Any) -> list[str]:
    return page.eval_on_selector_all(
        ".tree-folder > .tree-label",
        """els => els.map((e) => {
            const c = e.cloneNode(true);
            c.querySelectorAll(".folder-count, .twisty").forEach((n) => n.remove());
            return c.textContent.trim();
        })""",
    )


def test_the_ingested_subtree_is_hidden_until_asked_for(page: Any) -> None:
    """Default OFF, per the overlay (P4.2).

    The vault's own notes are what the reader authored; the `_ingested/` mirror
    is machine-written and dwarfs it, so a rail that opens showing everything
    buries the half that was written by hand.
    """
    _mount_toggle(page)
    assert "_ingested" not in _folder_names(page), (
        "the ingested mirror is visible on load — the toggle is defaulting ON"
    )
    assert "projects" in _folder_names(page), (
        "guard the guard: the vault folders are gone too, so the assertion "
        "above would pass against a rail that rendered nothing at all"
    )


def test_every_folder_count_drops_by_exactly_its_ingested_leaf_count(
    page: Any,
) -> None:
    """**THE** T16 test, and the one the declared mutation must redden.

    "projects" holds three vault notes and one ingested one, so it SURVIVES the
    filter while its badge must still change: 4 shown, 3 hidden. Asserting only
    that the `_ingested` subtree disappeared would pass against a rail whose
    counts never moved — which is precisely the defect the overlay warns about.

    MUTATION THAT MUST GO RED (measured, not assumed): in `js/tree.js`, delete
    the `note_count: node.vault_count` line from `pruneIngested`. The tree still
    shrinks and every badge keeps reporting the unfiltered total, so "projects"
    reads 4 with its ingested note nowhere on screen.
    """
    _mount_toggle(page)
    hidden = _folder_counts(page)
    assert hidden.get("projects") == "3", (
        f"with ingested hidden, projects must count only its 3 vault notes; "
        f"got {hidden.get('projects')!r}. A badge reading '4' is the count "
        "being taken from the unfiltered tree."
    )
    # "archive" is the control: pure vault, so the filter must not move it.
    # Its nested sibling "q3" is NOT checked here — it lives inside the
    # collapsed "projects" and is therefore not rendered at all, so asserting
    # on it would be asserting on an absent element.
    assert hidden.get("archive") == "1", "a pure-vault folder must not move"

    page.locator(".show-ingested input").check()
    page.wait_for_function(
        """() => [...document.querySelectorAll('.tree-folder > .tree-label')]
              .some((e) => e.textContent.includes('_ingested'))"""
    )
    shown = _folder_counts(page)
    assert shown.get("projects") == "4", (
        f"with ingested shown, projects must count all 4 of its notes; got "
        f"{shown.get('projects')!r} — the badge is not recounting on toggle"
    )
    assert shown.get("_ingested") == "3", (
        f"the ingested mirror should report its 3 leaves; got "
        f"{shown.get('_ingested')!r}"
    )
    # The delta is the whole claim, stated as one assertion rather than left
    # for the reader to compute from the two above.
    assert int(shown["projects"]) - int(hidden["projects"]) == 1, (
        "projects' badge did not drop by exactly its one ingested leaf"
    )


def test_the_arrow_keys_never_walk_into_a_hidden_ingested_note(
    page: Any,
) -> None:
    """The other half of one-prepared-tree, and it fails independently.

    A rail that filtered the DOM but handed the UNFILTERED tree to
    flattenVisible would render correctly and navigate into items that are not
    on screen — focus would appear to vanish. Home lands on the first visible
    item, which must be "archive" and never "_ingested".
    """
    _mount_toggle(page)
    page.locator('[role="treeitem"]').first.focus()
    page.keyboard.press("Home")
    assert _focused_label(page) == "archive", (
        f"Home landed on {_focused_label(page)!r}; with ingested hidden the "
        "first visible item is 'archive'. Landing on '_ingested' means "
        "navigation is reading a different tree than the DOM."
    )


def test_the_toggle_survives_a_reload(page: Any) -> None:
    """Persisted, per the overlay — a view preference the reader set once."""
    _mount_toggle(page)
    page.locator(".show-ingested input").check()
    page.wait_for_function(
        """() => [...document.querySelectorAll('.tree-folder > .tree-label')]
              .some((e) => e.textContent.includes('_ingested'))"""
    )

    page.reload()
    page.wait_for_selector('[role="treeitem"]')
    _mount_toggle(page)
    assert "_ingested" in _folder_names(page), (
        "the toggle reset to OFF across a reload — the preference is not "
        "persisted, so the reader re-sets it on every visit"
    )
    assert page.locator(".show-ingested input").is_checked() is True, (
        "the tree honoured the stored preference but the checkbox did not, so "
        "the control now misreports the state it controls"
    )


def test_the_krisp_feed_is_grouped_by_month_newest_first(page: Any) -> None:
    """Month headers for a feed folder (overlay P4.3), from `TreeNote.date`.

    The seed's krisp titles sort DIFFERENTLY from their dates — alphabetically
    Budget, Roadmap, Vendor; by date Vendor (Mar 15), Roadmap (Mar 3), Budget
    (Feb 20) — and the server sends them in title order. So a rail that failed
    to reorder is caught here rather than accidentally agreeing.
    """
    _mount_toggle(page)
    page.locator(".show-ingested input").check()
    page.wait_for_function(
        """() => [...document.querySelectorAll('.tree-folder > .tree-label')]
              .some((e) => e.textContent.includes('_ingested'))"""
    )
    page.locator('.tree-label', has_text="_ingested").first.click()
    page.wait_for_function(
        """() => [...document.querySelectorAll('.tree-folder > .tree-label')]
              .some((e) => e.textContent.includes('krisp'))"""
    )
    page.locator('.tree-label', has_text="krisp").first.click()
    page.wait_for_selector(".month-header")

    assert page.eval_on_selector_all(
        ".month-header", "els => els.map((e) => e.textContent)"
    ) == ["Mar 2026", "Feb 2026"], "month headers are absent, wrong, or oldest-first"

    # The notes themselves, in rendered order, newest day first.
    assert page.eval_on_selector_all(
        ".tree-note a", "els => els.map((e) => e.textContent)"
    )[:3] == ["Mar 15 · Vendor Sync", "Mar 3 · Roadmap Review",
              "Feb 20 · Budget Check"], (
        "krisp notes are not in newest-day-first order, or the day prefix is "
        "missing from the link text"
    )


def test_a_month_header_is_not_a_stop_on_the_arrow_key_path(page: Any) -> None:
    """Headers are captions, not destinations.

    A focusable header would put a stop between every month that the arrow keys
    have to step over, and it owns nothing to expand. The overlay keeps them
    non-collapsible for the same reason.
    """
    _mount_toggle(page)
    page.locator(".show-ingested input").check()
    page.wait_for_function(
        """() => [...document.querySelectorAll('.tree-folder > .tree-label')]
              .some((e) => e.textContent.includes('_ingested'))"""
    )
    assert page.locator('.month-header[role="treeitem"]').count() == 0, (
        "a month header is exposed as a treeitem"
    )
