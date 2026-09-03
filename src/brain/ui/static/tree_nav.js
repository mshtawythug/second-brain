/* Keyboard navigation for the vault tree — the decision half, kept pure.
 *
 * The WAI-ARIA tree pattern is a roving tabindex plus arrow keys: the tree is
 * ONE tab stop, and once inside it the arrows move focus. Before this module
 * existed the front end set `tabIndex = -1` on every label and anchor and never set it
 * back, and there was no arrow-key handling at all — so a keyboard user could
 * reach the rail and then could not open a single note from it (WCAG 2.1.1).
 *
 * Everything here operates on a flat array of plain descriptors:
 *
 *   { id, level, expandable, expanded }
 *
 * — one entry per *visible* item, in document order. A collapsed folder's
 * children are simply absent from that array, which is what makes "move to the
 * next visible item" a plain index step and keeps this file free of the DOM.
 *
 * `flattenVisible` PRODUCES that array, and that matters more than it looks.
 * The list used to be assembled as a side effect of the renderer's recursive DOM
 * build, where "collapsed folders contribute no children" held only because one
 * `push` statement happened to sit above an `if (open)`. A property held by
 * statement position is unwritten and unenforceable: hoist that line and
 * navigation walks into collapsed folders with every test still green. Here the
 * same property is the return value of a function, so the regression cannot be
 * expressed — there is no push to hoist.
 *
 * js/tree.js owns the rest: it builds the DOM, attaches a focusable node to each
 * descriptor, applies the returned action, and moves real focus.
 */

/* Every VISIBLE item of `node`, in document order, as plain descriptors.
 *
 * `expanded` is the Set of open folder paths. A folder that is not in it
 * contributes itself and NOTHING BELOW IT — the recursion simply does not
 * descend — which is the invariant the whole navigation layer rests on.
 *
 * Folders precede notes at each level, matching the render order. Ids are
 * namespaced (`folder:<path>`) so a folder can never collide with a note id.
 */
export function flattenVisible(node, expanded, level = 1) {
  const items = [];
  if (!node) return items;

  for (const child of node.children || []) {
    const open = expanded.has(child.path);
    items.push({
      id: `folder:${child.path}`,
      path: child.path,
      name: child.name,
      level,
      expandable: true,
      expanded: open,
    });
    if (open) items.push(...flattenVisible(child, expanded, level + 1));
  }

  for (const note of node.notes || []) {
    items.push({
      id: note.id,
      path: null,
      name: note.title,
      draft: Boolean(note.draft),
      level,
      expandable: false,
      expanded: false,
    });
  }
  return items;
}

/* Where focus should land after a re-render that destroyed every node.
 *
 * `renderTree` is a dispatch subscriber, so ANY state change anywhere rebuilds
 * the rail — a search landing, a draft toggle, a note payload arriving. If the
 * previously focused item survived, return it. If it did not (deleted, renamed,
 * or its folder collapsed out from under it) fall back to the nearest surviving
 * position rather than dropping focus to <body>. -1 means "focus nothing",
 * which the caller must treat as "leave focus where it is".
 */
export function restoreIndex(items, focusedId, previousIndex) {
  if (!items.length || focusedId === null || focusedId === undefined) return -1;
  const found = items.findIndex((item) => item.id === focusedId);
  if (found !== -1) return found;
  if (previousIndex < 0) return -1;
  return Math.min(previousIndex, items.length - 1);
}

/* Index of the item that carries tabindex="0". The selected note when it is
 * visible, otherwise the first item — never nothing, or the tree drops out of
 * the tab order entirely and the trap comes straight back. Returns -1 only for
 * an empty tree, which has nothing to focus. */
export function rovingIndex(items, selectedId) {
  if (!items.length) return -1;
  if (selectedId) {
    const found = items.findIndex((item) => item.id === selectedId);
    if (found !== -1) return found;
  }
  return 0;
}

/* The parent of items[index]: the nearest earlier item at a shallower level.
 * -1 at the top level. */
export function parentIndex(items, index) {
  const level = items[index].level;
  for (let i = index - 1; i >= 0; i -= 1) {
    if (items[i].level < level) return i;
  }
  return -1;
}

const clamp = (value, last) => Math.max(0, Math.min(value, last));

/* Decide what a key press does. Returns one of
 *
 *   { type: "move",     index }   focus another item
 *   { type: "expand",   index }   open a collapsed folder (focus stays)
 *   { type: "collapse", index }   close an open folder (focus stays)
 *   { type: "activate", index }   open the note / toggle the folder
 *
 * or null for "not ours" — which is the important case: every key this returns
 * null for must keep working exactly as it did, including "/" and Cmd+K.
 *
 * `modifiers` is `{meta, ctrl, alt}`. A modified arrow belongs to the browser
 * or the OS — Cmd+Down is "scroll to bottom", Alt+Left is "back" — so any of
 * the three yields null and the key is left alone. Shift is deliberately NOT
 * included: this is a single-select tree, so Shift+Arrow has no extend-selection
 * meaning to protect and plain movement is the right behaviour.
 */
export function treeKeyAction(key, index, items, modifiers) {
  if (!items.length || index < 0 || index >= items.length) return null;
  if (modifiers && (modifiers.meta || modifiers.ctrl || modifiers.alt)) return null;
  const last = items.length - 1;
  const item = items[index];

  switch (key) {
    case "ArrowDown":
      return { type: "move", index: clamp(index + 1, last) };
    case "ArrowUp":
      return { type: "move", index: clamp(index - 1, last) };
    case "Home":
      return { type: "move", index: 0 };
    case "End":
      return { type: "move", index: last };
    case "ArrowRight":
      if (!item.expandable) return null;
      /* Closed: open it, focus stays put — the user sees what appeared before
         moving into it. Open: step into it. Its first child is the next visible
         item by construction. */
      if (!item.expanded) return { type: "expand", index };
      return index < last ? { type: "move", index: index + 1 } : null;
    case "ArrowLeft": {
      if (item.expandable && item.expanded) return { type: "collapse", index };
      const parent = parentIndex(items, index);
      return parent === -1 ? null : { type: "move", index: parent };
    }
    case "Enter":
      return { type: "activate", index };
    default:
      return null;
  }
}
