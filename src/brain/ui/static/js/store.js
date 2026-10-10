/* The store: one plain object, a subscriber list, and the URL <-> state sync.
 *
 * `state` is exported as a LIVE OBJECT, not a copy. Every module that mutates
 * it — `state.draftBody` in inspector.js, `state.searchError` in results.js,
 * `state.expanded` in tree.js — is writing through the same reference the
 * renderers read, exactly as when all of this lived in one file. Exporting a
 * getter or a frozen snapshot instead would have been a behaviour change
 * wearing the costume of a refactor.
 *
 * URL is the source of truth for shareable state (q, filters, id). NAVIGATIONS
 * are written with history.pushState so Back works; keystroke-level typing is
 * written with a debounced history.replaceState so Back does not walk the query
 * one character at a time. See the URL section below for how the two are told
 * apart. Ephemeral state — editor mode, tree expansion, theme — lives in
 * localStorage, never the URL.
 */

export const state = {
  q: "", filters: { source: "", type: "", tag: "", after: "", before: "" },
  /* WHICH PAGE OF THE RANKING IS SHOWING. Deliberately NOT in the URL, unlike
     `q`, the filters and `id`. A shareable link denotes a SEARCH; an offset
     denotes a position inside one particular ranking of it, and the ranking is
     recomputed on every request — `SearchQuery.fetch_limit` records that a page
     re-pays the whole rank leg, measured at ~5.9 s for 545 matches. So a
     "page 4" link would be a slow link to a page whose contents depend on when
     it is opened. `readUrl()` resets it for the same reason: whatever the
     address bar says, it says page one, and the view must not disagree with it.
     Which page you are on is recovered by paging, not by history. */
  offset: 0,
  results: [], meta: null, searchStatus: "idle",
  selectedId: null, note: null, editing: false,
  saveStatus: "saved", draftBody: "",
  tree: null, expanded: loadExpanded(), health: null, sessionId: null,
};

/* PER-NOTE REVISIONS — the one invalidation mechanism for every per-note cache.
 *
 * marginalia.js (backlinks), graph.js and related.js each cache what they
 * fetched for a note, so a re-render on every dispatch costs no request. The
 * cost of that cache was that it never expired: save a note that adds a
 * `[[wikilink]]` and all three blocks kept showing the old neighbourhood until
 * a full reload. So their shared fetch helper (`perNoteFetch`, note_fetch.js)
 * keys every cache on `noteKey(id)` rather than on the bare id, and
 * inspector.js calls `bumpNoteRevision(id)` on a
 * SUCCESSFUL save. The saved note's key moves, its stale entries stop
 * matching, and its blocks refetch on the render the save's dispatch triggers.
 *
 * PER NOTE, NOT GLOBAL, and that is the point. A single global counter folded
 * into every key would invalidate every note's cache on any save; here only
 * the saved note's key changes, so every other note's entries survive. And
 * because nothing but a save moves a revision, an ordinary dispatch — toggling
 * the editor, an unsaved edit, opening another note and coming back — still
 * refetches nothing.
 *
 * A Map, held here rather than on `state`: it is not something any renderer
 * draws, and `dispatch` has no business replacing it.
 *
 * SUPERSEDED KEYS ARE RETAINED, DELIBERATELY. A bump leaves the old `id:rev`
 * entries in each module's cache, unreachable, rather than pruning them on
 * write. This is a single-user local UI: one dead entry per module per save is
 * trivial, and the per-note caches were already unbounded (one entry per note
 * opened) before revisions existed. Pruning would add a second code path to
 * the one invalidation rule for no observable gain. */
const noteRevisions = new Map();

function noteRevision(id) {
  return noteRevisions.get(id) || 0;
}

export function bumpNoteRevision(id) {
  noteRevisions.set(id, noteRevision(id) + 1);
}

/* The cache key for anything fetched about note `id` at its current revision. */
export function noteKey(id) {
  return `${id}:${noteRevision(id)}`;
}

/* ---------------------------------------------- the confidential lenses --
 *
 * The server serves a confidential note through TWO separate gates, and
 * `/api/health` reports both (routes_meta.health): `serve_confidential_bodies`
 * (may this session read a confidential note it opened) and
 * `serve_confidential_titles` (may a surface name one unprompted). The graph
 * and the related rail are refused by the server on these gates, so each of
 * those modules mirrors its route's gate EXACTLY rather than asking and
 * collecting a 403 — see `graphRefusedHere` (graph.js) and
 * `relatedRefusedHere` (related.js).
 *
 * FAILS CLOSED. Until `/api/health` has answered, if it never does, or if it
 * omits a key, the gate reads as NOT served. The server is the authority;
 * guessing "served" would only issue a request the server then refuses. */
const CONFIDENTIAL = "confidential";

/* The note's OWN tier, from its own payload (notes_service.read_note emits
   `sensitivity` only when it is not the default). */
export function isConfidentialNote(note) {
  return Boolean(note) && note.sensitivity === CONFIDENTIAL;
}

export function servesConfidential(gate) {
  return Boolean(state.health) && state.health[gate] === true;
}

const listeners = [];
export function subscribe(fn) { listeners.push(fn); }
export function dispatch(patch) { Object.assign(state, patch); listeners.forEach((fn) => fn()); }

function loadExpanded() {
  try { return new Set(JSON.parse(localStorage.getItem("brain-ui-expanded") || "[]")); }
  catch (e) { return new Set(); }
}

export function saveExpanded() {
  try { localStorage.setItem("brain-ui-expanded", JSON.stringify([...state.expanded])); }
  catch (e) { /* private mode — expansion state is not worth failing over */ }
}

/* --------------------------------------------------------------- the URL --
 *
 * TWO KINDS OF URL CHANGE, AND TELLING THEM APART IS THE WHOLE FEATURE.
 *
 * A NAVIGATION is somewhere the user can come Back to: opening a note, picking
 * a filter. It gets `pushState`, so Back returns to where they were.
 *
 * TYPING is not. `q` changes on every keystroke, and a `pushState` per
 * keystroke means Back walks the query character by character — "vendo",
 * "vend", "ven", … — which is the worst Back button on the web. Typing keeps
 * `replaceState`, debounced, so the URL stays shareable without ever growing
 * the history stack.
 *
 * THE DECISION IS MADE HERE, not at the call sites, and that is deliberate.
 * `syncUrl()` is called from three places — `openNote` in inspector.js, and the
 * query and filter handlers in main.js — and inspector.js belongs to another
 * task. More importantly, a `syncUrl({push: true})` flag would put the
 * classification in the caller's head, where the next call site added by
 * someone who has not read this comment gets it wrong silently. Deriving it
 * from WHAT ACTUALLY CHANGED cannot be forgotten by a new caller.
 */

/* The params whose change constitutes a navigation. DERIVED, not restated: the
   filter keys come from `state.filters` itself, so adding a filter cannot leave
   this list behind — the hand-maintained-roster failure that has cost this
   project a shipped WCAG defect and two blind guards this week. `q` is
   deliberately absent; that absence IS the debounce exception. */
const NAVIGATIONAL_PARAMS = ["id", ...Object.keys(state.filters)];

const URL_DEBOUNCE_MS = 200;

let urlTimer = null;

/* The query string the current state implies. */
function targetQuery() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  for (const [key, value] of Object.entries(state.filters)) if (value) params.set(key, value);
  if (state.selectedId) params.set("id", state.selectedId);
  return params.toString();
}

/* Both sides normalised through URLSearchParams before comparison, so a
   difference in encoding or key order never reads as a change. */
function currentQuery() {
  return new URLSearchParams(location.search).toString();
}

function href(query) {
  return query ? `?${query}` : location.pathname;
}

function isNavigation(query) {
  const next = new URLSearchParams(query);
  const now = new URLSearchParams(location.search);
  return NAVIGATIONAL_PARAMS.some(
    (key) => (next.get(key) || "") !== (now.get(key) || "")
  );
}

export function syncUrl() {
  const query = targetQuery();

  /* Already there. Recording it again would push a duplicate entry that Back
     appears to ignore — the user presses it and nothing moves. This is also
     what makes `popstate` safe: restoring a note calls openNote, which calls
     back into here, and the URL it would write is the one already showing. */
  if (query === currentQuery()) {
    clearTimeout(urlTimer);
    return;
  }

  if (isNavigation(query)) {
    /* Immediate, and the pending keystroke replace is dropped rather than
       allowed to fire afterwards. Debouncing a navigation would coalesce two
       notes opened inside the window into ONE history entry, silently losing
       the first — the exact Back target the user is reaching for. */
    clearTimeout(urlTimer);
    history.pushState(null, "", href(query));
    return;
  }

  clearTimeout(urlTimer);
  urlTimer = setTimeout(() => {
    /* Recomputed rather than closed over: the last keystroke wins. */
    history.replaceState(null, "", href(targetQuery()));
  }, URL_DEBOUNCE_MS);
}

export function readUrl() {
  const params = new URLSearchParams(location.search);
  state.q = params.get("q") || "";
  for (const key of Object.keys(state.filters)) state.filters[key] = params.get(key) || "";
  state.selectedId = params.get("id");
  /* The URL carries no offset, so reading it means page one. Without this a
     Back out of a search — which re-reads the URL and re-runs the search —
     would ask for the offset the user had paged to under a URL that never
     mentioned it, and the ledger would show page 4 of a search the address bar
     describes as page 1. Resetting HERE rather than at the two call sites is
     the same rule `NAVIGATIONAL_PARAMS` follows: derived from what the URL
     actually says, so a third caller cannot forget it. */
  state.offset = 0;
}
