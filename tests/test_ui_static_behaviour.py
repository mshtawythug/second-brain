"""Structural guards on the parts of the front end that cannot be executed here.

``tests/test_ui_tree_nav.py`` runs the tree's *decision* logic under node, and
``tests/test_ui_browser.py`` now runs the whole front end in a real browser.
What remains here is what neither can reach: invariants about the SHAPE of the
source, which by construction have no rendering that expresses them.

Source assertions rot into decoration the moment nobody checks they can fail —
this repository has shipped roughly a dozen guards that did nothing. So every
check below is a **function over a source string**, the real file is run through
it, and :func:`test_every_guard_can_fail` runs the same function over a source
with that exact defect reintroduced and requires it to raise.

That harness had a structural hole of its own, and it is now closed. It could
catch an anchor that stopped matching, but NOT a guard that was already failing
for an unrelated reason — such a guard raises on mutated source too, so
``pytest.raises`` is satisfied and the entry passes while certifying nothing.
``check_every_stylesheet_is_linked_in_order`` shipped in exactly that state.
:func:`test_every_guard_can_fail` therefore now asserts, in order: **(a)** the
guard passes on the UNMUTATED source, **(b)** the mutation lands the declared
number of times, and **(c)** only then, that the guard raises. Clause (a) makes
the vacuity inexpressible; clause (b) makes "my replacement hit the wrong thing"
fail at the line that made the mistake. All three were proven able to fail
before being believed.

WHAT LEFT WITH THE STYLESHEET SPLIT. Exactly one: ``check_measure`` asserted the
literal ``--measure: 48ch``, and
``test_the_reading_measure_stays_in_the_classical_band`` now renders
``.note-body`` and divides its computed max-width by the measured average
advance of a real alphabet in the font actually resolved. That is the property;
48ch was only the current means to it.

SEVEN stylesheet guards STAYED, and they stayed because the question "would the
browser test fail if this were wrong?" was answered by EXPERIMENT:

    Deleting the ``layout.css`` <link> from index.html — removing the
    ``.inspector`` grid, the ``1fr`` track and the stretch opt-in outright —
    left the browser harness at **15 passed**.

``test_the_editor_fills_the_pane_and_a_resize_drag_wins`` discriminates grid
from FLEX-GROW, which is the bug it was commissioned for. It does not
discriminate grid from *nothing*: the textarea's own ``min-height: 26rem`` is
416px and clears the "it fills" bar unaided. So ``check_editor_sizing``,
``check_inspector_is_a_grid``, ``check_color_scheme``, the three ``--ink-faint``
rules and ``check_stretch_is_opt_in`` are all still here — seven, counted.

(An earlier revision of this docstring said "five", and a comment beside the
``GUARDS`` list claimed six guards had been *retired* with the stylesheet split
when only ``check_measure`` was — the other five were live entries twenty lines
below, next to a comment saying "Retained rather than retired". Both are
corrected. A file whose entire premise is that claims must not drift from
behaviour does not get to exempt its own prose.)

``check_every_stylesheet_is_linked_in_order`` is the one stylesheet guard the
split ADDED rather than kept: a SET of files can be half-loaded where one
could not.

WHAT LEFT WITH THE JS SPLIT, AND WHY THAT LIST IS LONG. ``app.js`` became eight
modules under ``static/js/``, so every guard that read ``APP_JS`` as one string
lost its subject. None of them were re-anchored at a new address: a
source-substring assertion whose only oracle is the implementation it reads is a
change-detector, and quietly re-pointing one at ``js/tree.js`` would have
preserved the *appearance* of coverage while proving nothing new. Each was
instead asked what a user would notice, and ELEVEN tests at the end of
``test_ui_browser.py`` are the answers — every one of them mutation-tested
against the defect it names. The trade, guard by guard:

===============================================  =============================
retired source guard                             what executes it now
===============================================  =============================
``check_roving_tabindex``                        ``…_is_tabbable_after_a_rerender``
``check_tree_key_handling``                      the four arrow tests, plus
                                                 ``test_enter_activates_a_note``;
                                                 the import specifier is checked
                                                 harder by
                                                 ``test_every_es_module_import_resolves_to_a_file``
``check_tree_focus_survives_rerender``           ``…_expands_and_focus_survives…``,
                                                 ``…_collapses_and_focus_survives…``,
                                                 ``…_does_not_steal_focus_from_the_search_box``
``check_visible_items_come_from_the_pure_module`` ``…_skips_over_a_collapsed_folders_children``
``check_aria_selected``                          ``…_is_present_on_every_treeitem``
                                                 plus ``…_tracks_the_open_note``
``check_save_status_does_not_rebuild_the_editor`` ``…_conflicting_save_keeps_…_caret``,
                                                 ``…_failed_save_keeps_…_caret``
``check_typing_does_not_rebuild_the_editor``     ``…_typing_does_not_rebuild_…``
``check_aria_current``                           ``…_exactly_one_ledger_row_is_aria_current``
``check_ledger_shows_a_date``                    ``…_gutter_shows_a_date_not_a_truncated_uuid``
``check_aria_owns_ids_are_unique`` (3 of 4)      ``…_own_their_subtree``,
                                                 ``…_id_is_unique_in_the_document``,
                                                 ``…_ids_do_not_grow_across_renders``
===============================================  =============================

FOUR JS GUARDS STAYED, all of them anti-erosion invariants with no rendering
that can express them — and re-homing those to their new module IS legitimate,
because the property survived the move intact:

* :func:`check_single_innerhtml` — now over the CONCATENATION of every module,
  which is strictly stronger than it was: the rule is "one sink in the front
  end", and while it was one file that was also all it could mean.
* :func:`check_resize_is_not_inert` — the append-count invariant, settled by the
  layout.css experiment above.
* :func:`check_subtree_ids_come_from_a_counter` — the one quarter of the old
  aria-owns guard the browser cannot reach; see its docstring.
* :func:`check_tree_container_is_not_a_tab_stop` — index.html, untouched by the
  split.
"""
from __future__ import annotations

import ast
import re
import sys
import traceback
from pathlib import Path

import pytest

from brain.ui.app import static_dir

#: Opens NO database connection — this module reads files off disk and
#: parses them. The marker lets the session skip the schema reset and, more
#: importantly, the MACHINE-WIDE advisory lock; see
#: ``conftest._session_touches_the_database``.
pytestmark = pytest.mark.nodb

#: This file, read as source. Clauses (d) and (e) below reason about the guards
#: as an AST — which assertions exist, which are inside a loop over a literal —
#: and attribute each mutation's raise to a line in it.
_GUARD_PATH = Path(__file__)
_GUARD_SOURCE = _GUARD_PATH.read_text(encoding="utf-8")
_GUARD_LINES = _GUARD_SOURCE.splitlines()

STATIC = Path(str(static_dir()))
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")

#: The stylesheet is several files — four shared tiers (tokens / base / layout /
#: components) then one per feature — in cascade order. Each guard below is handed the
#: ONE file whose property it asserts, which keeps :func:`_block`'s uniqueness
#: check scoped to a single file — an anchor that is unique in components.css
#: but also appears in tokens.css must still be an error, and it is, because the
#: guard never sees both. The concatenation is offered separately, and only to
#: the one invariant that is genuinely stylesheet-wide.
#:
#: The per-feature sheets (``palette.css``, ``marginalia.css``) come after the
#: four shared tiers, and that is load-bearing twice over: each is layered on
#: the component tier, and ``check_every_stylesheet_is_linked_in_order`` asserts
#: ``links == list(CSS_ORDER)`` — an EXACT match, not a superset — so this tuple
#: and index.html's <link> block are one fact stored in two places and must
#: agree. Adding a sheet is therefore always a two-file change.
#:
#: THAT EXACTNESS IS NOT PEDANTRY, AND THERE IS NOW A PRICE TAG ON IT.
#: ``marginalia.css`` shipped with ``var(--ink-faint)`` on
#: ``.marginalia .breadcrumbs li`` — real text, below WCAG AA in both themes —
#: and NOTHING could see it, because a sheet outside this tuple is never handed
#: to ``check_ink_faint_used_once`` or any other ``css/*`` guard. The defect
#: surfaced the moment the name was added here, which is the whole argument for
#: the roster being exact rather than a floor: a sheet that is linked but
#: unrostered is styling the app while exempt from every rule the app has.
CSS_ORDER = (
    "tokens.css", "base.css", "layout.css", "components.css",
    "palette.css", "marginalia.css", "reading.css", "explorer.css",
    "discovery.css", "thread.css",
)
CSS = {name: (STATIC / "css" / name).read_text(encoding="utf-8") for name in CSS_ORDER}
ALL_CSS = "\n".join(CSS[name] for name in CSS_ORDER)

#: The ten ES modules. The order is fixed and arbitrary — NOT a dependency
#: order, because none exists: ``tree.js`` imports ``openNote`` from
#: ``inspector.js`` while ``inspector.js`` imports ``loadTree`` from
#: ``tree.js``, so the graph has a cycle and cannot be topologically sorted.
#: (An earlier revision of this comment claimed "dependency order (leaves
#: first)" three lines above the cycle it contradicts.) All this order has to
#: be is STABLE, so ``JS_ALL`` concatenates the same way every run and a guard
#: reading it cannot depend on collection order. Same discipline as
#: the stylesheet above: a guard is handed the single module whose property it
#: asserts, so ``_block`` cannot slice across a file boundary. ``JS_ALL`` exists
#: for the one rule that is genuinely front-end-wide.
#:
#: ``tree_nav.js`` is deliberately NOT here. It is pure, DOM-free and executed
#: directly by ``tests/test_ui_tree_nav.py``; nothing about it needs asserting
#: as text. See the design spec §2 for why it stayed outside ``js/``.
#:
#: THIS ROSTER IS HAND-MAINTAINED, AND THAT IS A KNOWN WEAKNESS. A module that
#: is not listed is not *failed* by any JS_ALL guard — it is never looked at,
#: and the guards stay green while meaning less than they appear to. That is
#: exactly what happened to ``palette.js``, which shipped complete and invisible
#: to ``check_single_innerhtml`` until it was added here by hand — and then
#: immediately again to ``marginalia.js``, one wave later, by the same
#: mechanism. Two instances is not bad luck; it is the roster's shape. The
#: stylesheet half of the same gap is worse than invisible: adding
#: ``marginalia.css`` to :data:`CSS_ORDER` made ``check_ink_faint_used_once``
#: fail on the SHIPPED source, i.e. the file had been carrying a real WCAG
#: defect for as long as it had been outside the roster. The structural
#: fix is to DISCOVER the roster from ``js/`` with an explicit opt-out list for
#: ``tree_nav.js``, the same marker-discovery shape the CI browser-selection
#: guard was rewritten into for this identical reason; it is tracked separately
#: and is deliberately not bundled into the wiring change that exposed it.
#: Until then: a new module MUST be added to this tuple in the same change that
#: creates it.
JS_ORDER = (
    "dom.js", "api.js", "store.js",
    "inspector.js", "tree.js", "results.js", "keys.js",
    "palette.js", "marginalia.js", "discovery.js",
    "thread.js", "main.js",
)
JS = {name: (STATIC / "js" / name).read_text(encoding="utf-8") for name in JS_ORDER}
JS_ALL = "\n".join(JS[name] for name in JS_ORDER)

#: Shipped ``.js`` files that are deliberately OUTSIDE :data:`JS_ORDER`, each
#: with the reason it is out.
#:
#: A DICT, NOT A SET, AND THAT IS THE MECHANISM. The roster gap this closes was
#: never that exclusions are wrong — ``tree_nav.js`` genuinely should not be in
#: ``JS_ORDER``. It was that an exclusion and an OVERSIGHT looked identical:
#: both were simply an absence. Requiring a reason string makes a silent
#: exclusion inexpressible, and :func:`test_the_js_roster_is_every_shipped_module`
#: rejects an empty one.
#:
#: Keys are paths relative to ``static/``, because the discovery walks all of
#: ``static/`` rather than only ``static/js/`` — see that test for why.
JS_OPT_OUT: dict[str, str] = {
    "tree_nav.js": (
        "Pure, DOM-free decision logic, deliberately kept outside js/ so it can "
        "be imported and executed directly by tests/test_ui_tree_nav.py. It is "
        "covered by real execution, which is strictly better than the source "
        "assertions JS_ALL applies — see the design spec §2."
    ),
    "theme.js": (
        "Not an ES module and not part of the app graph: 20 lines loaded "
        "non-deferred in <head> so the stored theme is applied before first "
        "paint. The JS_ALL guards assert module-shaped properties (a single "
        "innerHTML sink, import hygiene) that do not apply to a pre-paint "
        "shim, and index.html's own <script> tag is what pins its existence."
    ),
}

#: Stylesheets under ``static/css/`` deliberately outside :data:`CSS_ORDER`.
#: Empty today, and that is the correct state — every stylesheet that exists is
#: linked and rostered. It is declared anyway so that the first author who wants
#: an exclusion has to write down why, rather than discovering that omission is
#: free.
CSS_OPT_OUT: dict[str, str] = {}


def _block(source: str, opening: str, closing: str) -> str:
    """The text from ``opening`` to the first ``closing`` after it.

    The uniqueness assertion is the important part. ``str.index`` takes the
    FIRST match, so a non-unique opening anchor silently slices a different
    rule than the guard intends — and the guard then reports on code it was
    never pointed at. That has happened repeatedly in this file:

    * ``".gutter"`` matched ``.mono, .meta-line, .gutter, kbd {`` before
      ``.gutter {``, reporting a contrast failure that did not exist;
    * ``".editor {"`` began matching ``.inspector > .editor {`` the moment that
      rule was added, so the guard read ``align-self: stretch`` as the whole
      editor rule and declared the min-height floor missing.

    Both passed review as "obviously fine" anchors. Checking uniqueness by hand
    at authoring time has now failed often enough to stop relying on it, so it
    is enforced here for every current and future caller.
    """
    count = source.count(opening)
    # Two different failures with two different remedies. The duplicate case is
    # "you sliced the wrong rule"; the zero case is "the code moved" and has no
    # "first match" to slice from at all. One message for both misdirects
    # whoever hits the rarer one.
    assert count != 0, (
        f"_block opening anchor {opening!r} is not present at all. The code it "
        "points at was moved, renamed, or deleted — update the anchor to match "
        "the current source, or remove the guard if the property is gone."
    )
    assert count == 1, (
        f"_block opening anchor {opening!r} matches {count} times, so it would "
        "slice from whichever comes first and the guard would inspect the wrong "
        "code. Make the anchor unique (anchor on the line start, or include "
        "enough surrounding text to disambiguate)."
    )
    start = source.index(opening)
    end = source.index(closing, start)
    return source[start:end]


# ------------------------------------------------------- the guards, as code --


def check_single_innerhtml(js: str) -> None:
    """The server-rendered note body stays the ONLY innerHTML in the FRONT END.

    Handed the concatenation of every module in :data:`JS_ORDER`, which is what makes this
    stronger after the split than before it. While the front end was one file,
    "one innerHTML in this file" and "one innerHTML anywhere" were the same
    sentence. They are not any more: a second sink appearing in results.js or
    tree.js is exactly the regression, and only the concatenation can see it.
    """
    # ``.innerHTML`` with the dot: the prose mentions of the rule ("textContent,
    # never innerHTML" in dom.js, "The ONLY innerHTML in this file" in
    # inspector.js) are documentation, not sinks.
    assert js.count(".innerHTML") == 1, (
        f"expected exactly one .innerHTML in the whole front end (the "
        f"server-rendered body), found {js.count('.innerHTML')}"
    )
    assert "body.innerHTML = note.html;" in js, "the one innerHTML is not the body"


def _boot_body(js: str) -> str:
    """``boot()``'s body from ``js/main.js``, WITH COMMENTS STRIPPED.

    The stripping is a correctness fix, not tidying, and it was found the way
    everything in this file is found — by a clause failing, not by review.

    ``check_the_marginalia_is_wired_after_the_inspector`` compares the position
    of ``wireMarginalia();`` against the position of
    ``subscribe(renderInspector)``. The comment that EXPLAINS that constraint
    sits directly above the line and necessarily contains the words
    ``subscribe(renderInspector)`` — so on the relocated source
    ``str.index`` found the mention in the prose, which is still above the
    moved call, and the guard passed. ``test_every_guard_can_fail`` reported
    ``'marginalia-wired-after-the-inspector' did not raise``.

    Fourth instance of this exact shape in this file, and the first one where a
    guard's own justifying comment was the thing that blinded it:
    ``check_every_stylesheet_is_linked_in_order`` grepped ``@import`` against
    prose explaining why there is no ``@import``; ``check_single_innerhtml``
    counts ``.innerHTML`` with the dot because two modules document the rule in
    words; ``check_aria_current`` records the same trap. The rule that documents
    a constraint will always contain the tokens the constraint is written in.

    Both boot guards read through here, so neither can regress into it — and a
    future comment saying "we removed ``wirePalette();``" cannot keep the
    palette guard green either.
    """
    boot = _block(js, "async function boot() {", "\nboot();")
    without_blocks = re.sub(r"/\*.*?\*/", "", boot, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", without_blocks)


def check_the_palette_is_wired_into_boot(js: str) -> None:
    """The palette is REACHABLE from the shipped app, not merely present in it.

    This is the phase's signature defect written as an invariant. ``palette.js``
    was delivered complete, unit-correct and fully covered — and unreachable,
    because ``tests/test_ui_browser_nav.py`` mounts it by calling
    ``wirePalette()`` from ``page.evaluate`` rather than by booting the real
    front end. Every one of those six tests passes with ``js/main.js`` never
    having heard of the module. A user pressing ⌘P would have got a print
    dialog.

    So the two things that make it reachable are asserted here, separately,
    because they fail separately: the module must be IMPORTED (without which
    nothing in it ever executes and its own ⌘P listener is never registered),
    and ``wirePalette()`` must be called from ``boot()`` (without which the
    import is a no-op module evaluation that mounts no dialog).

    The second assertion is scoped to ``boot()``'s body rather than the file.
    ``"wirePalette();" in js`` is satisfied by the import line itself and by any
    comment mentioning the call — the same prose-satisfies-the-grep trap that
    made ``check_every_stylesheet_is_linked_in_order``'s ``@import`` check blind
    on shipped source. The import assertion is anchored on the whole statement
    for the same reason: this file's own header comment names ``palette.js``.
    """
    assert 'import { wirePalette } from "/static/js/palette.js";' in js, (
        "js/main.js does not import the palette module, so nothing in "
        "palette.js ever evaluates and its ⌘P listener is never registered. "
        "The browser suite still passes — it calls wirePalette() itself."
    )
    boot = _boot_body(js)
    assert "wirePalette();" in boot, (
        "boot() does not call wirePalette(), so no <dialog> is ever built and "
        "⌘P does nothing. Importing the module alone is not enough: the module "
        "registers its listener from wirePalette(), not at evaluation time."
    )


def check_the_ingested_toggle_is_wired_into_boot(js: str) -> None:
    """T16's control must be MOUNTED, not merely exported.

    The third instance of this phase's signature shape, and the most complete
    one yet: ``wireIngestedToggle()`` shipped working and mutation-proven, its
    filtering and its per-folder counts already reaching the app through
    ``renderTree`` — and the checkbox that turns it on was never injected,
    because nothing called it. The feature was not broken. It was unreachable,
    which every one of its own tests is structurally unable to notice.

    ONE ASSERTION, and the two omissions are deliberate.

    NO ORDERING PIN. ``wireMarginalia()`` gets one because it subscribes and is
    wiped by ``renderInspector``'s ``host.textContent = ""``; this injects a
    control into the static ``.rail-head`` and subscribes to nothing, so no
    order can be wrong. Pinning it anyway would be a change-detector — the
    shape this file exists to refuse — and it is the same call that kept
    ``wirePalette()`` out of the ordering guard.

    NO IMPORT ASSERTION. A missing named import is an ES-module LINK error:
    the whole module fails to evaluate and the app does not boot at all, which
    every browser test reports loudly. An assertion whose subject can only fail
    in a way that is already impossible to miss adds a claim without adding
    coverage.

    Read through :func:`_boot_body`, so a comment mentioning the call — such as
    the one directly above the call site, which names it twice — cannot satisfy
    the check.
    """
    assert "wireIngestedToggle();" in _boot_body(js), (
        "boot() does not call wireIngestedToggle(), so the 'Show ingested' "
        "control is never injected into .rail-head. The filtering and the "
        "counts still work — they ride renderTree — so the vault silently hides "
        "every ingested document with no way for the user to reveal them, and "
        "T16's own tests all pass because they mount the module themselves."
    )


def check_the_thread_is_wired_after_the_inspector(js: str) -> None:
    """Same constraint as the marginalia, and the task that shipped it said otherwise.

    T18's handover states "ORDERING IS NOT SENSITIVE (idempotent, guarded by a
    `data-threadReady` flag), unlike wireMarginalia". **Measured in a real
    browser, that is false**, and the flag is what makes it look true.

    ``renderThread`` does not build its own container — it DECORATES the
    ``.note-body`` that ``renderInspector`` creates, wrapping the newest ``<h2>``
    into a synthetic ``<details open>``. Registered before ``renderInspector``,
    it runs against the OUTGOING body (or none at all) and the incoming one is
    never decorated. The ``data-threadReady`` flag lives on the body ELEMENT,
    and every render builds a fresh body without it — so the flag gives
    idempotence within one body, not independence of order across renders.

    Probed both ways against the real modules, one note opened, identical
    otherwise:

        call on the wire line   -> data-threadReady ABSENT, newest not wrapped
        call after subscribe()  -> data-threadReady "1", newest wrapped

    **Zero page errors in both.** Thread mode is simply absent in the first, which
    is why a guard is warranted here and was not for ``wireIngestedToggle`` — that
    one genuinely injects rather than subscribing, and pinning ITS order would
    have been the change-detector this file refuses.

    Read through :func:`_boot_body`, so a comment naming the call cannot satisfy
    it.
    """
    boot = _boot_body(js)
    assert boot.count("wireThread();") == 1, (
        f"boot() calls wireThread() {boot.count('wireThread();')} times, expected "
        "exactly 1. Zero means thread mode never mounts; two would make the "
        "position check below measure whichever comes first."
    )
    assert boot.index("wireThread();") > boot.index("subscribe(renderInspector)"), (
        "wireThread() is called BEFORE subscribe(renderInspector). renderThread "
        "decorates the .note-body that renderInspector builds, so registering it "
        "first means it runs against the outgoing body and the incoming one is "
        "never decorated. Measured: data-threadReady absent, newest heading "
        "unwrapped, and NO page error — the feature is silently gone."
    )


def check_the_marginalia_is_wired_after_the_inspector(js: str) -> None:
    """POSITION, not merely presence — and the two are different defects.

    ``check_the_palette_is_wired_into_boot`` above asserts membership in
    ``boot()``'s body, which is the whole property for the palette:
    ``wirePalette()`` builds a ``<dialog>`` on ``document.body`` and subscribes
    to nothing, so it is order-free. ``wireMarginalia()`` is not. It calls
    ``subscribe(renderMarginalia)`` and draws into ``#inspector``, and
    ``store.js``'s ``dispatch()`` runs listeners in REGISTRATION order while
    ``renderInspector`` opens with ``host.textContent = ""``. Register the
    marginalia subscriber first and it is drawn, then wiped, on every dispatch.

    THE FAILURE IS SILENT: nothing throws, no console warning, ``#inspector``
    simply never carries an ``<aside class="marginalia">``. Boot itself is
    unharmed — probed in a real browser against a copy of the static tree, the
    relocated build still renders the vault tree with ZERO page errors, which
    is what makes this so easy to ship.

    AND THE BROWSER SUITE ONLY CATCHES IT BY ACCIDENT. Measured, on
    ``tests/test_ui_browser_reading.py`` with the relocation applied to the real
    ``main.js``: **2 failed, 11 passed.** Both failures are
    ``wait_for_selector(".marginalia .toc a")`` timeouts, and both are T14
    *backlinks* tests that first gate the links route. Not one of the eight T13
    tests — the marginalia's OWN tests, the ones a reader would expect to cover
    this — went red. Two reasons compound: each test calls ``wireMarginalia()``
    itself from ``page.evaluate``, and the backlinks rail attaches
    asynchronously after its fetch resolves, re-creating the ``.marginalia``
    element outside the dispatch cycle that wiped it. Gate that fetch and the
    mask is removed; leave it ungated and the defect is invisible.

    So behavioural coverage of this constraint is real but incidental,
    timing-dependent, and owned by a task that is not about the marginalia at
    all — three properties that make it exactly the kind of coverage that
    evaporates when someone refactors T14. This assertion is the deterministic
    one, which is why the position is pinned in source rather than trusted to
    review or to a neighbouring feature's async timing.

    The count assertion carries the uniqueness check the position comparison
    depends on. ``str.index`` takes the FIRST match, so a second
    ``wireMarginalia();`` anywhere in ``boot()`` would make the comparison below
    measure a call that is not the one doing the work — the non-unique-anchor
    trap ``_block`` exists to prevent, reappearing one level up. Two calls is
    also a real defect in its own right: ``wireMarginalia`` is idempotent, so
    the second is inert, and inert code that looks load-bearing is what a later
    reader relocates the WRONG one of.
    """
    assert 'import { wireMarginalia } from "/static/js/marginalia.js";' in js, (
        "js/main.js does not import the marginalia module, so the TOC and "
        "breadcrumbs never mount at all"
    )
    boot = _boot_body(js)
    assert boot.count("wireMarginalia();") == 1, (
        f"boot() calls wireMarginalia() {boot.count('wireMarginalia();')} times, "
        "expected exactly 1. Zero means the marginalia never mounts; two means "
        "the position assertion below measures whichever comes first, which may "
        "not be the call that matters."
    )
    assert boot.index("wireMarginalia();") > boot.index("subscribe(renderInspector)"), (
        "wireMarginalia() is called BEFORE subscribe(renderInspector). "
        "dispatch() runs listeners in registration order and renderInspector "
        "wipes #inspector with `host.textContent = \"\"`, so the marginalia "
        "block is drawn and erased on the same dispatch. Nothing throws and the "
        "browser suite stays green — it mounts the module itself — so this "
        "assertion is the only thing that can tell you the feature is gone."
    )


def check_tree_container_is_not_a_tab_stop(html: str) -> None:
    """FIX 1 — with a roving tabindex the container must not also be tabbable.

    Not converted: the browser tests count TREEITEMS carrying tabindex 0, and a
    tabbable container is not a treeitem — it would sail past every one of them
    while adding a focusable-but-inert stop before the rail.
    """
    tree = _block(html, '<div class="tree"', ">")
    assert 'role="tree"' in tree, "the tree lost its role"
    assert "tabindex" not in tree.lower(), (
        "the tree container is still a tab stop; with a roving tabindex that is "
        "a second, inert stop before the items"
    )


def check_subtree_ids_come_from_a_counter(js: str) -> None:
    """N10 — the one quarter of the aria-owns guard the browser cannot express.

    Three of the four assertions this guard used to make are now executed:
    presence (``test_folder_treeitems_own_their_subtree``), uniqueness
    (``test_every_aria_owns_id_is_unique_in_the_document``) and the per-render
    counter reset (``test_subtree_ids_do_not_grow_across_renders``).

    The fourth cannot be. Slugifying the folder path collides only when two
    OPEN folders have names that reduce to the same stem — ``q3-planning`` and
    ``q3 planning``, or two different non-ASCII names both reducing to empty.
    The browser fixture's folders are ``archive``, ``projects`` and ``q3``,
    which do not collide under any slugification, so the harness would stay
    green with the bug fully reintroduced. Making it expressible would mean
    adding a colliding pair to the fixture AND expanding every folder in the
    uniqueness test — at which point the guard would be asserting the fixture,
    not the code. So the positive form of the rule stays here as text: the id
    comes from a counter, and nothing derives it from the path.
    """
    assert "treeGroupSeq++" in js, (
        "subtree ids are not minted from a counter, so two folders whose names "
        "reduce to the same stem can collide on one IDREF — and an IDREF "
        "resolves to the FIRST match, making the second folder claim the "
        "first's subtree"
    )
    assert "child.path.replace(" not in js, (
        "subtree ids are derived from the folder path again — that is the "
        "collision this counter exists to prevent"
    )


def check_resize_is_not_inert(js: str) -> None:
    """N2 + N11 — the header wrapper is what puts the editor on the fill track.

    ``.inspector`` is ``grid-template-rows: auto 1fr``, so the body/editor must
    be the SECOND child. ``.back-btn`` is ``display: none`` above 780px and
    generates no box at all, so without a wrapper the fill target's row index
    changes with the viewport and the editor lands on the ``auto`` track at
    desktop width — filling nothing.

    KEPT rather than converted, on the same measured grounds as
    ``check_inspector_is_a_grid``: dropping layout.css wholesale left the
    browser harness at 15 passed, so the tests that measure the CONSEQUENCE of
    this layout also pass when there is no layout at all.

    An earlier attempt used a ``ResizeObserver`` to drop the editor out of flex
    growth on a user drag. It was MEASURED not to work: under ``flex: 1 1 auto``
    an inline height produces no size change, so the observer never fired at
    all. Its guard asserted the observer existed and was correctly conditioned
    — certifying inert code, the very defect this pass keeps removing.
    """
    assert 'el("div", "note-head")' in js, (
        "the inspector header is not wrapped, so .inspector has a variable "
        "number of grid rows and the editor is not reliably on the 1fr track"
    )
    assert "ResizeObserver" not in js, (
        "a ResizeObserver is back; under grid it is unnecessary, and under flex "
        "it was measured never to fire"
    )
    # Assert the PROPERTY, not a roster. Enumerating head's children missed the
    # <h1>: move the title back onto the host and the inspector gains a third
    # child, the header lands on `1fr`, and the editor stops filling — the exact
    # defect this guard names, passing green.
    render = _block(js, "function renderInspector(", "\nexport async function saveNote(")
    appends = render.count("host.appendChild(")
    assert appends == 5, (
        "renderInspector appends to the inspector host "
        f"{appends} times, expected 5 (empty | head | withheld | editor | body). "
        "`.inspector` is `grid-template-rows: auto 1fr`, so the body/editor must "
        "be the SECOND child on every path; an extra append pushes it off the "
        "fill track."
    )
    # Every non-empty path must put the head FIRST, so the fill child is second.
    head_at = render.index("host.appendChild(head)")
    for later in ("host.appendChild(el(\"p\", \"withheld\"", "host.appendChild(area)",
                  "host.appendChild(body)"):
        assert render.index(later) > head_at, (
            f"{later} is appended before the header, so it is not the second "
            "inspector child and does not land on the 1fr track"
        )


# DELETED with the stylesheet split — `check_measure`.
# It asserted the literal string "--measure: 48ch;".
# `tests/test_ui_browser.py::test_the_reading_measure_stays_in_the_classical_band`
# renders `.note-body` and divides its computed max-width by the measured
# average advance of a real lowercase alphabet in the font actually resolved,
# then asserts the classical 45-75 band. That is the property; 48ch was only
# the current means to it. A source guard on the number could not survive a
# type-scale change that kept the band correct, and could not notice a font
# stack change that broke it while the number stayed 48.


def check_color_scheme(css: str) -> None:
    """FIX 8 — native furniture follows ``color-scheme``, not the tokens.

    Asserts PLACEMENT, not a count. ``css.count(...) == 2`` was satisfied by two
    declarations sitting in the same block while the other went without — a
    stylesheet that is broken in exactly the way this guard exists to catch.
    """
    assert "color-scheme: light;" in css, ":root never declares color-scheme"

    # Delimited by the START of the next top-level block, not by a guessed
    # brace pattern: the media query's closing braces are indented, so "\n}\n}"
    # never matched and _block raised ValueError instead of asserting.
    media = _block(css, "@media (prefers-color-scheme: dark) {",
                   ':root[data-theme="dark"] {')
    assert "color-scheme: dark;" in media, (
        "the prefers-color-scheme dark block does not set color-scheme, so a "
        "user on a dark OS gets light scrollbars and date pickers"
    )
    manual = _block(css, ':root[data-theme="dark"] {', "\n}")
    assert "color-scheme: dark;" in manual, (
        "the [data-theme='dark'] block does not set color-scheme, so the manual "
        "toggle leaves native furniture painted light — the exact case the "
        "theme switcher exists for"
    )


def check_ink_faint_used_once(css: str) -> None:
    """FIX 9, part 1 of 3 — the whole-stylesheet half, so it takes ALL the CSS.

    ``--ink-faint`` fails AA as text on every ground in both themes: 2.69-3.03:1
    in light, 3.34-3.84:1 in dark, measured against the three paper tokens.
    ``--ink-muted`` is 5.66-7.27:1 over the same six pairings.

    "Used exactly once" is the only assertion here that cannot be scoped to a
    single file — a second use appearing in base.css or layout.css is exactly
    the regression, so the guard has to see all four. It is handed the
    concatenation for that reason and no other.
    """
    # COMMENTS STRIPPED FIRST. Fourth instance of this trap in this file, and the
    # first in CSS: `check_every_stylesheet_is_linked_in_order` grepped `@import`
    # against prose explaining why there is no `@import`; `check_single_innerhtml`
    # counts `.innerHTML` with the dot because two modules document the rule in
    # words; `_boot_body` strips JS comments because a guard was blinded by its
    # own justification. Here, `reading.css` carried a comment warning that "a
    # future `var(--ink-faint)` added below reddens the suite" — and reddened it
    # by naming the token.
    #
    # Rewording that one comment would have fixed one instance and left the trap
    # armed for the next author — and the next author is precisely the one who
    # documents the rule, i.e. the conscientious one. A guard that punishes
    # writing down why it exists is worse than the drift it prevents.
    # The rule is "the token is USED once", and a mention is not a use.
    visible = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    uses = [line.strip() for line in visible.splitlines() if "var(--ink-faint)" in line]
    assert len(uses) == 1, (
        f"--ink-faint must be used exactly once (.search-glyph, decorative); "
        f"found {len(uses)}: {uses}"
    )
    assert uses[0].startswith(".search-glyph"), (
        f"the one --ink-faint use is not the decorative glyph: {uses[0]}"
    )


def check_ink_faint_defined_in_all_scopes(css: str) -> None:
    """FIX 9, part 2 of 3 — the token DEFINITIONS, which live in tokens.css.

    PLACEMENT, per scope. This was once a COUNT wearing a placement comment, and
    the comment was false: ``css.count("--ink-faint:") == 3`` is satisfied by
    THREE definitions in ``:root`` and ZERO in either dark block — verified by
    constructing exactly that stylesheet, on which the old assertion passed
    while the token was absent from both dark themes. A comment asserting a
    property is not the property — including inside a test whose job is to
    prevent that.

    The token is deliberately NOT retuned: it is one tier of a three-tier scale
    a later redesign re-cuts, and darkening it enough to pass collapses it into
    ``--ink-muted``. So it must remain DEFINED in all three scopes even though
    only decoration may use it.
    """
    scopes = {
        ":root": _block(css, ":root {", "\n}"),
        "prefers-color-scheme dark": _block(
            css, "@media (prefers-color-scheme: dark) {", ':root[data-theme="dark"] {'
        ),
        '[data-theme="dark"]': _block(css, ':root[data-theme="dark"] {', "\n}"),
    }
    for scope, block in scopes.items():
        assert "--ink-faint:" in block, (
            f"--ink-faint is not defined in {scope}. The redesign owns retuning "
            "the token — this fix only repointed the rules that used it for "
            "text, so it must remain defined in all three scopes."
        )


def check_quiet_text_uses_muted(css: str) -> None:
    """FIX 9, part 3 of 3 — the six repointed rules, all in components.css.

    KEPT as a source guard rather than converted, and the reason is specific.
    ``test_quiet_text_meets_AA_against_its_own_background`` measures a real
    contrast ratio, which is strictly better evidence — but it measures ONE
    element (``.meta-sub``, falling back to ``.gutter``) in ONE theme.

    SEVEN rules were repointed, re-derived by counting ``--ink-faint`` consumers
    in ``git show HEAD:src/brain/ui/static/app.css`` — **eight** then, exactly
    **one** now (``components.css:116``, the aria-hidden ``.search-glyph``). Six
    of the seven are the text rules this guard sweeps; ``.twisty`` is the
    seventh and is listed separately below because a UI glyph is judged at 3:1,
    not 4.5:1.

    Of the six, five are not on screen in that browser test and the dark theme
    is never rendered at all. Partial coverage is not coverage, so the sweep
    stays until something executes all six in both themes.

    (An earlier revision said "six rules were repointed", counting the loop
    below and silently dropping ``.twisty``.)
    """
    # The six text rules that were repointed. `.twisty` is the seventh repoint
    # but is a UI glyph judged at 3:1, so it is listed separately in the CSS.
    for selector in ("#q::placeholder", "kbd.hint", ".meta-sub", ".gutter",
                     ".empty, .error-state", ".wikilink--unresolved"):
        # The trailing " {" is load-bearing: a bare ".gutter" matches the
        # `.mono, .meta-line, .gutter, kbd { font-variant-numeric: ... }` rule
        # first, so the guard would inspect a completely different block and
        # report a contrast failure that did not exist. Since the split that
        # rule lives in base.css and this guard only ever sees components.css,
        # which removes the collision — but the anchor stays explicit, because
        # relying on a file boundary to disambiguate is the same bet that
        # failed before, just with a longer fuse.
        rule = _block(css, f"{selector} {{", "}")
        assert "var(--ink-muted)" in rule, (
            f"{selector} no longer uses --ink-muted, so it is back below AA"
        )


def check_editor_sizing(css: str) -> None:
    """FIX 6 + N2 — the `.editor` half, in components.css.

    KEPT, after an attempt to retire it was MEASURED to be wrong. The reasoning
    for retiring it was that
    ``test_the_editor_fills_the_pane_and_a_resize_drag_wins`` settles all of
    this by measurement. It does not, and the experiment that showed so is
    worth recording because the conclusion is counter-intuitive:

        Deleting the ``layout.css`` <link> from index.html entirely — no
        ``.inspector`` grid, no ``1fr`` track, no stretch opt-in — left the
        browser harness at **15 passed**.

    All three of that test's assertions survive in ordinary block flow: the
    textarea's own ``min-height: 26rem`` is 416px, which clears the ">300px, so
    it fills" bar on its own; and an inline ``style.height`` wins in normal flow
    just as it does in grid. The test discriminates grid from FLEX-GROW, which
    is the bug it was written for. It does not discriminate grid from nothing.

    So `resize: vertical` is not the only assertion here without a behavioural
    expression — none of them have one. "Is it present" and "does it still do
    anything" are different questions, and so are "does the test pass" and
    "would the test fail if this were wrong".
    """
    editor = _block(css, "\n.editor {", "}")
    assert "clamp(" not in editor, (
        "back to sizing by arithmetic. `clamp(26rem, 60vh, 100%)` was DEAD "
        "CODE: .inspector's content box is ~93vh, so min(60vh, 100%) was always "
        "60vh and the 100% guard could never engage."
    )
    assert "flex:" not in editor, (
        "flex-grow is back on .editor, which makes `resize: vertical` inert — "
        "measured: a drag to 180px left the element at 456px"
    )
    assert "min-height: 26rem" in editor, "the short-viewport floor was removed"
    # The drag HANDLE specifically. Playwright cannot drag a native resize
    # gripper — it is user-agent chrome, not a DOM element — and the browser
    # test drives `style.height` from script, which works with or without this
    # declaration. Deleting it removes the affordance from the UI and leaves
    # that test fully green.
    assert "resize: vertical" in editor, (
        "the user's own resize handle was removed from .editor. The browser "
        "test does NOT cover this: it sets style.height directly, which works "
        "with or without the declaration."
    )


def check_inspector_is_a_grid(css: str) -> None:
    """FIX 6 — the `.inspector` half, now in layout.css.

    Kept for the reason given in :func:`check_editor_sizing`: dropping this
    file wholesale did not move the browser harness off 15 passed. Grid is what
    makes an explicit height beat the layout — measured, a drag to 180px left a
    flex child at 456px and moved a grid child to exactly 180px — but the test
    that measures the *consequence* also passes when there is no layout at all.
    """
    inspector = _block(css, ".inspector { padding: var(--s-6) var(--s-7);", "}")
    assert "display: grid" in inspector, (
        ".inspector is not a grid, so a user's resize height cannot win over "
        "the layout and `resize: vertical` on .editor is decoration"
    )
    assert "grid-template-rows: auto 1fr" in inspector, (
        ".inspector has no 1fr row, so the body/editor does not fill and the "
        "dead space Fix 6 was commissioned to remove comes back"
    )


def check_layout_precedes_components(html: str) -> None:
    """``components.css``'s responsive overrides win on ORDER alone.

    Found by the investigation into #62 — which refuted the defect it was sent
    to confirm and turned up this one instead. ``.shell``, ``.ledger`` and
    ``.inspector`` are declared in BOTH ``layout.css`` and ``components.css``,
    and in components.css they sit inside ``@media (max-width: 1100px)`` and
    ``@media (max-width: 780px)``. Same selector, same specificity, overlapping
    properties — so the later sheet wins, and "later" is the only thing making
    the narrow-viewport layout apply at all. Load components.css first and the
    desktop grid silently returns at phone widths.

    **Why this is pinned and ``.rail-head``'s order was not.** That pair is also
    same-selector and same-specificity, but its declaration sets are DISJOINT —
    components.css never declares ``flex-wrap`` or ``row-gap`` — so there is no
    competing declaration for the cascade to resolve and reordering is inert.
    Measured in a browser, both orders, identical. A guard there would pass, its
    swap-mutation would redden it, and it would look properly proven while
    asserting nothing about harm: a change-detector wearing the costume of a
    proof. **Overlap, not co-declaration, is what creates an order dependency.**

    **Asserted on index.html rather than on CSS_ORDER**, deliberately. The
    browser applies sheets in <link> order, so that is where the property lives;
    ``CSS_ORDER`` is a test-side mirror of it, kept honest by
    :func:`check_every_stylesheet_is_linked_in_order`. Pinning the mirror would
    also have been unmutable by this file's harness, which mutates source
    strings and cannot reach a module global.

    **This covers ONE PAIR, and reading it as more would be the #46 error.** The
    general rule — no sheet may redefine a property already declared for the
    same selector by an earlier sheet without an explicit pin — is still
    unasserted. Four cross-sheet overlaps exist; three are this pair's selectors
    and one (``.rail``) is inert because both declarations are identical. A real
    check needs a CSS parser, not a regex: the prototype scan got ``@media``
    wrong on its first pass, which is precisely the mistake such a check cannot
    afford.
    """
    links = re.findall(r'<link rel="stylesheet" href="/static/css/([^"]+)">', html)
    assert "layout.css" in links and "components.css" in links, (
        f"one of the two ordered sheets is not linked at all: {links}"
    )
    assert links.index("layout.css") < links.index("components.css"), (
        "components.css is loaded BEFORE layout.css. Its @media overrides for "
        ".shell/.ledger/.inspector are same-specificity as layout.css's base "
        "rules and win only by being later, so this silently restores the "
        "three-column desktop grid at <=1100px and <=780px — a layout defect "
        "with no error and no other failing test. components.css:420 documents "
        "the dependency in prose; prose does not fail a build."
    )


def check_every_stylesheet_is_linked_in_order(html: str) -> None:
    """The guard the SPLIT itself created, and nothing else covers.

    One ``app.css`` could not be half-loaded. A set of files can, and the failure is
    silent in the worst way: the page still renders, still passes every browser
    test, and is simply missing a tier of the design. That is not hypothetical
    — it is what the layout.css experiment above demonstrated.

    Asserts presence AND ORDER, because order is the cascade. `.deferred-tab`
    vs `.shell`, and every responsive override, are same-specificity pairs
    settled only by which file loaded first; a correct set of links in the
    wrong sequence is a real defect that nothing else here would notice.

    Also refuses `@import`, which would reintroduce the serialised round trip
    the four-link form exists to avoid, and would move the real load order out
    of this file where this guard can no longer see it.
    """
    links = re.findall(r'<link rel="stylesheet" href="/static/css/([^"]+)">', html)
    assert links == list(CSS_ORDER), (
        f"index.html links {links}, expected exactly {list(CSS_ORDER)} in that "
        "order. A missing sheet renders a page that still passes the browser "
        "harness (verified: dropping layout.css left it at 15 passed), and a "
        "reordered one silently flips every same-specificity pair — the "
        ".deferred-tab/.shell display conflict and all of the responsive "
        "overrides."
    )
    # COMMENTS STRIPPED FIRST, and this is a correctness fix, not tidying. The
    # `@import` check shipped BLIND: index.html's own header comment explains
    # why the stylesheet is separate <link> tags "rather than one file with @import"
    # and says the word twice, so `"@import" not in html` was False on the
    # shipped source and this guard raised on every run. Its mutation entry
    # passed for the same reason — the guard raised whether or not the mutation
    # was applied, which is a test certifying nothing.
    #
    # Third instance of this exact trap in this file. `check_aria_current` notes
    # "The CALL, not the word... Same trap as counting bare innerHTML", and
    # `check_single_innerhtml` counts `.innerHTML` with the dot for the same
    # reason. The prose that documents a rule will always contain the words the
    # rule forbids. `tests/test_ui_static_assets.py::
    # test_no_inline_script_or_style_in_the_shell` already strips comments with
    # this exact substitution, for this exact reason.
    markup = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    assert "@import" not in markup, (
        "an @import is back; the stylesheet must stay separate <link> tags so the "
        "preload scanner can fetch them in parallel"
    )


def check_stretch_is_opt_in(css: str) -> None:
    """N8 — the 1fr track hands its height to whatever lands on it.

    Three container-mode changes have each broken a different child:
    ``.back-btn`` (flex column, full-bleed), ``resize: vertical`` (flex-grow,
    inert), and ``.withheld`` (grid stretch — MEASURED at 404px in a 600px pane,
    a one-line notice rendered as a full-height sunk block). Patching victims
    one at a time guarantees a fourth, so stretching is opt-in and only
    ``.editor`` opts in.
    """
    assert ".inspector > * { align-self: start; }" in css, (
        "inspector children stretch by default again, so any child with a "
        "background or border renders as a full-height block"
    )
    assert ".inspector > .editor { align-self: stretch; }" in css, (
        ".editor no longer opts into stretch, so it does not fill the 1fr track"
    )
    # And EXACTLY one opt-in. Asserting only that the two rules exist leaves the
    # list free to grow: each addition is individually defensible, and together
    # they restore the coin-flip default the rule was built to remove. Same
    # shape as the _block uniqueness check — make the erosion inexpressible
    # rather than something a future reviewer has to notice.
    opt_ins = [
        line.strip() for line in css.splitlines()
        if line.strip().startswith(".inspector >") and "align-self: stretch" in line
    ]
    assert opt_ins == [".inspector > .editor { align-self: stretch; }"], (
        "the stretch opt-in list must contain EXACTLY the editor; found "
        f"{opt_ins}. Every additional opt-in is individually reasonable and "
        "collectively returns .inspector to 'children stretch unless someone "
        "remembered' — which is what broke .back-btn, resize and .withheld."
    )


#: Every guard, paired with a defect that must make it fail, and the number of
#: times its anchor is expected to occur.
#:
#: THE COUNT IS NOT BOOKKEEPING. A mutation that silently matches nothing — or
#: matches once when the author meant twice — leaves the guard reading unchanged
#: source, so ``pytest.raises`` fails and the entry looks broken, or worse, some
#: OTHER assertion in the same guard fires and the entry looks fine while
#: testing nothing it was written to test. That is not hypothetical: the sweep
#: that audited this file used ``str.replace(..., 1)`` against strings occurring
#: twice in the old ``app.js`` and briefly recorded two guards as blind that
#: were not. Declaring the count makes the mistake fail loudly at the point it
#: is made.
GUARDS: list[tuple[str, object, str, str, str, int]] = [
    # ---------------------------------------------------------------- js --
    ("single-innerhtml", check_single_innerhtml, "js/*",
     'const body = el("div", "note-body");',
     'const body = el("div", "note-body"); back.innerHTML = "x";', 1),
    # The split's own new failure mode for this rule: a SECOND sink appearing in
    # a different module, which the old per-file guard could not have seen.
    ("second-innerhtml-in-another-module", check_single_innerhtml, "js/*",
     'const chips = el("div", "chips");',
     'const chips = el("div", "chips"); chips.innerHTML = "x";', 1),
    # IDENTITY, not count: renaming the sink keeps `.innerHTML` at exactly one,
    # so the count assertion stays green and only "the one innerHTML is not the
    # body" can fire. A guard that merely counts sinks would accept an
    # innerHTML on any element in the front end.
    ("innerhtml-is-not-the-body", check_single_innerhtml, "js/*",
     "body.innerHTML = note.html;", "bodyZ.innerHTML = note.html;", 1),
    # REACHABILITY, not presence. Both entries mutate js/main.js — the file the
    # integrator owns — because "the module exists and its own tests pass" and
    # "a user can reach it" are different propositions, and this phase shipped
    # the first while believing the second.
    ("palette-module-imported", check_the_palette_is_wired_into_boot, "js/main.js",
     'import { wirePalette } from "/static/js/palette.js";\n', "", 1),
    # The import SURVIVES here, so the first assertion stays green and only the
    # boot() call assertion can fire. An import with no call is precisely the
    # half-wiring that looks correct in a diff.
    ("palette-wired-in-boot", check_the_palette_is_wired_into_boot, "js/main.js",
     "wireTabs(); wireKeys(); wireControls(); wirePalette();",
     "wireTabs(); wireKeys(); wireControls();", 1),
    # T16's control. Deletes ONLY the trailing call, so the rest of the line
    # survives and no other boot guard's anchor moves — `palette-wired-in-boot`
    # above matches this same line as a prefix, and a mutation that disturbed it
    # would fire the palette guard instead of this one.
    ("ingested-toggle-wired-in-boot", check_the_ingested_toggle_is_wired_into_boot,
     "js/main.js", " wireIngestedToggle();", "", 1),
    # T18's thread mode. Its handover said ordering was not sensitive; a browser
    # probe measured otherwise (see the guard docstring), so the position is
    # pinned exactly as the marginalia's is.
    ("thread-wired-in-boot", check_the_thread_is_wired_after_the_inspector,
     "js/main.js", " wireThread();", "", 1),
    # RELOCATES rather than deletes, so the count stays 1 and only the position
    # assertion can fire — the `inspector-head-is-first` lesson.
    # SWAPS TWO WHOLE LINES, so every marginalia anchor — which spans the
    # subscribe line — survives intact. The first version put wireThread() on
    # that same line; relocating it then invalidated four marginalia anchors as
    # collateral and the run reddened 8 entries instead of this one.
    ("thread-wired-after-the-inspector", check_the_thread_is_wired_after_the_inspector,
     "js/main.js",
     "  subscribe(renderTree); subscribe(renderResults); "
     "subscribe(renderInspector); wireMarginalia();\n  wireThread();",
     "  wireThread();\n  subscribe(renderTree); subscribe(renderResults); "
     "subscribe(renderInspector); wireMarginalia();", 1),
    ("marginalia-module-imported", check_the_marginalia_is_wired_after_the_inspector,
     "js/main.js",
     'import { wireMarginalia } from "/static/js/marginalia.js";\n', "", 1),
    # The import SURVIVES, so only the count assertion can fire — and it fires on
    # ZERO.
    ("marginalia-wired-in-boot", check_the_marginalia_is_wired_after_the_inspector,
     "js/main.js",
     "subscribe(renderInspector); wireMarginalia();",
     "subscribe(renderInspector);", 1),
    # The same assertion's OTHER half, which zero cannot reach: a duplicate call.
    # `str.index` would then measure the first, so the position assertion below
    # would report on a call that is not the one doing the work.
    ("marginalia-call-is-not-duplicated",
     check_the_marginalia_is_wired_after_the_inspector, "js/main.js",
     "  subscribe(renderTree); subscribe(renderResults); "
     "subscribe(renderInspector); wireMarginalia();",
     "  wireMarginalia();\n"
     "  subscribe(renderTree); subscribe(renderResults); "
     "subscribe(renderInspector); wireMarginalia();", 1),
    # RELOCATES, it does not delete — the lesson `inspector-head-is-first` was
    # rewritten for. Deleting the call would drop the count to 0 and fire the
    # assertion ABOVE, leaving the position assertion this entry exists for
    # never evaluated, green on all three clauses and proving the wrong thing.
    # Moving it keeps the count at exactly 1 so only the position can fail.
    ("marginalia-wired-after-the-inspector",
     check_the_marginalia_is_wired_after_the_inspector, "js/main.js",
     "  subscribe(renderTree); subscribe(renderResults); "
     "subscribe(renderInspector); wireMarginalia();",
     "  wireMarginalia(); subscribe(renderTree); subscribe(renderResults); "
     "subscribe(renderInspector);", 1),
    ("tree-tab-stop", check_tree_container_is_not_a_tab_stop, "index.html",
     '<div class="tree" id="tree" role="tree" aria-label="Vault notes">',
     '<div class="tree" id="tree" role="tree" aria-label="Vault notes" tabindex="0">', 1),
    ("tree-keeps-its-role", check_tree_container_is_not_a_tab_stop, "index.html",
     '<div class="tree" id="tree" role="tree" aria-label="Vault notes">',
     '<div class="tree" id="tree" aria-label="Vault notes">', 1),
    ("subtree-ids-from-counter", check_subtree_ids_come_from_a_counter, "js/tree.js",
     "subtree.id = `tree-group-${treeGroupSeq++}`;",
     'subtree.id = `tree-group-${child.path.replace(/[^A-Za-z0-9]+/g, "-")}`;', 1),
    # The counter SURVIVES here, so the first assertion stays green and only
    # "ids are derived from the path again" can fire. Belt-and-braces ids of the
    # form `tree-group-0-my-folder` are individually plausible and collectively
    # reintroduce the collision, because the path half is what a reader trusts.
    ("subtree-ids-not-path-derived", check_subtree_ids_come_from_a_counter,
     "js/tree.js",
     "subtree.id = `tree-group-${treeGroupSeq++}`;",
     "subtree.id = `tree-group-${treeGroupSeq++}"
     '-${child.path.replace(/[^A-Za-z0-9]+/g, "-")}`;', 1),
    ("note-head-wrapper", check_resize_is_not_inert, "js/inspector.js",
     'const head = el("div", "note-head");', 'const head = host;', 1),
    ("inspector-append-count", check_resize_is_not_inert, "js/inspector.js",
     "  host.appendChild(head);",
     "  host.appendChild(head); host.appendChild(el(\"span\"));", 1),
    # ORDER, at an unchanged count of five — and the anchor has to span the
    # whole block for that to be true.
    #
    # The FIRST version of this entry deleted `  host.appendChild(head);` and
    # claimed in this very comment that it "keeps `appends == 5` green — so
    # only the ordering assertion can fire". It did not: the count dropped 5→4
    # and `assert appends == 5` (:286) fired instead, so the ordering assertion
    # at :297 was never evaluated by anything. The entry passed all three
    # clauses of `test_every_guard_can_fail` while proving a different
    # proposition than its name — this file's own failure class, one level up:
    # the clauses prove *a* guard noticed, not that *the assertion under test*
    # noticed. Clause (d) below exists because of exactly this, and it names
    # this line if it regresses.
    #
    # So the mutation RELOCATES: it drops the append from the top and re-adds it
    # after the withheld notice. Do not shorten the anchor back to the two
    # `appendChild` lines — that is a deletion again.
    ("inspector-head-is-first", check_resize_is_not_inert, "js/inspector.js",
     "  head.appendChild(fm);\n"
     "  host.appendChild(head);\n"
     "\n"
     "  if (note.withheld) {\n"
     "    /* `withheld` is present ONLY when the body was withheld — same key and\n"
     "       same message vocabulary as MCP brain_show, so a client handles one\n"
     "       spelling across both surfaces. */\n"
     '    host.appendChild(el("p", "withheld", note.withheld));',
     "  head.appendChild(fm);\n"
     "\n"
     "  if (note.withheld) {\n"
     "    /* `withheld` is present ONLY when the body was withheld — same key and\n"
     "       same message vocabulary as MCP brain_show, so a client handles one\n"
     "       spelling across both surfaces. */\n"
     '    host.appendChild(el("p", "withheld", note.withheld));\n'
     "    host.appendChild(head);", 1),
    ("no-resize-observer", check_resize_is_not_inert, "js/inspector.js",
     "  const fm = el(\"div\", \"frontmatter\");",
     "  new ResizeObserver(() => {}); const fm = el(\"div\", \"frontmatter\");", 1),
    # --------------------------------------------------------------- css --
    ("color-scheme-light", check_color_scheme, "css/tokens.css",
     "color-scheme: light;", "", 1),
    ("color-scheme-dark", check_color_scheme, "css/tokens.css",
     ':root[data-theme="dark"] {\n  color-scheme: dark;\n',
     ':root[data-theme="dark"] {\n', 1),
    ("ink-faint-text", check_quiet_text_uses_muted, "css/components.css",
     ".gutter {\n  font-family: var(--font-mono); font-size: var(--t-meta);\n"
     "  color: var(--ink-muted);",
     ".gutter {\n  font-family: var(--font-mono); font-size: var(--t-meta);\n"
     "  color: var(--ink-faint);", 1),
    # The other five repointed rules. `.gutter` above was the only one with a
    # mutation, so five of the six selectors in that loop were asserted and
    # never proven able to fail — a loop is one line of code but six
    # independent claims.
    ("ink-faint-placeholder", check_quiet_text_uses_muted, "css/components.css",
     "#q::placeholder { color: var(--ink-muted); }",
     "#q::placeholder { color: var(--ink-faint); }", 1),
    ("ink-faint-kbd-hint", check_quiet_text_uses_muted, "css/components.css",
     "kbd.hint {\n  font-family: var(--font-mono); font-size: var(--t-meta);\n"
     "  color: var(--ink-muted);",
     "kbd.hint {\n  font-family: var(--font-mono); font-size: var(--t-meta);\n"
     "  color: var(--ink-faint);", 1),
    ("ink-faint-meta-sub", check_quiet_text_uses_muted, "css/components.css",
     ".meta-sub {\n  font-family: var(--font-mono); font-size: var(--t-meta);\n"
     "  color: var(--ink-muted);\n}",
     ".meta-sub {\n  font-family: var(--font-mono); font-size: var(--t-meta);\n"
     "  color: var(--ink-faint);\n}", 1),
    ("ink-faint-empty-state", check_quiet_text_uses_muted, "css/components.css",
     ".empty, .error-state { color: var(--ink-muted); font-style: italic; }",
     ".empty, .error-state { color: var(--ink-faint); font-style: italic; }", 1),
    ("ink-faint-unresolved-wikilink", check_quiet_text_uses_muted,
     "css/components.css",
     ".wikilink--unresolved { color: var(--ink-muted); text-decoration: line-through;",
     ".wikilink--unresolved { color: var(--ink-faint); text-decoration: line-through;",
     1),
    # The stylesheet-wide half of FIX 9, mutated across the CONCATENATION: a
    # second --ink-faint use reappearing in ANY sheet in CSS_ORDER is the
    # regression, so this is the one guard that must not be scoped to a file.
    ("ink-faint-used-once", check_ink_faint_used_once, "css/*",
     ".twisty { width: 0.8em; display: inline-block; color: var(--ink-muted); }",
     ".twisty { width: 0.8em; display: inline-block; color: var(--ink-faint); }", 1),
    # IDENTITY again, and the count cannot reach it: renaming the selector on
    # the ONE permitted use keeps the total at exactly one, so only "the one
    # --ink-faint use is not the decorative glyph" can fire. Without this, the
    # token could migrate from an aria-hidden ornament onto real text and the
    # guard would still report a compliant stylesheet.
    ("ink-faint-use-is-the-glyph", check_ink_faint_used_once, "css/*",
     ".search-glyph { color: var(--ink-faint); }",
     ".twisty-alt { color: var(--ink-faint); }", 1),
    # `.editor` sizing, in components.css. Retained rather than retired: see the
    # docstring — dropping layout.css wholesale left the browser harness green,
    # so its measurements do not stand in for these.
    ("editor-not-clamped", check_editor_sizing, "css/components.css",
     "  width: 100%; min-height: 26rem;",
     "  width: 100%; min-height: 26rem; height: clamp(26rem, 60vh, 100%);", 1),
    ("editor-no-flex-grow", check_editor_sizing, "css/components.css",
     "  width: 100%; min-height: 26rem;",
     "  width: 100%; min-height: 26rem; flex: 1 1 auto;", 1),
    ("editor-floor", check_editor_sizing, "css/components.css",
     "min-height: 26rem;", "", 1),
    ("editor-resize", check_editor_sizing, "css/components.css",
     "resize: vertical;", "", 1),
    # `.inspector` grid, in layout.css — the other half of the same fix.
    ("inspector-is-grid", check_inspector_is_a_grid, "css/layout.css",
     "display: grid; grid-template-rows: auto 1fr;",
     "display: flex; flex-direction: column;", 1),
    ("editor-fill-track", check_inspector_is_a_grid, "css/layout.css",
     "grid-template-rows: auto 1fr;", "grid-template-rows: auto auto;", 1),
    # The split's own new failure mode: a stylesheet that is simply not linked.
    ("stylesheet-link-missing", check_every_stylesheet_is_linked_in_order,
     "index.html",
     '<link rel="stylesheet" href="/static/css/layout.css">\n', "", 1),
    # The palette sheet SPECIFICALLY. `stylesheet-link-missing` above already
    # fires this assertion by dropping layout.css, so this entry is not needed
    # for clause (d) — it is here because the new <link> is the half of the
    # palette wiring that is invisible in a browser test: the six palette tests
    # attach the sheet at runtime themselves, so an unlinked palette.css ships a
    # working, entirely unstyled dialog with every test green.
    ("stylesheet-palette-link-missing", check_every_stylesheet_is_linked_in_order,
     "index.html",
     '<link rel="stylesheet" href="/static/css/palette.css">\n', "", 1),
    # The marginalia sheet SPECIFICALLY, for the same reason as the palette
    # entry above and with the same non-necessity: clause (d) is already
    # satisfied. It is here because the eight T13 browser tests attach
    # marginalia.css at runtime themselves, so an unlinked sheet ships a fully
    # working, entirely unstyled TOC with every test green — "present, wired and
    # visibly wrong" rather than absent, which is the hardest of these to notice.
    ("stylesheet-marginalia-link-missing", check_every_stylesheet_is_linked_in_order,
     "index.html",
     '<link rel="stylesheet" href="/static/css/marginalia.css">\n', "", 1),
    # The reading sheet SPECIFICALLY — third instance of the same shape, and the
    # argument is MEASURED here rather than inherited from the two entries above:
    # `_attach_reading_css` in tests/test_ui_browser_lede.py appends its own
    # <link> at runtime, and `test_reading_css_parses` is described there as "the
    # one test that would notice reading.css going missing". So that suite proves
    # the sheet PARSES and paints four distinguishable link kinds while proving
    # nothing about it reaching a user — unlinked, reading.css ships a lede and
    # four link kinds with no styling at all and every test green.
    ("stylesheet-reading-link-missing", check_every_stylesheet_is_linked_in_order,
     "index.html",
     '<link rel="stylesheet" href="/static/css/reading.css">\n', "", 1),
    # The discovery sheet SPECIFICALLY. Fourth of this shape, and the argument
    # differs from the three above in a way worth stating: the T17 browser suite
    # does NOT attach discovery.css at runtime, so an unlinked sheet here would
    # not merely ship an unstyled surface — it would ship one whose rows have no
    # `display: grid`, no hover and no focus ring, while every T17 test still
    # passes because they assert text and behaviour rather than paint.
    ("stylesheet-discovery-link-missing", check_every_stylesheet_is_linked_in_order,
     "index.html",
     '<link rel="stylesheet" href="/static/css/discovery.css">\n', "", 1),
    # Fourth of this shape (palette, marginalia, reading, explorer). Same reason
    # each time: nothing else can see an unlinked sheet. T16's four classes
    # render as plain text without it and every structural test stays green —
    # which is exactly the state that made this sheet necessary.
    ("stylesheet-explorer-link-missing", check_every_stylesheet_is_linked_in_order,
     "index.html",
     '<link rel="stylesheet" href="/static/css/explorer.css">\n', "", 1),
    # Fifth of this shape (palette, marginalia, reading, explorer, thread).
    ("stylesheet-thread-link-missing", check_every_stylesheet_is_linked_in_order,
     "index.html",
     '<link rel="stylesheet" href="/static/css/thread.css">\n', "", 1),
    # Swaps the two sheets whose ORDER is load-bearing. Distinct from
    # `stylesheet-link-order` above, which swaps base/layout to prove the
    # roster-vs-links equality assertion; this one proves a CASCADE property
    # that survives a consistent reorder of both.
    # The presence half. Without it, a missing sheet makes `.index()` raise
    # ValueError rather than AssertionError, so clause (c)'s pytest.raises would
    # not catch it and the guard would fail in a shape this harness cannot
    # attribute. Shares an anchor with `stylesheet-link-missing`, which is fine:
    # the two entries drive different guards.
    ("layout-before-components-needs-both", check_layout_precedes_components,
     "index.html",
     '<link rel="stylesheet" href="/static/css/layout.css">\n', "", 1),
    ("layout-before-components", check_layout_precedes_components, "index.html",
     '<link rel="stylesheet" href="/static/css/layout.css">\n'
     '<link rel="stylesheet" href="/static/css/components.css">',
     '<link rel="stylesheet" href="/static/css/components.css">\n'
     '<link rel="stylesheet" href="/static/css/layout.css">', 1),
    ("stylesheet-link-order", check_every_stylesheet_is_linked_in_order,
     "index.html",
     '<link rel="stylesheet" href="/static/css/base.css">\n'
     '<link rel="stylesheet" href="/static/css/layout.css">',
     '<link rel="stylesheet" href="/static/css/layout.css">\n'
     '<link rel="stylesheet" href="/static/css/base.css">', 1),
    # MUST land outside a comment. The previous mutation inserted the @import
    # INSIDE index.html's header comment, where the guard now correctly ignores
    # it — and where the old, comment-blind guard "caught" it only because it
    # was already failing on the unmutated source.
    ("stylesheet-no-import", check_every_stylesheet_is_linked_in_order,
     "index.html",
     '<link rel="stylesheet" href="/static/css/components.css">',
     '<link rel="stylesheet" href="/static/css/components.css">\n'
     '<style>@import url("/static/css/all.css");</style>', 1),
    # N8: stretching must stay opt-in, or the next inspector child breaks.
    ("stretch-opt-in", check_stretch_is_opt_in, "css/layout.css",
     ".inspector > * { align-self: start; }", "", 1),
    ("editor-opts-into-stretch", check_stretch_is_opt_in, "css/layout.css",
     ".inspector > .editor { align-self: stretch; }", "", 1),
    # A THIRD opt-in must fail, not merely be noticed by a future reader.
    ("no-extra-stretch-opt-in", check_stretch_is_opt_in, "css/layout.css",
     ".inspector > .editor { align-self: stretch; }",
     ".inspector > .editor { align-self: stretch; }\n"
     ".inspector > .note-body { align-self: stretch; }", 1),
    # PLACEMENT guards: each must fail when a declaration moves out of its block
    # rather than merely disappearing from the file.
    ("color-scheme-media-block", check_color_scheme, "css/tokens.css",
     "  :root:not([data-theme=\"light\"]) {\n    color-scheme: dark;",
     "  :root:not([data-theme=\"light\"]) {", 1),
    ("color-scheme-manual-block", check_color_scheme, "css/tokens.css",
     ":root[data-theme=\"dark\"] {\n  color-scheme: dark;",
     ":root[data-theme=\"dark\"] {", 1),
    # Anchored on the [data-theme="dark"] block's own line, INCLUDING the
    # preceding declaration, so it cannot also match the 4-space media-query
    # line. Third occurrence of the anchor-uniqueness trap today.
    ("ink-faint-defined-in-all-scopes", check_ink_faint_defined_in_all_scopes,
     "css/tokens.css",
     '  --ink:          oklch(91.0% 0.012 85);\n'
     '  --ink-muted:    oklch(70.0% 0.010 80);\n'
     '  --ink-faint:    oklch(54.0% 0.008 80);',
     '  --ink:          oklch(91.0% 0.012 85);\n'
     '  --ink-muted:    oklch(70.0% 0.010 80);', 1),
    # The guard loops over THREE scopes; the entry above only ever removed the
    # token from the manual `[data-theme="dark"]` block, so two thirds of it
    # were asserted and never proven able to fail. The light `:root` values and
    # the 4-space media-query indentation each make these anchors unique.
    ("ink-faint-defined-in-root", check_ink_faint_defined_in_all_scopes,
     "css/tokens.css",
     '  --ink-muted:    oklch(48.0% 0.010 60);\n'
     '  --ink-faint:    oklch(66.0% 0.008 60);',
     '  --ink-muted:    oklch(48.0% 0.010 60);', 1),
    ("ink-faint-defined-in-media-block", check_ink_faint_defined_in_all_scopes,
     "css/tokens.css",
     '    --ink-muted:    oklch(70.0% 0.010 80);\n'
     '    --ink-faint:    oklch(54.0% 0.008 80);',
     '    --ink-muted:    oklch(70.0% 0.010 80);', 1),
]

#: ``check_*`` functions deliberately absent from :data:`GUARDS`, each with the
#: reason. Empty today, and that is the correct state — all seventeen are
#: rostered. It is declared anyway, because the entire lesson of the roster gaps
#: is that omission must cost something: the first author who wants a guard
#: outside the harness has to write down why, rather than discovering that
#: leaving it out is free and silent.
#:
#: The dict shape and its two self-checks (an entry must name a real function,
#: and must carry a non-empty reason) are shared with :data:`JS_OPT_OUT` via
#: :func:`_check_opt_out`, and were mutation-proven there — see
#: :func:`test_the_js_roster_is_every_shipped_module`.
GUARDS_OPT_OUT: dict[str, str] = {}

#: Guards are addressed by the file they read. The per-stylesheet entries and
#: the per-module entries keep `_block`'s uniqueness assertion scoped to one
#: file; "css/*" and "js/*" are the concatenations, and each exists for a single
#: genuinely cross-file invariant (`check_ink_faint_used_once` and
#: `check_single_innerhtml`). Adding another "*"-scoped guard is a smell —
#: prefer the narrowest file that can express the property.
_SOURCES = {
    "index.html": INDEX_HTML,
    "css/*": ALL_CSS,
    "js/*": JS_ALL,
    **{f"css/{name}": text for name, text in CSS.items()},
    **{f"js/{name}": text for name, text in JS.items()},
}


@pytest.mark.parametrize(
    ("name", "guard", "filename"),
    [(name, guard, filename) for name, guard, filename, _, _, _ in GUARDS],
)
def test_guard_passes_on_the_shipped_source(
    name: str, guard: object, filename: str
) -> None:
    guard(_SOURCES[filename])  # type: ignore[operator]


@pytest.mark.parametrize(
    ("name", "guard", "filename", "anchor", "replacement", "occurrences"), GUARDS
)
def test_every_guard_can_fail(
    name: str, guard: object, filename: str, anchor: str,
    replacement: str, occurrences: int,
) -> None:
    """Reintroduce each defect and require the guard to notice.

    THREE assertions, and the ORDER of the first two is the whole point. This
    test used to be step (c) alone, and step (c) alone is satisfied by a guard
    that raises on EVERYTHING — including one already broken for a reason that
    has nothing to do with the mutation.

    That is not a hypothetical either. ``check_every_stylesheet_is_linked_in_order``
    shipped with ``assert "@import" not in html`` while ``index.html``'s own
    header comment explains why the stylesheet is separate <link> tags "rather than
    one file with @import". The guard therefore raised on the *unmutated*
    source, ``pytest.raises`` was satisfied, and
    ``test_every_guard_can_fail[stylesheet-no-import]`` passed — certifying a
    check that could never pass and never distinguish anything. Three
    parametrizations of :func:`test_guard_passes_on_the_shipped_source` were red
    the whole time, in a different test, which is exactly how it survived.

    Step (a) closes that hole permanently: a broken guard now fails HERE, in the
    test whose job is to prove the guard works, rather than only in a sibling
    test someone could read as unrelated.

    Step (b) closes the second one. The audit sweep that found the ``@import``
    bug used ``str.replace(..., 1)`` against strings occurring TWICE in the old
    ``app.js``; the intended target survived, the guards stayed silent, and it
    briefly recorded two guards as blind that were not. Declaring the expected
    occurrence count — and replacing ALL of them, not the first — turns "my
    replacement hit the wrong thing" from a silent wrong answer into a failure
    at the line that made the mistake. That class of error has now appeared
    roughly seven times in this project, once inside the tooling built to detect
    it.
    """
    source = _SOURCES[filename]

    # (a) The guard must PASS on the real file. Without this, everything below
    #     is satisfied by a guard that raises unconditionally.
    try:
        guard(source)  # type: ignore[operator]
    except AssertionError as exc:
        pytest.fail(
            f"the {name!r} guard does not pass on the UNMUTATED {filename}, so "
            f"the mutation check below proves nothing — it would raise whether "
            f"or not the defect was reintroduced, and pytest.raises would be "
            f"satisfied either way. Fix the guard (or the source) before "
            f"trusting this entry. Underlying failure: {exc}"
        )

    # (b) The mutation must land, exactly as many times as declared.
    found = source.count(anchor)
    assert found == occurrences, (
        f"the {name!r} mutation anchor occurs {found} times in {filename}, but "
        f"the entry declares {occurrences}. Either the source moved and the "
        f"anchor is stale, or the anchor is not as unique as its author "
        f"believed — in which case replacing it changes code this guard was "
        f"never pointed at."
    )
    mutated = source.replace(anchor, replacement)
    assert mutated != source, (
        f"the {name!r} mutation did not change the text; anchor and replacement "
        f"are identical"
    )

    # (c) And only now: the guard must notice.
    with pytest.raises(AssertionError):
        guard(mutated)  # type: ignore[operator]



#: Assertions that no single mutation can reach, keyed by
#: ``"<guard>::<assertion source text>"``, mapped to WHY.
#:
#: Currently empty, and that is a claim, not an oversight: every assertion in
#: every guard is reachable by one of the 38 entries. The dict exists because
#: the first genuinely-unreachable assertion must be annotated rather than
#: silently tolerated — and because an exemption list with no guard on its own
#: contents is how "temporarily excluded" becomes permanent. `test_no_stale_
#: exemptions` below is that guard: an entry that names an assertion which no
#: longer exists, or one that IS now fired, fails.
UNFIREABLE: dict[str, str] = {}


def _assert_lines(function_name: str) -> dict[int, str]:
    """Every ``assert`` inside one guard, as {lineno: source text}."""
    tree = ast.parse(_GUARD_SOURCE)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return {
                x.lineno: _GUARD_LINES[x.lineno - 1].strip()
                for x in ast.walk(node)
                if isinstance(x, ast.Assert)
            }
    raise AssertionError(f"no guard function named {function_name!r}")


def _guard_span(function_name: str) -> tuple[int, int]:
    tree = ast.parse(_GUARD_SOURCE)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return node.lineno, node.end_lineno or node.lineno
    raise AssertionError(f"no guard function named {function_name!r}")


def _fired_line(guard: object, mutated: str, name: str) -> int:
    """The lineno of the assertion the mutation actually tripped.

    Attribution reads the INNERMOST frame, and requires it to be an ``assert``
    inside the guard's own body. Two near-misses this deliberately rejects, both
    of which a looser rule scores as coverage:

    * A raise from a HELPER — ``_block``'s two anchor assertions at :168/:173.
      The innermost frame is then in ``_block``, while the guard still appears
      in the traceback at its CALL site (e.g. :457). Filtering frames to the
      guard's line span and taking the last one picks that call site, which is
      not an assertion at all — so the guard's real assertions are silently
      recorded as unfired and clause (d) reports the wrong defect. Asking
      "is the innermost frame an assert in this guard?" separates them.
    * Any other AssertionError raised inside the guard that is not one of its
      own ``assert`` statements.

    A helper raise is a genuinely different defect and is named as such: the
    mutation broke the guard's ANCHOR rather than the property, ``_block``
    rejects it, ``pytest.raises(AssertionError)`` is satisfied, and the
    assertion the entry exists for never runs.
    """
    try:
        guard(mutated)  # type: ignore[operator]
    except AssertionError:
        frames = [
            frame for frame in traceback.extract_tb(sys.exc_info()[2])
            if frame.filename == str(_GUARD_PATH)
        ]
        guard_name = guard.__name__  # type: ignore[attr-defined]
        low, high = _guard_span(guard_name)
        innermost = frames[-1].lineno if frames else -1
        assert low <= innermost <= high and innermost in _assert_lines(guard_name), (
            f"the {name!r} mutation did not trip an assertion of "
            f"{guard_name} — it raised at line {innermost}, which is "
            f"{'a helper' if not low <= innermost <= high else 'not an assert'}. "
            f"The mutation broke the guard's ANCHOR rather than the property it "
            f"guards: `_block` rejects it, `pytest.raises(AssertionError)` in "
            f"clause (c) is satisfied either way, and the assertion this entry "
            f"exists for never runs. Re-point the anchor."
        )
        return innermost
    raise AssertionError(f"{name!r} did not raise; clause (c) should have caught this")


def test_every_assertion_is_fired_by_some_mutation() -> None:
    """CLAUSE (d) — the invariant clauses (a)-(c) structurally cannot express.

    (a)-(c) prove that SOME assertion in the guard noticed. They cannot prove it
    was the assertion the entry names, because an earlier assertion in the same
    guard shadows every later one.

    That is not hypothetical. ``inspector-head-is-first`` shipped declaring
    "at an unchanged count of five ... so only the ordering assertion can fire",
    while its mutation DELETED the header append instead of relocating it:
    ``appends`` went 5 -> 4, ``assert appends == 5`` fired first, and
    ``assert render.index(later) > head_at`` — the assertion the entry exists
    for — was never evaluated. Green on all three clauses, certifying nothing.

    Fourth appearance of this shape in this file, each one level of abstraction
    above the last: a guard that could not pass, a mutation that landed on the
    wrong text, an entry that proved the wrong assertion. Vigilance has caught
    none of them; an invariant has caught each.

    This asserts COVERAGE: every assertion in every guard is tripped by at least
    one mutation. It needs no declaration on GUARDS — the target is inferred
    from where the raise lands — so it costs no migration and cannot itself
    drift out of date with the entries.
    """
    fired: dict[str, set[int]] = {}
    for name, guard, filename, anchor, replacement, _occurrences in GUARDS:
        mutated = _SOURCES[filename].replace(anchor, replacement)
        fired.setdefault(guard.__name__, set()).add(  # type: ignore[attr-defined]
            _fired_line(guard, mutated, name)
        )

    blind: list[str] = []
    for guard_name in sorted({g.__name__ for _, g, *_ in GUARDS}):  # type: ignore[attr-defined]
        for lineno, text in sorted(_assert_lines(guard_name).items()):
            if lineno in fired.get(guard_name, set()):
                continue
            if f"{guard_name}::{text}" in UNFIREABLE:
                continue
            blind.append(f"  {guard_name}:{lineno}  {text}")

    assert not blind, (
        "these assertions are never tripped by ANY mutation, so they are "
        "asserted and never proven able to fail:\n" + "\n".join(blind) +
        "\n\nEither add an entry whose mutation reaches them — check that an "
        "EARLIER assertion in the same guard is not shadowing yours; that is "
        "how `inspector-head-is-first` proved the wrong thing — or record the "
        "assertion in UNFIREABLE with the reason it cannot be reached."
    )


def test_no_stale_exemptions() -> None:
    """Guard the exemption list, exactly as the stretch opt-in list is guarded.

    An UNFIREABLE entry naming an assertion that no longer exists, or one that
    IS now reachable, must fail. Otherwise the escape hatch silently outlives
    its reason and clause (d) decays into the thing it prevents.
    """
    for key in UNFIREABLE:
        guard_name, _, text = key.partition("::")
        texts = set(_assert_lines(guard_name).values())
        assert text in texts, (
            f"UNFIREABLE names {guard_name}::{text!r}, which is no longer an "
            f"assertion in that guard — the exemption outlived its subject"
        )
    # And an exemption for something now covered is equally stale. Recomputing
    # coverage here rather than sharing state with the test above keeps the two
    # independent: a bug in one cannot silence the other.
    for name, guard, filename, anchor, replacement, _ in GUARDS:
        mutated = _SOURCES[filename].replace(anchor, replacement)
        lineno = _fired_line(guard, mutated, name)
        gname = guard.__name__  # type: ignore[attr-defined]
        text = _assert_lines(gname).get(lineno)
        assert f"{gname}::{text}" not in UNFIREABLE, (
            f"{gname}::{text!r} is exempted as unreachable, but the {name!r} "
            f"mutation reaches it. Delete the exemption."
        )


#: Loops whose members are NOT independent claims, keyed ``"<guard>::<loop var>"``.
#:
#: Clause (e) below charges one required entry per loop member, because a
#: ``for`` over six selectors is six claims wearing one ``assert``. That is true
#: when the members can regress independently — ``.gutter`` can lose
#: ``--ink-muted`` while ``.meta-sub`` keeps it — and FALSE when they form a
#: chain, where violating a later member necessarily violates an earlier one and
#: the earlier assertion reports first.
#:
#: Annotated, never silent, and guarded by ``test_no_stale_loop_exemptions``:
#: the same shape as ``UNFIREABLE`` above and as ``check_stretch_is_opt_in``'s
#: exact opt-in list. An escape hatch with no guard on its own contents is how
#: "temporarily excluded" becomes permanent.
LOOP_CHAIN_EXEMPTIONS: dict[str, str] = {
    "check_resize_is_not_inert::later": (
        "The three targets are POSITIONS IN ONE SEQUENCE, not independent "
        "properties. In js/inspector.js the appends occur at withheld:137 < "
        "area:152 < body:163, and the loop asserts each index is greater than "
        "head_at. For the `area` member to fail while `withheld` passes you "
        "would need index(area) < head_at < index(withheld) — i.e. area "
        "appearing BEFORE withheld in the source, which is a reordering of the "
        "render branches, not the defect this guard names. Any relocation of "
        "the header append that breaks the ordering breaks the FIRST member, "
        "which is what `inspector-head-is-first` exercises. Contrast "
        "check_quiet_text_uses_muted, whose six selectors are six separate CSS "
        "rules that genuinely regress one at a time — those get six entries."
    ),
}


def _loop_extra_claims(function_name: str) -> dict[str, int]:
    """Extra independent claims per ``for`` loop, as {loop variable: extra}.

    A loop over a literal of N members containing A assertions makes ``N * A``
    claims where the source has ``A`` ``assert`` statements — so it contributes
    ``(N - 1) * A`` beyond what :func:`_assert_lines` can see. Recognises the two
    literal shapes this file actually uses: a tuple/list literal, and
    ``<name>.items()`` where ``<name>`` is a dict literal assigned in the same
    function.
    """
    tree = ast.parse(_GUARD_SOURCE)
    function = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == function_name),
        None,
    )
    assert function is not None, f"no guard function named {function_name!r}"

    def members(iterable: ast.expr) -> int | None:
        if isinstance(iterable, ast.Tuple | ast.List):
            return len(iterable.elts)
        if (isinstance(iterable, ast.Call)
                and isinstance(iterable.func, ast.Attribute)
                and iterable.func.attr == "items"
                and isinstance(iterable.func.value, ast.Name)):
            name = iterable.func.value.id
            for statement in ast.walk(function):
                if (isinstance(statement, ast.Assign)
                        and isinstance(statement.value, ast.Dict)
                        and any(isinstance(t, ast.Name) and t.id == name
                                for t in statement.targets)):
                    return len(statement.value.keys)
        return None

    extras: dict[str, int] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.For):
            continue
        count = members(node.iter)
        asserts = sum(1 for x in ast.walk(node) if isinstance(x, ast.Assert))
        if not count or not asserts:
            continue
        variable = node.target.id if isinstance(node.target, ast.Name) else "<tuple>"
        extras[variable] = extras.get(variable, 0) + (count - 1) * asserts
    return extras


def test_every_independent_claim_has_a_mutation() -> None:
    """CLAUSE (e) — clause (d) counts assert STATEMENTS, not claims.

    Measured hole, not a theoretical one: delete five of the six ``ink-faint-*``
    entries and clause (d) stays **GREEN**, because all six trip the same single
    ``assert`` inside ``for selector in (...)``. One statement, six independent
    CSS rules, coverage counted once. Five of the six could lose their mutation
    and nothing would say so.

    So a loop over N literal members carrying A assertions is charged ``N * A``
    required entries rather than ``A``. Members that form a CHAIN rather than a
    set are exempted explicitly in :data:`LOOP_CHAIN_EXEMPTIONS`, with the
    reason, because charging for claims that cannot be independently expressed
    would push the next author toward a contrived mutation just to satisfy an
    arithmetic — which is the failure mode this file exists to prevent, in a new
    costume.

    This is a floor, not an equality: extra entries per guard are fine and
    several exist deliberately.
    """
    entries: dict[str, int] = {}
    for _name, guard, *_rest in GUARDS:
        key = guard.__name__  # type: ignore[attr-defined]
        entries[key] = entries.get(key, 0) + 1

    short: list[str] = []
    for guard_name in sorted(entries):
        asserts = len(_assert_lines(guard_name))
        extras = _loop_extra_claims(guard_name)
        charged = sum(
            extra for variable, extra in extras.items()
            if f"{guard_name}::{variable}" not in LOOP_CHAIN_EXEMPTIONS
        )
        required = asserts + charged
        if entries[guard_name] < required:
            short.append(
                f"  {guard_name}: {entries[guard_name]} entries for {required} "
                f"claims ({asserts} assert statements + {charged} extra loop "
                f"members)"
            )

    assert not short, (
        "these guards make more independent claims than they have mutations, "
        "so some claim is asserted and never proven able to fail:\n"
        + "\n".join(short)
        + "\n\nA `for` over N literal members is N claims, not one. Add an entry "
        "per member, or — if the members form a chain where breaking a later "
        "one necessarily breaks an earlier one — record the loop in "
        "LOOP_CHAIN_EXEMPTIONS with that reasoning."
    )


def test_no_stale_loop_exemptions() -> None:
    """Guard clause (e)'s escape hatch, as ``test_no_stale_exemptions`` guards (d)'s."""
    guard_names = {g.__name__ for _, g, *_ in GUARDS}  # type: ignore[attr-defined]
    for key, reason in LOOP_CHAIN_EXEMPTIONS.items():
        guard_name, _, variable = key.partition("::")
        assert guard_name in guard_names, (
            f"LOOP_CHAIN_EXEMPTIONS names {guard_name!r}, which has no entries "
            f"in GUARDS — the exemption outlived its guard"
        )
        extras = _loop_extra_claims(guard_name)
        assert variable in extras, (
            f"LOOP_CHAIN_EXEMPTIONS names {key!r}, but {guard_name} has no loop "
            f"over variable {variable!r} contributing extra claims. Found: "
            f"{sorted(extras)}. The exemption outlived its subject."
        )
        assert reason.strip(), f"{key!r} is exempted with no reason recorded"


#: The four ``host.appendChild`` calls in ``js/inspector.js``, in the SOURCE
#: ORDER that ``LOOP_CHAIN_EXEMPTIONS`` reasons from. Named once, so the guard
#: below and the exemption's prose cannot drift apart.
_INSPECTOR_APPENDS = (
    "host.appendChild(head)",
    'host.appendChild(el("p", "withheld"',
    "host.appendChild(area)",
    "host.appendChild(body)",
)


def test_the_chain_exemptions_premise_still_holds() -> None:
    """Guard the SENTENCE the chain exemption is written in, not just its subject.

    ``test_no_stale_loop_exemptions`` checks that the exempted loop still
    exists. It does not check the PREMISE the exemption rests on — that in
    ``js/inspector.js`` the appends occur ``head < withheld < area < body``,
    which is what makes those loop members a monotonic chain rather than an
    independent set. Reorder the render branches and the members become
    independently reachable: the exemption then silently hides a real coverage
    gap while every other clause stays green. Subject intact, reason expired.

    **This is a PROXY, and the message says so.** Source order is not the chain
    property; it is the evidence the chain property was derived from, and the
    two can come apart in both directions:

    * wrap one branch in a new ``if`` and it becomes independently reachable
      while its line number does not move — order holds, monotonicity is gone,
      this guard stays green;
    * a pure reorder that preserves the early-return structure trips this guard
      without the property actually changing.

    Asserting real reachability needs control-flow analysis and is not worth it
    here. So this detects a **changed premise**, never a **broken property**, and
    the failure text asks for re-analysis rather than announcing a conclusion it
    cannot support. A guard that overstates its own scope is how the next reader
    stops re-deriving and starts trusting.

    UNIQUENESS FIRST, for the reason this file has learned three times
    (``.gutter``, ``.editor {``, the ``[data-theme="dark"]`` scope anchor): a
    second occurrence of any anchor makes ``str.index`` measure a different call
    and report an order that is not there — the guard protecting the exemption
    carrying the same defect class as the guards it protects.
    """
    if "check_resize_is_not_inert::later" not in LOOP_CHAIN_EXEMPTIONS:
        pytest.skip("the chain exemption was removed; its premise no longer matters")

    js = JS["inspector.js"]

    ambiguous = {a: js.count(a) for a in _INSPECTOR_APPENDS if js.count(a) != 1}
    assert not ambiguous, (
        f"these appends do not occur exactly once in js/inspector.js: "
        f"{ambiguous}. Position comparison below uses the FIRST match, so a "
        f"duplicate anchor would report an order that is not there — the same "
        f"non-unique-anchor defect `_block` exists to prevent, one level up."
    )

    positions = [js.index(anchor) for anchor in _INSPECTOR_APPENDS]
    order = [
        anchor for _, anchor in sorted(zip(positions, _INSPECTOR_APPENDS, strict=True))
    ]
    assert order == list(_INSPECTOR_APPENDS), (
        "the source order the chain exemption was derived from has CHANGED; "
        "re-analyse whether the loop members are still monotonically reachable.\n"
        f"  expected: {list(_INSPECTOR_APPENDS)}\n"
        f"  found:    {order}\n\n"
        "This guard checks source order, which is a PROXY for the chain "
        "property, not the property itself — so this is not a statement that "
        "the exemption is now wrong. It is a statement that the evidence it was "
        "written from no longer holds. Re-derive whether breaking a later member "
        "of check_resize_is_not_inert's ordering loop still necessarily breaks "
        "an earlier one. If it does not, the members are independent, the "
        "exemption must be DELETED, and clause (e) will then require one entry "
        "per member. Re-pointing this guard at the new order without redoing "
        "that analysis restores the amnesty the exemption was written to avoid."
    )


def test_the_ink_faint_guard_passes_when_the_token_is_only_in_a_comment() -> None:
    """The CSS analogue of the ``@import`` regression directly below.

    A stylesheet that DOCUMENTS the rule will contain the token the rule
    forbids — `reading.css` does, in a comment warning that adding
    ``var(--ink-faint)`` "reddens the suite", which duly reddened it. Both
    halves are asserted: a commented mention must PASS, and a real declaration
    must still FAIL. Without the second half, stripping comments could be
    "fixed" by deleting the assertion entirely.
    """
    commented = (
        "/* --ink-faint is not used here; prefer var(--ink-faint) alternatives. */\n"
        ".search-glyph { color: var(--ink-faint); }\n"
    )
    check_ink_faint_used_once(commented)

    with pytest.raises(AssertionError):
        check_ink_faint_used_once(
            commented + ".breadcrumbs li { color: var(--ink-faint); }\n"
        )


def test_the_no_import_guard_passes_when_the_word_is_only_in_a_comment() -> None:
    """The regression that made `check_every_stylesheet_is_linked_in_order` blind.

    index.html's header comment explains why the stylesheet is separate <link> tags
    "rather than one file with @import". A guard that greps the raw text can
    never pass against that prose — and it did not: three parametrizations of
    `test_guard_passes_on_the_shipped_source` failed on the shipped source while
    the matching mutation test passed vacuously, because the guard raised either
    way.

    So: a document whose ONLY `@import` is inside a comment must pass, and the
    same document with a real one must fail. Without the first half, stripping
    comments could be "fixed" by deleting the assertion entirely.
    """
    links = "\n".join(
        f'<link rel="stylesheet" href="/static/css/{name}">' for name in CSS_ORDER
    )
    commented = f"<!--\n  separate links rather than one file with @import\n-->\n{links}"
    check_every_stylesheet_is_linked_in_order(commented)

    with pytest.raises(AssertionError):
        check_every_stylesheet_is_linked_in_order(
            f'{commented}\n<style>@import url("/static/css/all.css");</style>'
        )


# ---------------------------------------------------------------------------
# The rosters, DISCOVERED rather than remembered.
#
# JS_ORDER and CSS_ORDER are hand-maintained tuples, and four modules escaped
# every guard in this file by simply not being in one: palette.js, marginalia.js
# and discovery.js in turn, then thread.js and thread.css together. Three of
# those were caught by hand; marginalia.css was carrying a real WCAG contrast
# defect the whole time it sat outside.
#
# WHY THE EXISTING GUARDS COULD NOT SEE IT, which is the part worth keeping.
# `check_every_stylesheet_is_linked_in_order` is genuinely strict — it asserts
# `links == list(CSS_ORDER)`, an exact sequence, no supersets. And it passes
# when BOTH sides omit the same file. Strictness about the RELATIONSHIP between
# two artefacts buys nothing when what drifts is the SCOPE of both: a guard
# comparing two lists to each other cannot see a third thing that is in neither.
# Only the filesystem knows what actually ships.
#
# The shape here is borrowed from tests/test_ci_workflow.py, which discovers
# browser modules by MARKER rather than by filename precisely so that its oracle
# cannot agree with the workflow's own glob and go blind alongside it.
# ---------------------------------------------------------------------------


def _shipped(root: Path, suffix: str) -> set[str]:
    """Every ``suffix`` file under ``root``, as a ``static/``-relative path."""
    return {
        str(path.relative_to(STATIC))
        for path in root.rglob(f"*{suffix}")
        if path.is_file()
    }


def _check_opt_out(opt_out: dict[str, str], shipped: set[str], label: str) -> None:
    """An opt-out must name something real and must say why.

    STALE ENTRIES ARE REJECTED, not ignored. An opt-out for a deleted file is
    the same failure mode as the roster gap itself, inverted: a line that looks
    like a decision and governs nothing. It would also silently pre-authorise a
    future file of that name to skip every guard.
    """
    for name, reason in sorted(opt_out.items()):
        assert name in shipped, (
            f"{label} opts out {name!r}, which does not exist. Delete the entry "
            "— a stale opt-out silently pre-authorises any future file with "
            "that name to skip every guard in this module."
        )
        assert reason.strip(), (
            f"{label}[{name!r}] has no reason. The whole point of a dict here is "
            "that an exclusion and an oversight must not look alike."
        )


def test_the_js_roster_is_every_shipped_module() -> None:
    """JS_ORDER + JS_OPT_OUT must account for every ``.js`` file that ships.

    THE WALK IS ALL OF ``static/``, NOT ``static/js/``, deliberately. Scoping it
    to ``js/`` would make the two root-level scripts — ``theme.js`` and
    ``tree_nav.js`` — invisible to this check by construction, which is the
    identical mistake one directory up: a filename oracle agreeing with the
    thing it is supposed to audit. Both are now EXCLUSIONS WITH REASONS in
    :data:`JS_OPT_OUT` rather than absences nobody decided on.

    WHAT MEMBERSHIP BUYS, stated precisely so this does not install a false
    belief where a known gap used to be. Being in ``JS_ORDER`` means a module is
    handed to the ``js/*`` guards — today: the single-innerHTML sink rule and
    the import/wiring checks. It does NOT mean the module is tested, reviewed,
    or exercised in a browser. This closes a VISIBILITY gap; it does not create
    coverage. A newly-visible module gains exactly the guards listed in
    :data:`GUARDS`, and nothing else.

    RED PHASE, and why this does not punish it (#40). The failure fires on a
    file that EXISTS on disk but is unrostered — never on a test that imports a
    module not yet written, which is the legitimate intermediate state of TDD
    and which this check cannot even see, because discovery walks the
    filesystem. Creating the module is the moment the roster line is owed, and
    the fix is one line, named in the message. An author mid-red-phase sees a
    precise instruction, not a mystery.
    """
    shipped = _shipped(STATIC, ".js")
    _check_opt_out(JS_OPT_OUT, shipped, "JS_OPT_OUT")

    rostered = {f"js/{name}" for name in JS_ORDER}
    accounted = rostered | set(JS_OPT_OUT)
    assert shipped == accounted, (
        f"static/ ships {sorted(shipped - accounted)} that no roster accounts "
        f"for, and claims {sorted(accounted - shipped)} that do not exist. Add "
        "each shipped module to JS_ORDER in the same change that creates it, or "
        "to JS_OPT_OUT with the reason it is excluded. A module in neither is "
        "invisible to every guard in this file while looking covered — that is "
        "how palette.js, marginalia.js, discovery.js and thread.js each shipped "
        "unchecked."
    )


def test_the_css_roster_is_every_shipped_stylesheet() -> None:
    """CSS_ORDER + CSS_OPT_OUT must account for every stylesheet that ships.

    MEMBERSHIP IS ASSERTED AS A SET, AND THE ORDER STAYS HAND-WRITTEN. That is
    not laziness: :data:`CSS_ORDER` is the CASCADE — four shared tiers then the
    per-feature sheets — and the cascade is a design decision that cannot be
    derived from a directory listing. Sorting it would silently reorder
    same-specificity pairs. So the filesystem answers "is anything missing",
    which is the question it can answer, and
    ``check_every_stylesheet_is_linked_in_order`` keeps answering "are the
    roster and index.html in the same order", which it already does well.
    Together they close the gap neither could close alone.

    WHAT MEMBERSHIP BUYS (#46). A rostered sheet is handed to the ``css/*``
    guards, which today means ONE contrast policy — ``--ink-faint`` must be used
    exactly once, on the decorative ``.search-glyph`` — plus the linked-in-order
    check. It does NOT mean the sheet has been checked for contrast generally,
    for focus states, or for anything else WCAG asks. ``marginalia.css``'s defect
    was caught because its author reached for that ONE token; a sheet failing AA
    with a different colour would pass every guard here. Do not read a green
    suite as an accessibility result.
    """
    shipped = _shipped(STATIC / "css", ".css")
    _check_opt_out(CSS_OPT_OUT, shipped, "CSS_OPT_OUT")

    rostered = {f"css/{name}" for name in CSS_ORDER}
    accounted = rostered | {f"css/{name}" for name in CSS_OPT_OUT}
    assert shipped == accounted, (
        f"static/css/ ships {sorted(shipped - accounted)} that CSS_ORDER does "
        f"not list, and CSS_ORDER claims {sorted(accounted - shipped)} that do "
        "not exist. A stylesheet outside the roster is styling the app while "
        "exempt from every rule the app has — marginalia.css carried a real "
        "WCAG AA failure for exactly as long as it sat there."
    )


def test_every_check_function_is_in_the_guards_table() -> None:
    """The THIRD roster — and the only one whose gap is permanently invisible.

    :data:`GUARDS` is hand-maintained exactly like ``JS_ORDER`` and
    ``CSS_ORDER``, and until this test it had no completeness oracle at all. A
    ``check_*`` added without a row is never run by
    :func:`test_guard_passes_on_the_shipped_source`, never mutated by
    :func:`test_every_guard_can_fail`, and never seen by
    :func:`test_every_assertion_is_fired_by_some_mutation`. It can be **entirely
    inert while a test literally named "every guard can fail" reports success.**

    WHY THIS ONE IS WORSE THAN THE OTHER TWO, which is the reason it is worth a
    separate test rather than a line in a checklist. An unrostered stylesheet is
    eventually noticed, because the page looks wrong; an unrostered module
    eventually throws. **An unrostered guard is indistinguishable from a working
    one, forever** — it is a function that reads plausibly, is referenced
    nowhere, and fails nothing. Three of the seventeen guards here are reachable
    ONLY through this table.

    It is the same shape as the ``links == list(CSS_ORDER)`` finding — total
    strictness about a RELATIONSHIP, no oracle at all on the SCOPE — turned on
    the harness itself.

    WHAT MEMBERSHIP BUYS (#46), and the distinction is sharper here than for the
    file rosters. A rostered guard is EXERCISED: proven to pass on the shipped
    source, and proven to fail against one declared defect. It is emphatically
    NOT proven CORRECT — it may assert the wrong property, or a weaker one than
    its message claims. ``check_resize_is_not_inert`` counts
    ``host.appendChild(`` in one function's source text while its message cites
    "the body must be the second child on every path", which is a broader claim
    than it can check. Discovery closes a visibility gap; it does not audit
    meaning.

    RED PHASE (#40): nothing to gate, and unlike the two filesystem checks that
    is true for a different reason rather than the same one. Those two cannot
    see a module that has not been written yet, so a test-first author never
    trips them. This one cannot either — but because a ``check_*`` either exists
    in the namespace or does not; there is no intermediate state in which it is
    half-defined. Whichever way an author works, the row is owed at the moment
    the function exists, and the message below names it.
    """
    defined = {
        name
        for name, obj in globals().items()
        if name.startswith("check_")
        and callable(obj)
        # Scoped to functions DEFINED HERE. Without this a `check_*` imported
        # into this namespace would read as locally defined and demand a row it
        # does not need — the guard would be reporting on someone else's file.
        and getattr(obj, "__module__", None) == __name__
    }
    _check_opt_out(GUARDS_OPT_OUT, defined, "GUARDS_OPT_OUT")

    # Anti-vacuity, and not a formality: this set is built by string-prefix
    # introspection, so a rename of the `check_` convention would empty it and
    # leave the assertion below comparing {} to {} — passing while checking
    # nothing, which is precisely the failure mode this test exists to end.
    assert defined, (
        "no check_* functions were discovered in this module, so this guard "
        "just checked nothing. Either every guard was deleted, or the naming "
        "convention changed and this discovery needs updating with it."
    )

    rostered = {guard.__name__ for _, guard, *_ in GUARDS}
    accounted = rostered | set(GUARDS_OPT_OUT)
    assert defined == accounted, (
        f"these guards are defined but appear in no GUARDS row: "
        f"{sorted(defined - accounted)}; and these rows name something this "
        f"module does not define: {sorted(accounted - defined)}. A guard "
        "outside GUARDS is never run on the shipped source, never mutated, and "
        "never audited — it is inert while `test_every_guard_can_fail` reports "
        "success. Add a row with a defect that must make it fail, or add it to "
        "GUARDS_OPT_OUT with the reason it is exempt."
    )
