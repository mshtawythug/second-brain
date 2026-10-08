/* The related-notes rail: documents similar to the open note, with a snippet.
 *
 * SAME SHAPE AS marginalia.js AND graph.js, and every contract in
 * marginalia.js's header applies here verbatim — read it first. In short:
 *
 * ATTACHED AFTER THE NOTE'S OWN CHILDREN of `#inspector`. `.inspector` is
 * `grid-template-rows: auto 1fr`; `.note-head` must keep `auto` and
 * `.note-body`/`.editor` must keep `1fr`, so a block lands only on an implicit
 * `auto` row after them. It goes in through `placeInspectorBlock` (dom.js),
 * which keeps it LAST of the three blocks whatever order the fetches land in.
 *
 * SUBSCRIBER ORDER IS A WIRING CONTRACT. `renderInspector` wipes `#inspector`
 * (`host.textContent = ""`) on every dispatch, so this renderer must be
 * registered AFTER it. main.js calls `wireRelated()` after `wireGraph()`.
 *
 * NO innerHTML, NO insertAdjacentHTML, NO outerHTML, NO DOMParser. Every node
 * is built with `el()` from dom.js, which sets `textContent`. Titles and
 * snippets are document text, and document text is never markup.
 *
 * THE CONTRACT CONSUMED: `GET /api/notes/{id}/related` answers
 * `{id, related: [{id, title, vault_path, source, score, snippet,
 * snippet_truncated}], count, vector_sim_floor}`. An empty `related` is a
 * normal answer and draws NO rail. A withheld confidential note answers 403
 * `related_withheld` — and this module never asks for a withheld note at all.
 * `score` is deliberately not displayed: it is a ranking input, not something
 * the reader can act on.
 */

import { $, el, placeInspectorBlock } from "/static/js/dom.js";
import { perNoteFetch } from "/static/js/note_fetch.js";
import { state, subscribe } from "/static/js/store.js";

/* What a row with an empty title is called: a link with no text has no
   accessible name. Same word graph.js uses. */
const UNTITLED = "Untitled";

const RAIL_LABEL = "Related notes";

let wired = false;

/* The related rows for a note, through the shared per-note fetch
 * (note_fetch.js — read its header for the cache, the revision key, the
 * failure sentinel and the stale-response guard). A FAILED fetch caches
 * `null`, which draws nothing. */
const fetchRelated = perNoteFetch({
  endpoint: "related",
  normalise: (payload) => normalise(payload),
  failed: null,
  rerender: () => renderRelated(),
});

/* Idempotent, like wireMarginalia() and wireGraph(): a second call must not
   register a second subscriber drawing a second rail over the first. */
export function wireRelated() {
  if (wired) return;
  wired = true;
  subscribe(renderRelated);
  renderRelated();
}

export function renderRelated() {
  const host = $("inspector");
  if (!host) return;

  /* Remove our own previous block BEFORE deciding whether to draw. Not every
     dispatch rebuilds the inspector, and a rail describing the previous note
     is worse than none. */
  const stale = host.querySelector(".related-rail");
  if (stale) stale.remove();

  const note = state.note;
  /* Editing: the editor owns the 1fr track, and similarity computed from the
     saved text describes something the user is in the middle of changing.
     Withheld: nothing about a withheld note is drawn — and, because this check
     precedes the fetch, nothing about it is even requested. */
  if (!note || state.editing || note.withheld) return;

  /* `undefined` means the request is in flight (NOT awaited — the note is
     already painted, and the rail must never stand between the reader and
     it); this renders again when it lands. */
  const cached = fetchRelated(note);
  if (cached === undefined) return;

  /* `null` (failed) and `[]` (nothing similar) both draw NOTHING — not an
     empty heading. A heading over no rows is chrome promising a list and
     delivering a blank. */
  if (!cached || !cached.length) return;
  placeInspectorBlock(host, buildRail(cached));
}

/* The payload, validated at the boundary. Rows without a string id are
   dropped — they cannot be linked to. Anything that is not a list is `null`. */
function normalise(payload) {
  if (!payload || !Array.isArray(payload.related)) return null;
  return payload.related.filter((row) => row && typeof row.id === "string" && row.id);
}

function buildRail(rows) {
  const nav = el("nav", "related-rail");
  nav.setAttribute("aria-label", RAIL_LABEL);
  nav.appendChild(el("h2", "rail-heading", "Related"));

  const list = el("ul");
  for (const row of rows) list.appendChild(buildRow(row));
  nav.appendChild(list);
  return nav;
}

function buildRow(row) {
  const item = el("li");
  const title = String(row.title ?? "").trim() || UNTITLED;
  /* The same href the backlinks rail and the graph use, so a middle-click or a
     copied link opens the same note a plain click does. */
  const link = el("a", null, title);
  link.setAttribute("href", `?id=${encodeURIComponent(row.id)}`);
  link.dataset.noteId = row.id;
  link.addEventListener("click", (event) => {
    event.preventDefault();
    openNoteById(row.id);
  });
  item.appendChild(link);

  /* No snippet, no element — an empty paragraph is a band of padding that
     says nothing. A truncated snippet is marked, so a cut sentence does not
     read as the document's own full stop. */
  const snippet = String(row.snippet ?? "").trim();
  if (snippet) {
    const shown = row.snippet_truncated === true ? `${snippet}…` : snippet;
    item.appendChild(el("p", "related-snippet", shown));
  }
  return item;
}

/* Imported lazily, at click time, to keep this module out of the
   inspector <-> tree import cycle that store.js's header documents — the same
   pattern, for the same reason, as marginalia.js and graph.js. */
async function openNoteById(id) {
  const inspector = await import("/static/js/inspector.js");
  inspector.openNote(id);
}
