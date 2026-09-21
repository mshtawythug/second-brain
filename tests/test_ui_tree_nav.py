"""D-A11: the vault tree's keyboard navigation, executed rather than inspected.

``brain ui`` had a **keyboard trap** in the rail (WCAG 2.1.1): the renderer set
``tabIndex = -1`` on every label and anchor and never set one back, the
container carried a static ``tabindex="0"``, and no arrow key was handled
anywhere — so a keyboard user could reach the vault and then could not open a
single note from it.

The decision half of the fix lives in ``static/tree_nav.js`` precisely so it can
be *run* here instead of grepped. The module is pure: it maps (key, focused
index, visible items) to an action, and knows nothing about the DOM. js/tree.js owns
the other half — building the item list, applying the action, moving focus —
which ``tests/test_ui_static_behaviour.py`` guards structurally.

Node is used the same way ``tests/test_buildid_etag.py`` uses it: the source is
copied into a tmp ``.mjs`` beside a harness, so no repo ``package.json``
``type`` setting can change how the module is parsed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from brain.ui.app import static_dir

#: Opens NO database connection — this module copies a JS file to a tmp dir and
#: runs it under node. The marker lets the session skip the schema reset and,
#: more importantly, the MACHINE-WIDE advisory lock; see
#: ``conftest._session_touches_the_database``.
pytestmark = pytest.mark.nodb

TREE_NAV = Path(str(static_dir())) / "tree_nav.js"

#: A vault shaped like a real one: a CLOSED folder, an OPEN folder with two
#: notes inside it, and a note at the root. Only visible items are listed —
#: the closed folder contributes no children, which is the invariant that makes
#: "the next visible item" a plain index step.
HARNESS = """\
import {
  rovingIndex, parentIndex, treeKeyAction, flattenVisible, restoreIndex,
} from "./tree_nav.mjs";

const failures = [];
function eq(actual, expected, what) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) failures.push(`${what}: expected ${b}, got ${a}`);
}

const items = [
  { id: "folder:archive",  level: 1, expandable: true,  expanded: false },
  { id: "folder:projects", level: 1, expandable: true,  expanded: true  },
  { id: "note-a",          level: 2, expandable: false, expanded: false },
  { id: "note-b",          level: 2, expandable: false, expanded: false },
  { id: "note-c",          level: 1, expandable: false, expanded: false },
];
const LAST = 4;

/* --- roving tabindex: exactly one tabbable item, never zero ------------- */
eq(rovingIndex(items, null), 0, "nothing selected -> first item is tabbable");
eq(rovingIndex(items, "note-b"), 3, "the selected note is the tabbable one");
eq(rovingIndex(items, "hidden-in-a-closed-folder"), 0,
   "a selection that is not visible falls back to the first item");
eq(rovingIndex([], "note-a"), -1, "an empty tree has nothing to focus");

/* --- vertical movement -------------------------------------------------- */
eq(treeKeyAction("ArrowDown", 0, items), { type: "move", index: 1 }, "ArrowDown");
eq(treeKeyAction("ArrowDown", LAST, items), { type: "move", index: LAST },
   "ArrowDown at the end stays put");
eq(treeKeyAction("ArrowUp", 3, items), { type: "move", index: 2 }, "ArrowUp");
eq(treeKeyAction("ArrowUp", 0, items), { type: "move", index: 0 },
   "ArrowUp at the top stays put");
eq(treeKeyAction("Home", 3, items), { type: "move", index: 0 }, "Home");
eq(treeKeyAction("End", 0, items), { type: "move", index: LAST }, "End");

/* ArrowDown crosses a level boundary: from the open folder into its child,
   and from its last child back out to the root-level note. */
eq(treeKeyAction("ArrowDown", 1, items), { type: "move", index: 2 },
   "ArrowDown descends into an open folder");
eq(treeKeyAction("ArrowDown", 3, items), { type: "move", index: 4 },
   "ArrowDown leaves a folder at its last child");

/* --- horizontal movement ------------------------------------------------ */
eq(treeKeyAction("ArrowRight", 0, items), { type: "expand", index: 0 },
   "ArrowRight opens a closed folder");
eq(treeKeyAction("ArrowRight", 1, items), { type: "move", index: 2 },
   "ArrowRight on an open folder steps into it");
eq(treeKeyAction("ArrowRight", 2, items), null, "ArrowRight on a note does nothing");
eq(treeKeyAction("ArrowLeft", 1, items), { type: "collapse", index: 1 },
   "ArrowLeft closes an open folder");
eq(treeKeyAction("ArrowLeft", 2, items), { type: "move", index: 1 },
   "ArrowLeft on a child moves to its parent");
eq(treeKeyAction("ArrowLeft", 0, items), null,
   "ArrowLeft on a closed top-level folder has nowhere to go");
eq(treeKeyAction("ArrowLeft", 4, items), null,
   "ArrowLeft on a top-level note has nowhere to go");
eq(parentIndex(items, 3), 1, "parentIndex finds the enclosing folder");
eq(parentIndex(items, 0), -1, "a top-level item has no parent");

/* An open folder with nothing after it must not step off the end. */
const trailing = [{ id: "f", level: 1, expandable: true, expanded: true }];
eq(treeKeyAction("ArrowRight", 0, trailing), null,
   "ArrowRight cannot step past the last item");

/* --- activation --------------------------------------------------------- */
eq(treeKeyAction("Enter", 3, items), { type: "activate", index: 3 }, "Enter activates");
eq(treeKeyAction("Enter", 0, items), { type: "activate", index: 0 },
   "Enter on a folder activates it too");

/* --- everything else stays unclaimed ------------------------------------ */
/* These are load-bearing: "/" focuses the search box and "k" rides Cmd+K, and
   both are handled by the document-level listener. A tree action for either
   would swallow the shortcut. */
for (const key of ["/", "k", "Escape", "Tab", " ", "a", "PageDown"]) {
  eq(treeKeyAction(key, 0, items), null, `"${key}" is not the tree's key`);
}
eq(treeKeyAction("ArrowDown", 0, []), null, "an empty tree yields no action");
eq(treeKeyAction("ArrowDown", -1, items), null, "no focused item yields no action");
eq(treeKeyAction("ArrowDown", 99, items), null, "an out-of-range index is inert");

/* --- flattenVisible: the invariant, as a return value ------------------- */
/* The same shape as `items` above, but as the API actually delivers it. A
   collapsed folder must contribute ITSELF and nothing below it. */
const TREE = {
  children: [
    { path: "archive", name: "archive", children: [], notes: [{ id: "buried", title: "Buried" }] },
    { path: "projects", name: "projects",
      children: [{ path: "projects/q3", name: "q3", children: [],
                  notes: [{ id: "deep", title: "Deep" }] }],
      notes: [{ id: "note-a", title: "A" }, { id: "note-b", title: "B", draft: true }] },
  ],
  notes: [{ id: "note-c", title: "C" }],
};

let flat = flattenVisible(TREE, new Set());
eq(flat.map((i) => i.id), ["folder:archive", "folder:projects", "note-c"],
   "collapsed folders contribute NOTHING below them");
eq(flat.map((i) => i.level), [1, 1, 1], "top level is level 1");

flat = flattenVisible(TREE, new Set(["projects"]));
eq(flat.map((i) => i.id),
   ["folder:archive", "folder:projects", "folder:projects/q3", "note-a", "note-b", "note-c"],
   "expanding a folder inserts its children immediately after it");
eq(flat.map((i) => i.level), [1, 1, 2, 2, 2, 1], "children are one level deeper");
eq(flat.find((i) => i.id === "folder:projects/q3").expanded, false,
   "a nested folder that is not in the expanded set is closed");
eq(flat.find((i) => i.id === "note-b").draft, true, "draft flag survives");

flat = flattenVisible(TREE, new Set(["projects", "projects/q3"]));
eq(flat.map((i) => i.id),
   ["folder:archive", "folder:projects", "folder:projects/q3", "deep",
    "note-a", "note-b", "note-c"],
   "a nested expansion descends two levels");
eq(flat.find((i) => i.id === "deep").level, 3, "grandchild is level 3");

/* An expanded ARCHIVE reveals its note; the projects subtree stays closed. */
flat = flattenVisible(TREE, new Set(["archive"]));
eq(flat.map((i) => i.id), ["folder:archive", "buried", "folder:projects", "note-c"],
   "expansion is per-folder, not global");

eq(flattenVisible(null, new Set()), [], "a null tree flattens to nothing");
eq(flattenVisible({}, new Set()), [], "a tree with no children or notes is empty");

/* --- restoreIndex: focus after a re-render destroyed every node --------- */
eq(restoreIndex(items, "note-b", 3), 3, "a surviving item is found again");
eq(restoreIndex(items, "note-b", 0), 3,
   "identity wins over the remembered position");
eq(restoreIndex(items, "deleted-note", 3), 3,
   "a vanished item falls back to its old position");
eq(restoreIndex(items.slice(0, 2), "deleted-note", 4), 1,
   "the fallback clamps to the shortened list rather than running off the end");
eq(restoreIndex([], "note-a", 2), -1, "an emptied tree restores nothing");
eq(restoreIndex(items, null, 2), -1, "nothing was focused -> restore nothing");
eq(restoreIndex(items, undefined, 2), -1, "undefined is treated as nothing");
eq(restoreIndex(items, "gone", -1), -1,
   "no remembered position and no match -> restore nothing");

/* --- modifier keys belong to the browser -------------------------------- */
for (const mod of ["meta", "ctrl", "alt"]) {
  eq(treeKeyAction("ArrowDown", 0, items, { [mod]: true }), null,
     `${mod}+ArrowDown is the browser's, not the tree's`);
}
eq(treeKeyAction("ArrowDown", 0, items, { shift: true }),
   { type: "move", index: 1 },
   "Shift is NOT a modifier here: single-select tree, plain movement is right");
eq(treeKeyAction("ArrowDown", 0, items, {}), { type: "move", index: 1 },
   "an empty modifier object behaves like none");
eq(treeKeyAction("ArrowDown", 0, items, undefined), { type: "move", index: 1 },
   "omitting modifiers entirely still works");

if (failures.length) {
  console.error(failures.join("\\n"));
  process.exit(1);
}
console.log("ok");
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover — environment-dependent
        pytest.skip("node is required for the tree_nav.js runtime harness")
    return node


def _run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "tree_nav.mjs").write_text(source, encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    return subprocess.run(
        [_node(), str(harness)], capture_output=True, text=True, check=False
    )


def test_tree_keyboard_navigation_behaves(tmp_path: Path) -> None:
    """Run every key against the real module."""
    result = _run(tmp_path, TREE_NAV.read_text(encoding="utf-8"))
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize(
    ("anchor", "replacement", "what"),
    [
        (
            'return { type: "move", index: clamp(index + 1, last) };',
            "return null;",
            "ArrowDown",
        ),
        (
            'if (!item.expanded) return { type: "expand", index };',
            "if (!item.expanded) return null;",
            "ArrowRight on a closed folder",
        ),
        (
            'case "Enter":\n      return { type: "activate", index };',
            'case "Enter":\n      return null;',
            "Enter",
        ),
        (
            "if (items[i].level < level) return i;",
            "if (items[i].level < level) return -1;",
            "parentIndex",
        ),
        (
            # Anchored on the SELECTED-id lookup specifically. A bare
            # `if (found !== -1) return found;` now appears in restoreIndex too,
            # and `.replace(..., 1)` would hit that one first — the mutation
            # would still go red, but it would be testing a different function
            # than its label claims.
            "const found = items.findIndex((item) => item.id === selectedId);",
            "const found = -1;",
            "rovingIndex honouring the selection",
        ),
        (
            "if (open) items.push(...flattenVisible(child, expanded, level + 1));",
            "items.push(...flattenVisible(child, expanded, level + 1));",
            "flattenVisible skipping collapsed subtrees "
            "(the exact regression the extraction exists to prevent)",
        ),
        (
            # Deletes the WHOLE statement including its `if (open)` guard.
            # Anchoring on the inner call alone left `if (open)` dangling before
            # a closing brace — a syntax error, so node exited non-zero and the
            # row "passed" while proving only that the module stopped parsing.
            "if (open) items.push(...flattenVisible(child, expanded, level + 1));",
            "",
            "flattenVisible descending at all",
        ),
        (
            "if (previousIndex < 0) return -1;\n"
            "  return Math.min(previousIndex, items.length - 1);",
            "return -1;",
            "restoreIndex falling back to the nearest surviving position",
        ),
        (
            "if (modifiers && (modifiers.meta || modifiers.ctrl || modifiers.alt)) return null;",
            "",
            "modifier keys being left to the browser",
        ),
    ],
)
def test_the_harness_can_actually_fail(
    tmp_path: Path, anchor: str, replacement: str, what: str
) -> None:
    """Guard the guard: break each behaviour and prove the harness goes red.

    This repository has shipped ~12 guards that could not fail. A mutation whose
    anchor silently missed produces exactly that, so the substitution is
    asserted to have applied before the harness is run at all.
    """
    source = TREE_NAV.read_text(encoding="utf-8")
    assert anchor in source, f"the mutation anchor for {what} no longer exists"
    mutated = source.replace(anchor, replacement, 1)
    assert mutated != source, f"the {what} mutation did not apply"

    result = _run(tmp_path, mutated)
    assert result.returncode != 0, (
        f"breaking {what} left the harness GREEN — it is not testing that behaviour"
    )
