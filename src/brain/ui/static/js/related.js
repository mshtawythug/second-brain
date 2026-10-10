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
 * normal answer and draws NO rail. A confidential note answers 403
 * `related_withheld` unless the session serves confidential titles AND bodies
 * — so this module does not ask in that case (`relatedRefusedHere`, which
 * mirrors the route's gate) and shows a one-line notice in the rail's place
 * instead. A WITHHELD note (body not served) is never asked about either, and
 * draws nothing: the inspector's own withheld notice already explains it.
 * `score` is deliberately not displayed: it is a ranking input, not something
 * the reader can act on.
 */

import { $, el, placeInspectorBlock } from "/static/js/dom.js";
import { perNoteFetch } from "/static/js/note_fetch.js";
import {
  isConfidentialNote, servesConfidential, state, subscribe,
} from "/static/js/store.js";

/* What a row with an empty title is called: a link with no text has no
   accessible name. Same word graph.js uses. */
const UNTITLED = "Untitled";

const RAIL_LABEL = "Related notes";

/* What stands in the rail's place when this server refuses it. One line of
   text, no markup — it names no candidate, only that the rail is hidden. */
const RELATED_REFUSED_NOTICE = "Related notes are hidden for confidential notes on this server.";

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

  /* A confidential note this server will not rank: say so, and do NOT ask.
     The request could only come back 403 `related_withheld`, leaving a blank
     rail and a console error where the reader deserves a reason. */
  if (relatedRefusedHere(note)) {
    placeInspectorBlock(host, buildRefusedNotice());
    return;
  }

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

/* MIRRORS routes_related.note_related's gate EXACTLY, and the two change
   together: the route refuses a confidential root unless the session serves
   confidential titles AND bodies
   (`strict = not (ctx.serve_confidential_titles and ctx.serve_confidential_bodies)`).
   Stricter than the graph's gate on purpose — a related row carries a snippet,
   which is body text. This is the ONE place the client decides it. */
function relatedRefusedHere(note) {
  return isConfidentialNote(note) && !(
    servesConfidential("serve_confidential_titles")
    && servesConfidential("serve_confidential_bodies")
  );
}

/* The block element itself (class `related-rail`), so the notice keeps the
   rail's slot in the canonical order (placeInspectorBlock) and is removed with
   it on the next render. A section, not a nav: it holds no links. */
function buildRefusedNotice() {
  const section = el("section", "related-rail");
  section.setAttribute("aria-label", RAIL_LABEL);
  section.appendChild(el("p", "related-withheld", RELATED_REFUSED_NOTICE));
  return section;
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
