/* The command palette: jump to any note by title, from anywhere.
 *
 * WHY ⌘P AND NOT ⌘K. The T12 plan row left this undecided. It is decided here:
 * ⌘K is already bound, in js/keys.js, to "focus the search box", and `/` is a
 * second way to reach the same box. Taking ⌘K for the palette would DELETE a
 * shipped behaviour in order to add a new one — the two are not interchangeable
 * (the search box queries document CONTENT through the server; the palette
 * filters note TITLES already in memory). ⌘P is free, and Cmd/Ctrl+P's browser
 * default — print — is one this app has no use for. `preventDefault` suppresses
 * it, so nothing is lost either.
 *
 * NO NETWORK CALL. The corpus this searches is `state.tree`, which main.js's
 * boot() has already fetched from /api/tree for the vault rail. A palette that
 * re-fetched would be a second copy of the same data that could disagree with
 * the rail on screen, and would put a request between the keystroke and the
 * first row.
 *
 * THE DIALOG IS BUILT HERE, not declared in index.html. Two reasons, one of
 * them structural: this module then has ONE integration point (a wirePalette()
 * call) instead of three that must agree — markup, ids, and behaviour drift
 * apart exactly the way a hand-maintained roster does. The other is that the
 * markup is meaningless without the script: an empty <dialog> in the shell is a
 * feature that silently does nothing if the module fails to load, rather than a
 * feature that is visibly absent.
 *
 * wirePalette() IS IDEMPOTENT. Calling it twice must not mint a second dialog
 * or a second global keydown listener — the second would call showModal() on an
 * already-open dialog, which throws InvalidStateError and leaves the palette
 * wedged. The browser harness calls it directly (js/main.js is owned by the
 * phase integrator), so the double call is a real path, not a hypothetical.
 */

import { el } from "/static/js/dom.js";
import { state } from "/static/js/store.js";
import { openNote } from "/static/js/inspector.js";

/* Rows rendered at most. A 1,392-note vault with an empty query would otherwise
   build 1,392 <li> elements on every keystroke, and nobody scrolls a palette. */
const MAX_ROWS = 25;

/* Scoring weights. A CONTIGUOUS hit beats any scattered one, a hit at a word
   boundary beats one inside a word, and among equals the earlier hit wins.
   Ties fall back to vault order, so the ranking is total and stable rather than
   dependent on Array.prototype.sort's internals. */
const SUBSTRING_BONUS = 1000;
const WORD_START_BONUS = 100;

let dialog = null;      /* also the idempotence flag — see the header */
let input = null;
let list = null;
let rows = [];          /* the ranked notes currently rendered */
let highlight = 0;

/* Every note in the loaded tree, depth-first, folders before notes — the same
   order the rail draws. Pure: takes the tree, returns a list, touches no DOM. */
export function collectNotes(tree) {
  const found = [];
  const walk = (node) => {
    if (!node) return;
    for (const child of node.children || []) walk(child);
    for (const note of node.notes || []) {
      found.push({ id: note.id, title: note.title, path: note.path });
    }
  };
  walk(tree);
  return found;
}

/* A score for `title` against `query`, or null when it does not match at all.
   Higher is better. Pure. */
export function scoreTitle(title, query) {
  const text = (title || "").toLowerCase();
  const needle = (query || "").trim().toLowerCase();
  if (!needle) return 0;                       /* no query: everything ties */

  const at = text.indexOf(needle);
  if (at !== -1) {
    const wordStart = at === 0 || !/[a-z0-9]/.test(text[at - 1]);
    return SUBSTRING_BONUS + (wordStart ? WORD_START_BONUS : 0) - at;
  }

  /* Fuzzy leg: the characters in order but not adjacent, which is what lets
     "dpn" find "Deep Note". Scored by SPREAD — the tightest run of the query's
     characters wins — and always below any contiguous hit, because a substring
     match is what the user more often meant. */
  let cursor = 0;
  let first = -1;
  let last = -1;
  for (const character of needle) {
    const found = text.indexOf(character, cursor);
    if (found === -1) return null;
    if (first === -1) first = found;
    last = found;
    cursor = found + 1;
  }
  return -(last - first);
}

/* The ranked matches. Stable by construction: the original index is the
   explicit tie-break, not an assumption about the sort algorithm. */
export function rankNotes(notes, query) {
  return notes
    .map((note, order) => ({ note, order, score: scoreTitle(note.title, query) }))
    .filter((row) => row.score !== null)
    .sort((a, b) => (b.score - a.score) || (a.order - b.order))
    .map((row) => row.note);
}

function renderRows(query) {
  rows = rankNotes(collectNotes(state.tree), query).slice(0, MAX_ROWS);
  list.textContent = "";
  rows.forEach((note, index) => {
    const row = el("li", "palette-opt");
    row.id = `palette-opt-${index}`;
    row.setAttribute("role", "option");
    /* The id the harness — and any future feature — reads to say WHICH note a
       row stands for. The title alone is not an identity: two notes in
       different folders may share one. */
    row.dataset.noteId = note.id;
    row.appendChild(el("span", "palette-opt-title", note.title));
    row.appendChild(el("span", "palette-opt-path", note.path || ""));
    /* mousedown, not click: click fires after the dialog has already taken
       focus changes, and pointer activation should not depend on that. */
    row.addEventListener("mousedown", (event) => {
      event.preventDefault();
      activate(index);
    });
    list.appendChild(row);
  });
  setHighlight(0);
}

/* Move the highlight, and ANNOUNCE it.
 *
 * Focus never leaves the combobox — that is the whole point of the ARIA 1.2
 * pattern, since a listbox that stole focus would break typing. So
 * aria-activedescendant is the ONLY channel by which assistive tech learns
 * which row is current; a version that toggled the CSS class alone would look
 * correct and be silent. */
function setHighlight(next) {
  if (rows.length === 0) {
    highlight = 0;
    input.removeAttribute("aria-activedescendant");
    return;
  }
  highlight = Math.max(0, Math.min(next, rows.length - 1));
  [...list.children].forEach((row, index) => {
    const current = index === highlight;
    row.setAttribute("aria-selected", String(current));
    row.classList.toggle("is-active", current);
  });
  input.setAttribute("aria-activedescendant", `palette-opt-${highlight}`);
  const row = list.children[highlight];
  if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
}

function activate(index) {
  const note = rows[index];
  if (!note) return;               /* Enter on an empty list is a no-op */
  dialog.close();
  openNote(note.id);
}

function onPaletteKeydown(event) {
  const delta = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
  if (delta !== 0) {
    event.preventDefault();
    setHighlight(highlight + delta);
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    activate(highlight);
  }
  /* Escape is deliberately NOT handled: <dialog> cancels natively, and closing
     is not activating. A handler that opened the highlighted row on close would
     navigate a user who explicitly backed out. */
}

function onGlobalKeydown(event) {
  if (!(event.metaKey || event.ctrlKey) || event.key !== "p") return;
  event.preventDefault();                       /* suppress the print dialog */
  openPalette();
}

export function openPalette() {
  if (!dialog) wirePalette();
  if (dialog.open) return;
  input.value = "";
  renderRows("");
  dialog.showModal();
  input.focus();
}

export function wirePalette() {
  if (dialog) return dialog;                    /* idempotent — see header */

  dialog = el("dialog", "palette");
  dialog.id = "palette";
  dialog.setAttribute("aria-label", "Jump to a note");

  const box = el("div", "palette-box");

  input = el("input", "palette-input");
  input.id = "palette-input";
  input.type = "text";
  input.placeholder = "Jump to a note…";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Jump to a note");
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "true");
  input.setAttribute("aria-controls", "palette-list");
  input.setAttribute("aria-autocomplete", "list");

  list = el("ul", "palette-list");
  list.id = "palette-list";
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "Matching notes");

  box.appendChild(input);
  box.appendChild(list);
  dialog.appendChild(box);
  document.body.appendChild(dialog);

  input.addEventListener("input", () => renderRows(input.value));
  input.addEventListener("keydown", onPaletteKeydown);
  document.addEventListener("keydown", onGlobalKeydown);
  return dialog;
}
