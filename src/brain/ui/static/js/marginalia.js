/* The marginalia block: breadcrumbs and a table of contents for the open note.
 *
 * WHERE IT ATTACHES, AND WHY THERE. `#inspector` is the only container the
 * reading surface has: `.shell` is a three-column grid (rail | ledger |
 * inspector) declared in index.html and layout.css, and BOTH of those files are
 * integrator-owned for the whole of phase 2. A true right-hand marginalia
 * *column* needs a fourth grid track and a DOM slot, so this module does the
 * one thing it can do without touching either: it appends its own
 * `<aside class="marginalia">` to `.inspector`.
 *
 * APPENDED LAST, and that is load-bearing rather than incidental. `.inspector`
 * is `grid-template-rows: auto 1fr` — the first child gets `auto`, the second
 * `1fr`, and any further child an implicit `auto` row. `.note-head` must keep
 * `auto` and `.note-body`/`.editor` must keep `1fr` (layout.css records the
 * measurement behind that: an explicit height defeats grid's `stretch` but is
 * only a base size under `flex-grow`, which is what makes `resize: vertical` on
 * the editor mean anything). Inserting this block anywhere earlier hands the
 * `1fr` track to the wrong child and pushes the body to the foot of the pane.
 * Appending it last is also what layout.css's `.inspector > * { align-self:
 * start }` explicitly anticipates: "the NEXT child added here is safe".
 *
 * SUBSCRIBER ORDER IS A WIRING CONTRACT. `renderInspector` rebuilds `#inspector`
 * from scratch on every dispatch (`host.textContent = ""`), so this renderer
 * must run AFTER it or its block is wiped the instant it is drawn. main.js
 * registers `subscribe(renderInspector)` last of the three at boot, so
 * `wireMarginalia()` must be called AFTER that line — not alongside
 * `wirePalette()` above it. Getting this backwards does not error; the feature
 * simply never appears. The integration note on the task records the same
 * constraint.
 *
 * NO innerHTML ANYWHERE. Every node is built with `el()` from dom.js, which
 * sets `textContent`. Heading text arrives from the server as a plain string
 * (`extract_headings` returns the heading's text content, not its markup), and
 * it is inserted as text, never parsed.
 */

import { api } from "/static/js/api.js";
import { $, el } from "/static/js/dom.js";
import { state, subscribe } from "/static/js/store.js";

let wired = false;

/* noteId -> the backlink rows already fetched for it.
 *
 * renderMarginalia runs on EVERY dispatch — toggling the editor, saving, moving
 * — and without a cache each of those would re-request the same links. The
 * cache is what makes the fetch happen once per note rather than once per
 * render.
 *
 * A FAILED fetch caches `[]` deliberately. The alternative is retrying on every
 * subsequent dispatch, which turns one unreachable endpoint into a request
 * storm against a server that is already failing. The cost is that a transient
 * failure stays blank until the note is reopened — the right trade for a rail
 * that is, by construction, supplementary to the note.
 */
const backlinkCache = new Map();
const inFlight = new Set();

/* Idempotent by design, and that idempotence is now LOAD-BEARING rather than
 * anticipatory: main.js's boot() calls this, AND the browser harness calls it
 * again from page.evaluate (it was written while index.html and main.js were
 * integrator-owned and it must not edit them). Both calls happen on every test
 * run, so the second must be a no-op rather than a second subscriber drawing a
 * second TOC over the first. Same contract as wirePalette(). */
export function wireMarginalia() {
  if (wired) return;
  wired = true;
  subscribe(renderMarginalia);
  renderMarginalia();
}

/* The headings the SERVER extracted, never a set derived here.
 *
 * DEFECT S4 LIVES ON THIS LINE. `notes_service.read_note` renders
 * `strip_redundant_title_heading(body, title)` and extracts headings from that
 * SAME stripped string, so `note.headings` is in agreement with `note.html` by
 * construction. A TOC that synthesised its own leading entry from `note.title`
 * — which is exactly what the unstripped body would have yielded — would open
 * with a link to an anchor the rendered HTML does not contain. So: no
 * synthesised entries, and nothing derived from `note.body` (which is the raw,
 * UNSTRIPPED markdown source).
 */
function tocEntries(note) {
  return Array.isArray(note.headings) ? note.headings : [];
}

function breadcrumbTrail(note) {
  return (note.vault_path || "").split("/").filter(Boolean);
}

export function renderMarginalia() {
  const host = $("inspector");
  if (!host) return;

  /* Remove our own previous block before deciding whether to draw a new one.
     renderInspector's wipe usually does this for us, but not every dispatch
     that changes OUR inputs rebuilds the inspector, and a stale TOC describing
     the previously-open note is worse than none. */
  const stale = host.querySelector(".marginalia");
  if (stale) stale.remove();

  const note = state.note;
  /* Three reasons to draw nothing. `editing` is the interesting one: the editor
     wants the 1fr track to itself, and a table of contents beside a textarea of
     raw markdown describes a rendering the reader is not looking at. */
  if (!note || state.editing || note.withheld) return;

  const headings = tocEntries(note);
  const crumbs = breadcrumbTrail(note);
  /* No headings and no path is not an empty rail — it is no rail. An empty
     <nav> is chrome that promises a table of contents and delivers a blank. */
  if (!headings.length && !crumbs.length) return;

  const aside = el("aside", "marginalia");
  aside.setAttribute("aria-label", "Note contents");

  if (crumbs.length) aside.appendChild(renderBreadcrumbs(crumbs));
  if (headings.length) aside.appendChild(renderToc(headings));

  host.appendChild(aside);

  /* LAST, and deliberately NOT awaited. The note is already in the DOM by the
     time this runs; the backlinks are a second request that must never stand
     between the reader and the note they asked for. See attachBacklinks. */
  attachBacklinks(note, aside);
}

/* The backlinks rail: which documents link INTO the open note.
 *
 * SYNCHRONOUS WHEN CACHED, one fetch otherwise. The function is called from
 * renderMarginalia on every dispatch, so the cache is what keeps that from
 * meaning a request per render.
 *
 * NOT AWAITED, and that is the T14 requirement rather than a style choice. The
 * note body is painted by renderInspector before this runs; making the rail's
 * request block would put a second round trip between the reader and a note
 * that is already fetched and ready. A blocking form is the mutation this is
 * tested against.
 *
 * SILENT ON FAILURE — no toast, no placeholder, no empty rail. `/api/notes/…`
 * already reported anything that stopped the NOTE from loading; a second error
 * for a supplementary rail tells the reader about a request they never made,
 * about a surface they may not be looking at. `api()` throws on a non-2xx and
 * does NOT toast — toasting is the caller's choice, and this caller declines.
 */
function attachBacklinks(note, aside) {
  const cached = backlinkCache.get(note.id);
  if (cached !== undefined) {
    if (cached.length) aside.appendChild(renderBacklinks(cached));
    return;
  }
  if (inFlight.has(note.id)) return;
  inFlight.add(note.id);

  api(`/api/notes/${encodeURIComponent(note.id)}/links`).then(
    (payload) => {
      inFlight.delete(note.id);
      backlinkCache.set(
        note.id, Array.isArray(payload && payload.backlinks) ? payload.backlinks : []
      );
      /* Re-render rather than appending to the captured `aside`: by the time
         this resolves the reader may have opened another note, and that aside
         is detached. The id check is the stale-response guard — without it a
         slow response for note A paints A's backlinks under note B. */
      if (state.note && state.note.id === note.id) renderMarginalia();
    },
    () => {
      inFlight.delete(note.id);
      backlinkCache.set(note.id, []);
    },
  );
}

function renderBacklinks(rows) {
  const nav = el("nav", "backlinks-rail");
  nav.setAttribute("aria-label", "Linked from");
  nav.appendChild(el("h2", "rail-heading", "Linked from"));

  const list = el("ul");
  for (const row of rows) {
    const item = el("li");
    /* The TITLE, not the link text. `link_text` is what the AUTHOR typed inside
       the brackets — an alias, a partial, sometimes a lower-cased fragment —
       and a rail that showed it would name the same document differently on
       every page that links to it. The title is the document's own name. */
    const link = el("a", null, row.title);
    link.setAttribute("href", `?id=${encodeURIComponent(row.id)}`);
    link.dataset.noteId = row.id;
    /* `derived` edges are computed, not authored, so they are marked: a reader
       deciding whether a connection is one they made needs to see which are
       theirs. The attribute carries it rather than a class, so the distinction
       survives restyling. */
    if (row.link_kind) link.dataset.linkKind = row.link_kind;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      openNoteById(row.id);
    });
    item.appendChild(link);
    list.appendChild(item);
  }

  nav.appendChild(list);
  return nav;
}

/* Imported lazily, at click time, to keep this module out of the
   inspector <-> tree import cycle that store.js's header documents. */
async function openNoteById(id) {
  const inspector = await import("/static/js/inspector.js");
  inspector.openNote(id);
}

function renderBreadcrumbs(crumbs) {
  const nav = el("nav", "breadcrumbs");
  nav.setAttribute("aria-label", "Vault location");
  const list = el("ol");
  for (const segment of crumbs) list.appendChild(el("li", null, segment));
  nav.appendChild(list);
  return nav;
}

function renderToc(headings) {
  const nav = el("nav", "toc");
  nav.setAttribute("aria-label", "On this page");
  const list = el("ol");

  for (const heading of headings) {
    const item = el("li");
    /* The level drives indentation in CSS rather than nested <ol>s. Markdown
       heading levels are not guaranteed to nest properly — a document may jump
       h2 -> h4 — and building real nesting from a flat, possibly-skipping
       sequence invents a hierarchy the author did not write. */
    item.dataset.level = String(heading.level);

    const link = el("a", null, heading.text);
    link.setAttribute("href", `#${heading.id}`);
    link.dataset.headingId = heading.id;
    link.addEventListener("click", (event) => onTocClick(event, nav, heading.id));
    item.appendChild(link);
    list.appendChild(item);
  }

  nav.appendChild(list);
  return nav;
}

function onTocClick(event, nav, headingId) {
  /* preventDefault because the default action is a HASH NAVIGATION, and the
     hash is not what this feature is. The browser writes location.hash whether
     or not the target exists and whether or not anything moved, so relying on
     it would make a TOC that links to nothing indistinguishable from one that
     works. Scrolling is performed explicitly below, against the element. */
  event.preventDefault();

  const target = document.getElementById(headingId);
  /* Absent target: mark nothing, scroll nothing. This is the S4 case, and
     failing silently here rather than throwing keeps one bad entry from
     breaking the rest of the rail — the browser test is what makes the
     condition visible, not a console error the reader never sees. */
  if (!target) return;

  target.scrollIntoView({ block: "start" });

  /* aria-current, not a class alone: the TOC is a navigation landmark, so the
     active entry has to be announced to a screen reader rather than merely
     tinted for a sighted one. */
  for (const other of nav.querySelectorAll("a[aria-current]")) {
    other.removeAttribute("aria-current");
  }
  event.currentTarget.setAttribute("aria-current", "location");
}
