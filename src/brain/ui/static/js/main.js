/* brain ui — the entry point.
 *
 * No framework, no bundler, no CDN. Plain ES modules, loaded directly, so the
 * wheel stays pure-Python and the app works fully offline. Nothing here is
 * fetched from the network.
 *
 * State lives in one plain object with a subscriber list (store.js); each
 * render function updates only its own subtree with targeted DOM operations. No
 * virtual DOM, no full re-render.
 *
 * URL is the source of truth for shareable state (q, filters, id): navigations
 * are pushed, keystroke-level typing is replaced (see store.js), and popstate
 * rebuilds from the URL rather than from a history stack of this app's own.
 * Ephemeral state — editor mode, tree expansion, theme — lives in
 * localStorage, never the URL.
 *
 * This file owns BOOT ORDER, and the order is load-bearing. See boot().
 */

import { api } from "/static/js/api.js";
import { $, toast } from "/static/js/dom.js";
import { dispatch, readUrl, state, subscribe, syncUrl } from "/static/js/store.js";
import {
  loadTree, onTreeKeydown, renderTree, wireIngestedToggle,
} from "/static/js/tree.js";
import { renderResults, runSearch, scheduleSearch } from "/static/js/results.js";
import { openNote, renderInspector } from "/static/js/inspector.js";
import { wireKeys } from "/static/js/keys.js";
/* palette.js registers its OWN ⌘P listener and builds its own <dialog>, so this
   import plus the wirePalette() call in boot() is the whole integration — there
   is deliberately no keys.js edit and no markup in index.html to keep in sync. */
import { wirePalette } from "/static/js/palette.js";
/* marginalia.js is NOT symmetric with palette.js, and the difference is the
   whole reason its call site is where it is. wirePalette() builds a <dialog> on
   document.body and subscribes to nothing, so it is order-free. wireMarginalia()
   subscribes to the store and draws INTO #inspector — see boot(). */
import { wireMarginalia } from "/static/js/marginalia.js";
import { wireThread } from "/static/js/thread.js";

/* The filter controls, as [element id, state.filters key] pairs. Declared once
   because boot() seeds them from the URL and wireControls() binds them, and two
   hand-maintained copies of the same list is how one of them silently loses a
   filter. */
const FILTERS = [
  ["f-source", "source"], ["f-type", "type"],
  ["f-tag", "tag"], ["f-after", "after"], ["f-before", "before"],
];

async function loadFacets() {
  try {
    const facets = await api("/api/facets");
    fillSelect($("f-source"), "Source", facets.sources);
    fillSelect($("f-type"), "Type", facets.content_types);
    fillSelect($("f-tag"), "Tag", facets.tags);
  } catch (e) { /* dropdowns degrade to empty; search still works */ }
}

function fillSelect(node, label, buckets) {
  node.textContent = "";
  node.appendChild(new Option(label, ""));
  for (const bucket of buckets) {
    const text = bucket.count == null ? bucket.value : `${bucket.value} (${bucket.count})`;
    node.appendChild(new Option(text, bucket.value));
  }
}

function newNote() {
  const dialog = $("new-dialog");
  $("new-title").value = ""; $("new-folder").value = ""; $("new-tags").value = "";
  dialog.addEventListener("close", async () => {
    if (dialog.returnValue !== "ok") return;
    const title = $("new-title").value.trim();
    if (!title) return;
    try {
      const created = await api("/api/notes", {
        method: "POST",
        body: {
          title,
          folder: $("new-folder").value.trim(),
          tags: $("new-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
        },
      });
      await loadTree();
      openNote(created.id);
      toast("Created");
    } catch (error) { toast(error.message, "error"); }
  }, { once: true });
  dialog.showModal();
  $("new-title").focus();
}

function wireTabs() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".tab")) {
        other.classList.toggle("is-active", other === tab);
        if (other === tab) other.setAttribute("aria-current", "page");
        else other.removeAttribute("aria-current");
      }
      for (const name of ["notes", "ingest", "agent", "publish"]) {
        $(`tab-${name}`).hidden = name !== tab.dataset.tab;
      }
    });
  }
}

/* Push the URL's shareable state into the controls. Extracted because boot()
   and onPopState() must agree: a Back that restored the note but left a stale
   query in the search box would show a URL, a result list and an input that
   disagree about what the user searched for. Two copies of this loop is how one
   of them silently loses a filter — the same reason FILTERS is declared once. */
function seedControls() {
  $("q").value = state.q;
  for (const [id, key] of FILTERS) {
    $(id).value = state.filters[key];
  }
}

/* Back / Forward. The URL is the source of truth for shareable state, so the
   handler re-reads it and rebuilds from that rather than keeping a history
   stack of its own — one authority, and the browser already owns it.
   `openNote` calls syncUrl(), which finds the URL it would write is the one
   already showing and does nothing, so restoring a note cannot push a new
   entry and trap the user going backwards. */
function onPopState() {
  readUrl();
  seedControls();
  /* Clear first. The outgoing note must not sit on screen while the incoming
     one is fetched — that is the "Back appeared to do nothing" bug, and with a
     slow request it is a long, convincing one. */
  dispatch({ note: null });
  if (state.selectedId) openNote(state.selectedId);
  /* UNCONDITIONAL, and the missing `if (state.q)` was a real bug caught in
     review. runSearch() already clears the ledger and returns to `idle` when
     the query is empty (results.js:128), so calling it covers Back to a state
     with no query too. Guarding it here left the PREVIOUS result list on screen
     under an empty search box and an id-less URL — a ledger describing a search
     the address bar no longer says was made, which is worse than a stale note
     because nothing about it looks wrong. Clearing it here instead would put a
     second copy of results.js's emptying rule in this file. */
  runSearch();
}

function wireControls() {
  $("q").addEventListener("input", (event) => {
    state.q = event.target.value; syncUrl(); scheduleSearch();
  });
  for (const [id, key] of FILTERS) {
    $(id).addEventListener("change", (event) => {
      state.filters[key] = event.target.value; syncUrl(); runSearch();
    });
  }
  /* One listener on the container, which survives every renderTree — the tree's
     children are replaced wholesale, the container never is. */
  $("tree").addEventListener("keydown", onTreeKeydown);
  $("new-note").addEventListener("click", newNote);
  $("rail-toggle").addEventListener("click", () => {
    document.querySelector(".rail").classList.toggle("is-open");
  });
  window.addEventListener("popstate", onPopState);
  $("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("brain-ui-theme", next); } catch (e) { /* ignore */ }
  });
}

async function boot() {
  readUrl();
  /* wireIngestedToggle() is ORDER-FREE, unlike wireMarginalia() below, and the
     asymmetry is the point rather than an inconsistency: it INJECTS a control
     into the static .rail-head and subscribes to nothing, so no renderer wipes
     it. Its filtering and its counts already reach the app through renderTree —
     only the control was missing, which is why the feature was fully working
     and completely unreachable. No ordering pin is added for it: pinning an
     order that is not real is a change-detector, the shape the guard file
     exists to refuse. */
  wireTabs(); wireKeys(); wireControls(); wirePalette(); wireIngestedToggle();
  /* wireMarginalia() IS LAST ON THIS LINE AND MAY NOT MOVE LEFT OF
     subscribe(renderInspector). store.js's dispatch() runs listeners in
     REGISTRATION order, and renderInspector opens with `host.textContent = ""`
     — so a marginalia subscriber registered first is drawn and then wiped on
     the same dispatch, every dispatch. The failure is SILENT: no error, no
     console warning, the page boots normally, the block simply never appears.
     The browser suite barely sees it — measured, relocating this call leaves
     tests/test_ui_browser_reading.py at 2 failed / 11 passed, and NEITHER
     failure is one of the marginalia's own T13 tests (they each call
     wireMarginalia() from page.evaluate, and the backlinks rail re-creates the
     element asynchronously outside the dispatch that wiped it). So the position
     is pinned by
     check_the_marginalia_is_wired_after_the_inspector, whose
     `marginalia-wired-after-the-inspector` entry relocates this call to the
     head of the line to prove the pin can fail. It is on the SAME line as the
     subscription it must follow so that the constraint is literal rather than
     separated from its reason by this comment. */
  subscribe(renderTree); subscribe(renderResults); subscribe(renderInspector); wireMarginalia();
  wireThread();

  seedControls();

  try {
    const health = await api("/api/health");
    dispatch({ health });
    if (health.read_only) {
      $("mode-badge").hidden = false;
      /* Hide every write affordance, not just the inspector's. The middleware
         refuses the request either way, but offering a "+ New" that can only
         produce a 403 is a UI that lies about what it can do. */
      $("new-note").hidden = true;
    }
    for (const notice of health.notices || []) toast(notice);
  } catch (e) { toast("Cannot reach the brain server.", "error"); }

  await Promise.all([loadTree(), loadFacets()]);
  if (state.q) runSearch();
  if (state.selectedId) openNote(state.selectedId);
  dispatch({});
}

boot();
