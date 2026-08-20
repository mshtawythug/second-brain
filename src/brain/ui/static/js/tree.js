/* The vault rail: DOM build, roving tabindex, and arrow-key navigation.
 *
 * The DECISIONS live in /static/tree_nav.js — a pure, DOM-free module this file
 * imports and never duplicates. See the note in the design spec (§2) for why
 * that file stayed outside js/ instead of being folded in here.
 *
 * NOTE THE IMPORT CYCLE: this module imports `openNote` from inspector.js, and
 * inspector.js imports `loadTree` from this one. That is a real cycle and it is
 * deliberate — activating a tree item opens a note, and mutating a note
 * (draft toggle, move, delete) reloads the tree. ES modules resolve it because
 * both bindings are function DECLARATIONS, which are hoisted, and neither
 * module CALLS the other at evaluation time. Add a top-level call to an
 * imported binding in either file and it becomes a TDZ error at boot — which
 * the browser harness catches immediately, since a page that does not boot has
 * no treeitem to wait for.
 */

import {
  flattenVisible, restoreIndex, rovingIndex, treeKeyAction,
} from "/static/tree_nav.js";
import { api } from "/static/js/api.js";
import { $, el, toast } from "/static/js/dom.js";
import { dispatch, saveExpanded, state } from "/static/js/store.js";
import { openNote } from "/static/js/inspector.js";

/* The visible items, in document order. The DESCRIPTORS come from
   tree_nav.flattenVisible — a pure function — and this file only attaches the
   focusable DOM node and the activation closure to each. Assembling the list
   here as a side effect of the recursive build is what previously made
   "collapsed folders contribute no children" depend on one statement's
   position. */
let treeItems = [];

/* Serial for the `aria-owns` IDREFs minted below. Reset every render so ids do
   not grow without bound across a long session. */
let treeGroupSeq = 0;

/* ------------------------------------------------ the "show ingested" toggle --
 *
 * DEFAULT OFF, matching the Quartz overlay (P4.2). The vault's own notes are
 * what the reader authored; the `_ingested/` mirror is machine-written and
 * dwarfs it, so a rail that opens showing everything buries the half that was
 * written by hand.
 *
 * THE KEY IS brain-ui NAMESPACED, deliberately diverging from the overlay's
 * `brain.explorer.showIngested`. That is a different application on a different
 * origin, and store.js already established `brain-ui-<thing>` for this app's
 * localStorage. Sharing a key across two apps that render different trees would
 * couple their UI state for no benefit. The DEFAULT is what the plan says to
 * take from the overlay, and it is taken.
 *
 * Lives here rather than in store.js's `state` because store.js is
 * integrator-owned this phase, and because this is rail-local view state that no
 * other module reads. */
const SHOW_INGESTED_KEY = "brain-ui-show-ingested";

let showIngested = loadShowIngested();

export function loadShowIngested() {
  try {
    return JSON.parse(localStorage.getItem(SHOW_INGESTED_KEY) || "false") === true;
  } catch (e) {
    /* Private mode, or a value some other version wrote. Falling back to the
       default is right: this is a view preference, not data. */
    return false;
  }
}

function saveShowIngested(value) {
  try {
    localStorage.setItem(SHOW_INGESTED_KEY, JSON.stringify(value));
  } catch (e) { /* private mode — a rail preference is not worth failing over */ }
}

/* Folders whose notes are grouped by month, per the overlay (P4.3). NOT every
   folder: month headers make sense where the folder is a chronological feed —
   a meeting transcript per call, an email thread per day — and are noise in a
   hand-organised vault folder where the author chose the arrangement. */
const MONTH_GROUPED_PATHS = new Set(["_ingested/krisp", "_ingested/gmail"]);

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/* `YYYY-MM` -> `Mon YYYY`, and the day for a leaf's label.
 *
 * Parsed off the ISO STRING rather than through `new Date()`. `TreeNote.date`
 * carries a date the server already resolved; handing it to the Date
 * constructor would re-interpret a bare `YYYY-MM-DD` as UTC midnight and then
 * render it in the viewer's local zone, so a note dated the 1st displays as the
 * previous month for anyone west of Greenwich. Slicing the string cannot do
 * that. */
function monthKeyOf(note) {
  const date = typeof note.date === "string" ? note.date : "";
  return /^\d{4}-\d{2}/.test(date) ? date.slice(0, 7) : "";
}

function monthLabel(key) {
  const [year, month] = key.split("-");
  return `${MONTHS[Number(month) - 1]} ${year}`;
}

function dayLabel(note) {
  const date = typeof note.date === "string" ? note.date : "";
  if (!/^\d{4}-\d{2}-\d{2}/.test(date)) return "";
  return `${MONTHS[Number(date.slice(5, 7)) - 1]} ${Number(date.slice(8, 10))}`;
}

/* ------------------------------------------------------- the view transform --
 *
 * ONE transform, consumed by BOTH the DOM build and flattenVisible.
 *
 * That is the whole defence against the defect this feature is known for. The
 * overlay's comment records it: filter the rendered tree while the counts are
 * computed from something else, and the badges keep reporting the hidden notes.
 * The same split would break arrow-key navigation — flattenVisible would hand
 * back descriptors for items the DOM never drew, so ArrowDown would step onto
 * nothing. Both failures have the same cause: two readers of two different
 * trees. Here there is one tree, and agreement is not maintained, it is
 * structural.
 */
function prepareTree(tree) {
  if (!tree) return tree;
  const filtered = showIngested ? tree : pruneIngested(tree);
  return orderMonthGrouped(filtered);
}

/* Drop every ingested leaf, and every folder left holding none.
 *
 * FILTERS BY TIER, not by the `_ingested/` folder name the overlay matches on.
 * The server marks each leaf's tier and derives `vault_count` from exactly that
 * predicate, so filtering by tier makes the rendered tree and the recomputed
 * badge two views of ONE decision. Matching the folder name instead would hide
 * `_ingested/` while leaving any ingested note stored elsewhere visible — and
 * its ancestors' `vault_count` would already have excluded it, so the tree and
 * the badges would disagree by construction rather than by accident.
 *
 * Returns a NEW tree; the payload in `state.tree` is never mutated, so toggling
 * back on restores the full tree without a refetch. */
function pruneIngested(node) {
  const children = (node.children || [])
    .filter((child) => child.vault_count > 0)
    .map(pruneIngested);
  const notes = (node.notes || []).filter((note) => note.tier !== "ingested");
  return {
    ...node,
    children,
    notes,
    /* THE COUNT IS REWRITTEN HERE, and this line is the feature. The renderer
       always draws `note_count`; with the ingested leaves gone, the correct
       total for this node is the one the server already computed for exactly
       this filter. Leaving the original `note_count` in place is the declared
       mutation for this task — the tree shrinks and the badges do not. */
    note_count: node.vault_count,
    ingested_count: 0,
  };
}

/* Newest month first, and newest day first inside each month.
 *
 * Applied as a DATA transform rather than at render time so flattenVisible sees
 * the same order the reader does — otherwise ArrowDown would walk the rail in
 * the server's title order while the eye follows the date order.
 *
 * Undated notes sort last, together, keeping their existing (title) order.
 * A feed folder should not open on a note with no date where the newest one
 * belongs, and dropping them entirely would hide documents. */
function orderMonthGrouped(node) {
  const children = (node.children || []).map(orderMonthGrouped);
  if (!MONTH_GROUPED_PATHS.has(node.path)) return { ...node, children };
  const notes = [...(node.notes || [])].sort((a, b) => {
    const da = typeof a.date === "string" ? a.date : "";
    const db = typeof b.date === "string" ? b.date : "";
    if (!da && !db) return 0;
    if (!da) return 1;
    if (!db) return -1;
    return db.localeCompare(da);          /* ISO strings sort lexically */
  });
  return { ...node, children, notes };
}

export function renderTree() {
  const host = $("tree");

  /* Capture focus BEFORE the wipe. renderTree is a dispatch subscriber, so it
     runs on EVERY state change from anywhere — the note payload landing ~100ms
     after Enter, a search result, a draft toggle, the boot dispatch. Each one
     destroys every node in the rail. Without this, focus silently fell to
     <body> and the next Tab restarted at the top of the page. */
  const hadFocus = host.contains(document.activeElement);
  const previousIndex = hadFocus
    ? treeItems.findIndex((item) => item.node === document.activeElement)
    : -1;
  const previousId = previousIndex === -1 ? null : treeItems[previousIndex].id;

  host.textContent = "";
  treeItems = [];
  treeGroupSeq = 0;
  if (!state.tree) return;
  if (state.tree.count === 0) {
    host.appendChild(el("p", "empty", state.tree.empty_hint || "Nothing here yet."));
    return;
  }

  /* ONE prepared tree, handed to BOTH readers. See prepareTree. */
  const view = prepareTree(state.tree);

  const nodes = new Map();
  host.appendChild(buildBranch(view, 1, nodes));
  /* Descriptors from the pure module; nodes from the build above. */
  treeItems = flattenVisible(view, state.expanded).map((descriptor) => ({
    ...descriptor,
    ...nodes.get(descriptor.id),
  }));
  applyRovingTabindex();

  /* Restore ONLY if the rail held focus. Restoring unconditionally would steal
     it from the search box or the editor on every dispatch — a worse bug than
     the one this fixes, and one that would read as the app fighting the user. */
  if (previousId !== null) {
    const target = restoreIndex(treeItems, previousId, previousIndex);
    if (target !== -1) focusTreeItem(target);
  }
}

/* Exactly ONE item is tabbable; the rest are reachable only by arrow key. That
   is the whole WAI-ARIA tree pattern, and it is what makes the rail a single
   tab stop instead of a hundred — or, as it was before, none at all. */
function applyRovingTabindex(index) {
  const target = index === undefined ? rovingIndex(treeItems, state.selectedId) : index;
  treeItems.forEach((item, i) => { item.node.tabIndex = i === target ? 0 : -1; });
  return target;
}

function focusTreeItem(index) {
  const target = applyRovingTabindex(index);
  if (target >= 0) treeItems[target].node.focus();
}

function setFolderOpen(path, open) {
  if (open) state.expanded.add(path);
  else state.expanded.delete(path);
  saveExpanded();
  renderTree();
}

export function onTreeKeydown(event) {
  const index = treeItems.findIndex((item) => item.node === event.target);
  if (index === -1) return;
  const action = treeKeyAction(event.key, index, treeItems, {
    meta: event.metaKey, ctrl: event.ctrlKey, alt: event.altKey,
  });
  if (!action) return;                /* every other key keeps its old meaning */
  event.preventDefault();
  const item = treeItems[action.index];
  if (action.type === "move") { focusTreeItem(action.index); return; }
  if (action.type === "expand" || action.type === "collapse") {
    setFolderOpen(item.path, action.type === "expand");
    return;
  }
  /* No refocus needed here: renderTree restores focus itself, for every path
     that rebuilds the rail rather than only the ones enumerated at a call
     site. That is the whole point of moving the fix into renderTree. */
  item.activate();
}

/* Builds the DOM and records each item's focusable node in `nodes`, keyed by the
   same id flattenVisible produces. It no longer decides WHICH items exist or in
   what order — that is the pure module's job now.

   The WAI-ARIA "Navigation Treeview" shape: the <li> is role="none" and the
   TREEITEM is the focusable element inside it. Previously role="treeitem" and
   the aria-* state sat on the <li> while the roving tabindex and DOM focus went
   to the child — so the element the user focused had no role, and aria-selected
   was announced to nobody. Keeping the <a> as the treeitem also preserves the
   href, which is what makes Cmd-click and middle-click open a note in a new
   tab; focusing the <li> instead would split the interactive element from the
   focusable one all over again. */
function buildBranch(node, level, nodes) {
  const group = el("ul", "tree-group");
  group.setAttribute("role", "group");

  for (const child of node.children) {
    const item = el("li", "tree-folder");
    item.setAttribute("role", "none");
    const open = state.expanded.has(child.path);

    const label = el("div", "tree-label");
    label.setAttribute("role", "treeitem");
    label.setAttribute("aria-level", String(level));
    label.setAttribute("aria-expanded", String(open));
    /* aria-selected belongs on EVERY selectable treeitem, not only the chosen
       one: on role="tree" its absence means "not selectable", so a screen
       reader announced no selection state at all anywhere in the rail. */
    label.setAttribute("aria-selected", "false");
    label.appendChild(el("span", "twisty", open ? "▾" : "▸"));
    label.appendChild(el("span", null, child.name));
    /* The count of the PREPARED node, so it reports what the rail is actually
       showing. `.folder-count` matches the overlay's class name. Recursive over
       the whole subtree, because the rail renders folders collapsed and the
       number beside a closed folder is the thing the user opened it to learn.
       `aria-hidden` because the accessible name already carries the folder
       name, and a bare number appended to it announces as "projects 4" — the
       screen-reader user gets the count from the group's own set size. */
    const badge = el("span", "folder-count", String(child.note_count));
    badge.setAttribute("aria-hidden", "true");
    label.appendChild(badge);
    const toggle = () => setFolderOpen(child.path, !state.expanded.has(child.path));
    label.addEventListener("click", toggle);
    item.appendChild(label);
    nodes.set(`folder:${child.path}`, { node: label, activate: toggle });
    if (open) {
      /* aria-owns completes the APG Navigation Treeview pattern this markup
         adopts. `<li role="none">` strips containment, so the nested
         `role="group"` is appended to the LI and lands as a SIBLING of its
         folder in the accessibility tree — making a child announce its set
         position across two levels ("4 of 7"). aria-owns re-parents it.
         aria-level is explicit on every treeitem, so depth already read
         correctly; this fixes the count, not the depth. */
      const subtree = buildBranch(child, level + 1, nodes);
      /* A COUNTER, not a sanitised path. Slugifying collapses `q3-planning` and
         `q3 planning` onto one id, and strips two different non-ASCII names to
         the same empty stem — and an IDREF resolves to the first match, so the
         second folder's treeitem would claim the FIRST folder's group. A false
         parent/child relationship announced to assistive tech is worse than the
         missing containment aria-owns exists to restore. */
      subtree.id = `tree-group-${treeGroupSeq++}`;
      label.setAttribute("aria-owns", subtree.id);
      item.appendChild(subtree);
    }
    group.appendChild(item);
  }

  /* Month headers, for feed folders only. The notes are ALREADY in date order
     — orderMonthGrouped did that as a data transform — so this loop only has to
     notice where the month changes. Doing the ordering here instead would put
     the DOM in date order while flattenVisible walked the server's title order,
     and the arrow keys would visit the rail in a different sequence than the
     eye. */
  const grouped = MONTH_GROUPED_PATHS.has(node.path);
  let previousMonth = null;

  for (const note of node.notes) {
    if (grouped) {
      const key = monthKeyOf(note);
      if (key !== previousMonth) {
        previousMonth = key;
        /* role="none" and NOT a treeitem: it is a caption, not a destination.
           Making it focusable would put a stop between every month that the
           arrow keys have to step over, and it owns nothing to expand. The
           overlay keeps them non-collapsible for the same reason. */
        const header = el("li", "month-header", key ? monthLabel(key) : "Undated");
        header.setAttribute("role", "none");
        group.appendChild(header);
      }
    }

    const item = el("li", "tree-note");
    item.setAttribute("role", "none");
    const selected = note.id === state.selectedId;
    if (selected) item.classList.add("is-selected");

    /* A real anchor, so middle-click and Cmd-click open a new tab. */
    const link = el("a", "tree-label");
    link.setAttribute("role", "treeitem");
    link.setAttribute("aria-level", String(level));
    link.setAttribute("aria-selected", String(selected));
    link.href = `?id=${encodeURIComponent(note.id)}`;
    if (note.draft) link.appendChild(el("span", "dot-draft", "●"));
    /* In a feed folder the day goes in front of the title, so the date of a
       call or a thread is readable without opening it. Only the DAY — the month
       and year are already the header directly above. Omitted entirely when the
       note has no date, rather than printing a placeholder. */
    if (grouped) {
      const day = dayLabel(note);
      if (day) link.appendChild(el("span", "note-day", `${day} · `));
    }
    link.appendChild(el("span", null, note.title));
    link.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.button !== 0) return;
      event.preventDefault();
      openNote(note.id);
    });
    item.appendChild(link);
    nodes.set(note.id, { node: link, activate: () => openNote(note.id) });
    group.appendChild(item);
  }
  return group;
}

/* Mounts the "Show ingested" control above the rail.
 *
 * INJECTED FROM SCRIPT rather than declared in index.html, because index.html
 * is integrator-owned for the whole of phase 2 — the same reason, and the same
 * pattern, as palette.js mounting its own dialog and the overlay injecting this
 * very button script-side to keep the upstream component unforked.
 *
 * IDEMPOTENT: the browser harness calls this itself, and boot() will too. A
 * second call must not produce a second checkbox with a second listener.
 */
export function wireIngestedToggle() {
  const head = document.querySelector(".rail-head");
  if (!head || head.querySelector(".show-ingested")) return;

  const label = el("label", "show-ingested");
  const box = el("input");
  box.type = "checkbox";
  box.checked = showIngested;
  /* A real checkbox, not a button with aria-pressed. The state is "included or
     not", which is what a checkbox means; a screen reader announces it without
     the label having to spell the state out, and it is operable by space from
     the keyboard for free. */
  box.addEventListener("change", () => {
    showIngested = box.checked;
    saveShowIngested(showIngested);
    /* Re-render from the payload already in state — no refetch. The tier split
       the server sends (vault_count / ingested_count) is precisely what makes
       that possible. */
    renderTree();
  });
  label.appendChild(box);
  label.appendChild(el("span", null, "Show ingested"));
  head.appendChild(label);
}

export async function loadTree() {
  try { dispatch({ tree: await api("/api/tree") }); }
  catch (error) { toast(error.message, "error"); }
}
