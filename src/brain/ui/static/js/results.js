/* The ledger: the result list, the latency line, and the debounced search. */

import { api } from "/static/js/api.js";
import { $, el } from "/static/js/dom.js";
import { dispatch, state } from "/static/js/store.js";
import { openNote } from "/static/js/inspector.js";
import { ceilingNote, emptyLedgerMessage } from "/static/js/ledger_status.js";
import { pagerModel } from "/static/js/pager.js";

/* The source vocabulary, PORTED from `quartz/util/sourceIcons.ts` rather than
   invented here. Four overlay components already share that table (Search,
   TagContent, RelatedDocs, CommandPalette); a krisp row that looks like a
   microphone in the wiki and like something else in the app is the kind of
   divergence a consolidation is supposed to remove, not create.
   Adding a source is one line — and, per the overlay's own note, necessary but
   not sufficient: a source-specific chip style would need the same entry. */
const SOURCE_ICONS = {
  krisp: "🎙️", slack: "💬", gmail: "📧", manual: "✍️", vault: "🌱",
};

/* `vault` is the fallback, matching `sourceIconFor`. It is a real glyph rather
   than a blank because the gutter is a fixed-width column: an empty cell on the
   rows whose source is unrecognised ragged the whole list, and "unknown source"
   is information the reader can act on only if it is drawn. */
const sourceIconFor = (kind) => SOURCE_ICONS[kind] || SOURCE_ICONS.vault;

export function renderResults() {
  const host = $("results");
  host.textContent = "";

  /* THE PAGER IS LEFT ALONE WHILE LOADING, and that is not an oversight.
     Clicking Next dispatches `loading` before the request resolves; hiding or
     disabling the buttons on that render would destroy the focus of the very
     click that started it, on every page turn. The list below it is cleared
     either way, so a live pager over a "searching…" ledger is momentary. */
  if (state.searchStatus === "loading") { $("meta").textContent = "searching…"; return; }
  if (state.searchStatus === "error") {
    $("meta").textContent = "";
    host.appendChild(el("li", "error-state", state.searchError || "search failed"));
    /* No payload means no page to be on. Offering Next beside an error would
       advertise navigation through results that were never returned. */
    renderPager(null);
    return;
  }
  if (state.searchStatus === "idle") {
    $("meta").textContent = ""; $("meta-sub").textContent = "";
    renderPager(null);
    return;
  }

  const meta = state.meta || {};
  const timing = meta.timing_ms || {};
  const total = meta.total_documents != null ? meta.total_documents : state.results.length;
  const seconds = timing.total != null ? (timing.total / 1000).toFixed(1) : "?";
  $("meta").textContent = `${total} notes · ${seconds} s`;

  /* The honest phase split. A search on a real corpus is dominated by the
     embedding round-trip, so showing only a total would imply a speed the
     product does not have. In FTS-only mode the embed phase disappears. */
  const parts = [];
  if (timing.embed != null) parts.push(`embed ${(timing.embed / 1000).toFixed(1)}`);
  if (timing.sql != null) parts.push(`rank ${(timing.sql / 1000).toFixed(1)}`);
  if (meta.fts_only) parts.push("fts-only");
  $("meta-sub").textContent = parts.join(" · ");

  if (state.results.length === 0) {
    /* WHICH empty page this is (#27). Both ranking legs cap their candidate
       pools independently of the caller's limit, so a page can be empty because
       the ranker stopped looking rather than because nothing matched — and the
       hardcoded sentence that used to live here told the second reader to go
       and broaden a query that was already over-matching. The server says which
       ending happened; `ledger_status.js` only picks the words. */
    host.appendChild(el("li", "empty", emptyLedgerMessage(meta)));
    return;
  }

  for (const result of state.results) {
    const item = document.createElement("li");
    const row = el("a", "result");
    row.href = `?id=${encodeURIComponent(result.id)}`;
    /* aria-current marks the open row for assistive tech; the class only makes
       it visible. Set on the selected row ONLY — there is deliberately no
       `removeAttribute` else-branch, because renderResults cleared the host
       above and this <a> was constructed one line ago, so removal would be a
       no-op on an element that never had the attribute. (An earlier version had
       one, justified by a comparison to wireTabs() that does not hold:
       wireTabs iterates PERSISTENT tab buttons, where clearing the previous
       tab's attribute is genuinely load-bearing.) */
    if (result.id === state.selectedId) {
      row.classList.add("is-selected");
      row.setAttribute("aria-current", "true");
    }
    /* The id is still one hover away — but it is not what belongs in the widest
       column of every row. Eight hex characters told the reader nothing; the
       date tells them which of four similarly-titled notes this is. */
    row.title = result.id;

    const gutter = el("div", "gutter");
    /* ADDITIVE — the glyph goes beside the word, never instead of it. An
       icon-only gutter reads as decoration to anyone who does not know the
       vocabulary, and reads as nothing at all to a screen reader, which is why
       the glyph is aria-hidden: "studio microphone" announced next to the word
       "krisp" is duplication, not information. */
    const icon = el("span", "source-icon", sourceIconFor(result.source_kind));
    icon.setAttribute("aria-hidden", "true");
    const source = el("div", "source-cell");
    source.appendChild(icon);
    source.appendChild(el("span", null, result.source_kind || "—"));
    gutter.appendChild(source);
    /* "—" matches the gutter's existing empty spelling one line above. It is
       defensive only: /api/search never serves a dateless row, because
       documents.ingested_at is NOT NULL so the coalesce always resolves. The
       reachable null is a graph-shaped SearchResult, which this route does not
       return. */
    gutter.appendChild(el("div", null, result.date || "—"));
    row.appendChild(gutter);

    const main = el("div");
    main.appendChild(el("div", "result-title", result.title));
    main.appendChild(el("div", "result-snippet",
      result.withheld ? "— snippet withheld (confidential) —" : (result.snippet || "")));
    if (result.tags && result.tags.length) {
      const chips = el("div", "chips");
      for (const tag of result.tags.slice(0, 4)) chips.appendChild(el("span", "chip", tag));
      main.appendChild(chips);
    }
    row.appendChild(main);

    row.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.button !== 0) return;
      event.preventDefault();
      openNote(result.id);
    });
    item.appendChild(row);
    host.appendChild(item);
  }

  /* The ceiling is also reachable on a PARTIALLY filled last page — 87 ranked,
     a page starting at 75, twelve rows and no more to come. That page is not
     empty, so the branch above never sees it, and without this line it would
     read as an ordinary end of list. Empty string when there is nothing to
     say, so an ordinary result set gains no row. */
  const note = ceilingNote(meta);
  if (note) host.appendChild(el("li", "ledger-note", note));

  renderPager(meta);
}


/* ------------------------------------------------------------- paging --- */

/* The control's STATE only — `js/pager.js` makes every decision and
   `static/index.html` owns the elements. Nothing here is created or destroyed,
   which is what lets focus survive a page turn (see the markup's comment). */
function renderPager(meta) {
  const model = pagerModel(meta);
  const prev = $("pager-prev");
  const next = $("pager-next");
  const focused = document.activeElement;

  $("pager").hidden = !model.visible;
  prev.disabled = !model.canPrev;
  next.disabled = !model.canNext;
  /* The range and the reason, in that order, with the separator dropped when
     either is absent — `join` over a filtered pair rather than a template with
     a dangling "·" on the first page. */
  $("pager-status").textContent =
    [model.rangeLabel, model.boundary].filter(Boolean).join(" · ");

  /* A disabled button is not focusable, so paging to the last page would take
     the keyboard user's focus with it and drop them at <body> — the top of the
     document, several tab stops from where they were working. Hand focus to
     the sibling that is still live; when neither is, the pager is hidden
     anyway and there is nothing here to hold it. */
  if (focused === prev && prev.disabled && !next.disabled) next.focus();
  if (focused === next && next.disabled && !prev.disabled) prev.focus();
}

/* Wired once, from boot(). The elements are static, so unlike the result rows
   these listeners are attached a single time and survive every render. */
export function wirePager() {
  $("pager-prev").addEventListener("click", () => goToPage(pagerModel(state.meta).prevOffset));
  $("pager-next").addEventListener("click", () => goToPage(pagerModel(state.meta).nextOffset));
}

/* The ONLY thing that moves the ledger off page one. The model is recomputed
   from `state.meta` at CLICK time rather than closed over at render time, so a
   handler can never act on the page before last. */
function goToPage(offset) {
  state.offset = offset;
  runSearch();
}

let searchTimer = null;
let inFlight = null;

export function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 180);
}

/* The query+filters of the last search that was actually issued. Used ONLY to
   decide whether `state.offset` still refers to the ranking it was chosen in;
   see runSearch. */
let lastSearchKey = null;

export async function runSearch() {
  if (!state.q.trim()) {
    state.offset = 0;
    dispatch({ results: [], meta: null, searchStatus: "idle" });
    return;
  }

  /* Every in-flight request is cancelled by the next keystroke, so a slow
     query can never overwrite a newer result. With a multi-second embed on a
     real corpus this is load-bearing, not a nicety. */
  if (inFlight) inFlight.abort();
  inFlight = new AbortController();

  const params = new URLSearchParams({ q: state.q });
  for (const [key, value] of Object.entries(state.filters)) if (value) params.set(key, value);

  /* PAGE ONE WHENEVER THE SEARCH ITSELF CHANGED — derived from what actually
     changed, never announced by the caller. An offset is a position inside one
     ranking; carry it into a different query and the reader lands on page 4 of
     something they have not seen page 1 of, or on an empty page for a query
     with three matches.

     The alternative was `state.offset = 0` in the query handler and in each
     filter handler in main.js. That is the hand-maintained roster again: the
     next control that narrows a search is added by someone who has not read
     this comment, and it silently keeps the stale offset. store.js makes the
     same argument for deriving `isNavigation` from the URL diff rather than
     from a flag passed by the caller.

     Compared BEFORE the offset is appended, so paging — which changes nothing
     but the offset — leaves the key equal and the offset intact. */
  const searchKey = params.toString();
  if (searchKey !== lastSearchKey) {
    state.offset = 0;
    lastSearchKey = searchKey;
  }
  /* Omitted at zero rather than sent as `offset=0`. `_parse_offset` reads an
     absent offset as `DEFAULT_OFFSET`, so the two are identical to the server,
     and keeping a first-page request byte-identical to the one the ledger sent
     before paging existed means this change cannot perturb a cache, a log line
     or a telemetry row for any search that never pages. */
  if (state.offset > 0) params.set("offset", String(state.offset));

  dispatch({ searchStatus: "loading" });
  try {
    const payload = await api(`/api/search?${params}`, { signal: inFlight.signal });
    dispatch({
      results: payload.results, meta: payload, searchStatus: "ready",
      sessionId: payload.session_id,
    });
  } catch (error) {
    if (error.name === "AbortError") return;
    state.searchError = error.message;
    dispatch({ searchStatus: "error" });
  }
}
