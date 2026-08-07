/* brain ui — the whole front end.
 *
 * No framework, no bundler, no CDN. Plain ES modules, loaded directly, so the
 * wheel stays pure-Python and the app works fully offline. Nothing here is
 * fetched from the network.
 *
 * State lives in one plain object with a subscriber list; each render function
 * updates only its own subtree with targeted DOM operations. No virtual DOM, no
 * full re-render.
 *
 * URL is the source of truth for shareable state (q, filters, id), written with
 * history.replaceState. Ephemeral state — editor mode, tree expansion, theme —
 * lives in localStorage, never the URL.
 */

/* ------------------------------------------------------------------ state -- */

const state = {
  q: "", filters: { source: "", type: "", tag: "", after: "", before: "" },
  results: [], meta: null, searchStatus: "idle",
  selectedId: null, note: null, editing: false,
  saveStatus: "saved", draftBody: "",
  tree: null, expanded: loadExpanded(), health: null, sessionId: null,
};

const listeners = [];
function subscribe(fn) { listeners.push(fn); }
function dispatch(patch) { Object.assign(state, patch); listeners.forEach((fn) => fn()); }

function loadExpanded() {
  try { return new Set(JSON.parse(localStorage.getItem("brain-ui-expanded") || "[]")); }
  catch (e) { return new Set(); }
}
function saveExpanded() {
  try { localStorage.setItem("brain-ui-expanded", JSON.stringify([...state.expanded])); }
  catch (e) { /* private mode — expansion state is not worth failing over */ }
}

/* -------------------------------------------------------------------- api -- */

/* The token arrives in the URL FRAGMENT, which is never sent to the server,
   never written to an access log, and never leaked in a Referer. We read it,
   hold it in memory, and strip it from the address bar immediately. */
let token = "";
if (location.hash.startsWith("#t=")) {
  token = decodeURIComponent(location.hash.slice(3));
  history.replaceState(null, "", location.pathname + location.search);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers["X-Brain-UI-Token"] = token;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  let payload = null;
  try { payload = await response.json(); } catch (e) { payload = null; }
  if (!response.ok) {
    const err = new Error((payload && payload.error && payload.error.message) || response.statusText);
    err.code = (payload && payload.error && payload.error.code) || "http_error";
    err.status = response.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

/* ------------------------------------------------------------------- dom --- */

const $ = (id) => document.getElementById(id);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;   // textContent, never innerHTML
  return node;
};

function toast(message, kind) {
  const node = $("toast");
  node.textContent = message;
  node.dataset.kind = kind || "info";
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, 3600);
}

/* ------------------------------------------------------------------- url --- */

let urlTimer = null;
function syncUrl() {
  clearTimeout(urlTimer);
  urlTimer = setTimeout(() => {
    const params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    for (const [key, value] of Object.entries(state.filters)) if (value) params.set(key, value);
    if (state.selectedId) params.set("id", state.selectedId);
    const query = params.toString();
    history.replaceState(null, "", query ? `?${query}` : location.pathname);
  }, 200);
}

function readUrl() {
  const params = new URLSearchParams(location.search);
  state.q = params.get("q") || "";
  for (const key of Object.keys(state.filters)) state.filters[key] = params.get(key) || "";
  state.selectedId = params.get("id");
}

/* ------------------------------------------------------------------ tree --- */

function renderTree() {
  const host = $("tree");
  host.textContent = "";
  if (!state.tree) return;
  if (state.tree.count === 0) {
    host.appendChild(el("p", "empty", state.tree.empty_hint || "Nothing here yet."));
    return;
  }
  host.appendChild(buildBranch(state.tree, 1));
}

function buildBranch(node, level) {
  const group = el("ul", "tree-group");
  group.setAttribute("role", "group");

  for (const child of node.children) {
    const item = el("li", "tree-folder");
    item.setAttribute("role", "treeitem");
    item.setAttribute("aria-level", String(level));
    const open = state.expanded.has(child.path);
    item.setAttribute("aria-expanded", String(open));

    const label = el("div", "tree-label");
    label.appendChild(el("span", "twisty", open ? "▾" : "▸"));
    label.appendChild(el("span", null, child.name));
    label.tabIndex = -1;
    label.addEventListener("click", () => {
      if (state.expanded.has(child.path)) state.expanded.delete(child.path);
      else state.expanded.add(child.path);
      saveExpanded();
      renderTree();
    });
    item.appendChild(label);
    if (open) item.appendChild(buildBranch(child, level + 1));
    group.appendChild(item);
  }

  for (const note of node.notes) {
    const item = el("li", "tree-note");
    item.setAttribute("role", "treeitem");
    item.setAttribute("aria-level", String(level));
    if (note.id === state.selectedId) item.classList.add("is-selected");

    /* A real anchor, so middle-click and Cmd-click open a new tab. */
    const link = el("a", "tree-label");
    link.href = `?id=${encodeURIComponent(note.id)}`;
    link.tabIndex = -1;
    if (note.draft) link.appendChild(el("span", "dot-draft", "●"));
    link.appendChild(el("span", null, note.title));
    link.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.button !== 0) return;
      event.preventDefault();
      openNote(note.id);
    });
    item.appendChild(link);
    group.appendChild(item);
  }
  return group;
}

/* --------------------------------------------------------------- results --- */

function renderResults() {
  const host = $("results");
  host.textContent = "";

  if (state.searchStatus === "loading") { $("meta").textContent = "searching…"; return; }
  if (state.searchStatus === "error") {
    $("meta").textContent = "";
    host.appendChild(el("li", "error-state", state.searchError || "search failed"));
    return;
  }
  if (state.searchStatus === "idle") { $("meta").textContent = ""; $("meta-sub").textContent = ""; return; }

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
    host.appendChild(el("li", "empty", "No notes matched. Try fewer filters."));
    return;
  }

  for (const result of state.results) {
    const item = document.createElement("li");
    const row = el("a", "result");
    row.href = `?id=${encodeURIComponent(result.id)}`;
    if (result.id === state.selectedId) row.classList.add("is-selected");

    const gutter = el("div", "gutter");
    gutter.appendChild(el("div", null, result.source_kind || "—"));
    gutter.appendChild(el("div", null, result.id.slice(0, 8)));
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
}

/* ------------------------------------------------------------------ search -- */

let searchTimer = null;
let inFlight = null;

function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 180);
}

async function runSearch() {
  if (!state.q.trim()) { dispatch({ results: [], meta: null, searchStatus: "idle" }); return; }

  /* Every in-flight request is cancelled by the next keystroke, so a slow
     query can never overwrite a newer result. With a multi-second embed on a
     real corpus this is load-bearing, not a nicety. */
  if (inFlight) inFlight.abort();
  inFlight = new AbortController();

  const params = new URLSearchParams({ q: state.q });
  for (const [key, value] of Object.entries(state.filters)) if (value) params.set(key, value);

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

/* --------------------------------------------------------------- inspector -- */

async function openNote(id) {
  dispatch({ selectedId: id, editing: false, saveStatus: "saved" });
  syncUrl();
  document.body.dataset.view = "note";
  try {
    const query = state.sessionId ? `?session_id=${encodeURIComponent(state.sessionId)}` : "";
    const note = await api(`/api/notes/${encodeURIComponent(id)}${query}`);
    dispatch({ note, draftBody: note.body });
  } catch (error) {
    dispatch({ note: null });
    toast(error.message, "error");
  }
}

function renderInspector() {
  const host = $("inspector");
  host.textContent = "";
  const note = state.note;
  if (!note) {
    host.appendChild(el("p", "empty", "Select a note from the vault, or search."));
    return;
  }

  /* Below 780px the ledger and inspector are a two-view stack, so opening a
     note replaces the result list entirely. Without this the only way back is
     the browser's back button — a dead end on a touch device. CSS shows it
     only at that breakpoint; every shortcut needs a visible equivalent. */
  const back = el("button", "ghost-btn back-btn", "← Results");
  back.type = "button";
  back.addEventListener("click", () => { document.body.dataset.view = "list"; });
  host.appendChild(back);

  host.appendChild(el("h1", "note-title", note.title));

  const bar = el("div", "note-bar");
  const readOnly = state.health && state.health.read_only;

  if (!readOnly && note.editable && !note.withheld) {
    const edit = el("button", "ghost-btn", state.editing ? "Preview" : "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => dispatch({ editing: !state.editing }));
    bar.appendChild(edit);

    if (state.editing) {
      const save = el("button", "accent-btn", "Save");
      save.type = "button";
      save.addEventListener("click", saveNote);
      bar.appendChild(save);
    }
  }

  const status = el("span", "save-state", {
    saved: "● Saved", dirty: "● Unsaved", saving: "Saving…",
    error: "● Error", conflict: "● Conflict",
  }[state.saveStatus]);
  status.dataset.state = state.saveStatus;
  bar.appendChild(status);

  if (!readOnly) {
    const draft = el("button", "ghost-btn", note.draft ? "○ Draft" : "Published");
    draft.type = "button";
    draft.addEventListener("click", () => toggleDraft(!note.draft));
    bar.appendChild(draft);

    if (note.movable) {
      const move = el("button", "ghost-btn", "⋯ Move");
      move.type = "button";
      move.addEventListener("click", moveNote);
      bar.appendChild(move);
    }

    const del = el("button", "ghost-btn", "⌫ Delete");
    del.type = "button";
    del.addEventListener("click", deleteNote);
    bar.appendChild(del);
  }
  host.appendChild(bar);

  const fm = el("div", "frontmatter");
  fm.appendChild(el("div", null, note.vault_path || "(not exported to the vault)"));
  fm.appendChild(el("div", null,
    `type ${note.content_type} · ${note.tier} · ${note.source_kind || "—"}` +
    (note.sensitivity && note.sensitivity !== "normal" ? ` · ${note.sensitivity}` : "")));
  if (note.tags.length) fm.appendChild(el("div", null, note.tags.map((t) => `[${t}]`).join(" ")));
  host.appendChild(fm);

  if (note.withheld) {
    /* `withheld` is present ONLY when the body was withheld — same key and
       same message vocabulary as MCP brain_show, so a client handles one
       spelling across both surfaces. */
    host.appendChild(el("p", "withheld", note.withheld));
    return;
  }

  if (state.editing) {
    const area = el("textarea", "editor");
    area.value = state.draftBody;
    area.setAttribute("aria-label", "Note source");
    area.addEventListener("input", () => {
      state.draftBody = area.value;
      if (state.saveStatus !== "dirty") dispatch({ saveStatus: "dirty" });
    });
    host.appendChild(area);
    area.focus();
    return;
  }

  const body = el("div", "note-body");
  /* The ONLY innerHTML in this file. The value is server-rendered by
     brain.ui.render, which parses with html=False (raw tags are escaped, not
     passed through) and strips any href scheme outside http/https/mailto. The
     default-src 'none' CSP is the second layer behind that. */
  body.innerHTML = note.html;
  host.appendChild(body);
}

async function saveNote() {
  const note = state.note;
  dispatch({ saveStatus: "saving" });
  try {
    const result = await api(`/api/notes/${encodeURIComponent(note.id)}`, {
      method: "PUT",
      body: { body_hash: note.body_hash, body: state.draftBody },
    });
    note.body = state.draftBody;
    note.body_hash = result.body_hash;
    note.html = result.html;
    dispatch({ saveStatus: "saved", editing: false });
    toast("Saved");
  } catch (error) {
    if (error.code === "stale_write") {
      dispatch({ saveStatus: "conflict" });
      toast("This note changed on disk. Reopen it to see the current version.", "error");
      return;
    }
    dispatch({ saveStatus: "error" });
    toast(error.message, "error");
  }
}

async function toggleDraft(next) {
  try {
    await api(`/api/notes/${encodeURIComponent(state.note.id)}/draft`, {
      method: "POST", body: { draft: next },
    });
    state.note.draft = next;
    dispatch({});
    loadTree();
    toast(next ? "Marked as draft" : "Published");
  } catch (error) { toast(error.message, "error"); }
}

async function moveNote() {
  const folder = prompt("Move to folder (vault-relative, blank for root):", "");
  if (folder === null) return;
  try {
    await api(`/api/notes/${encodeURIComponent(state.note.id)}/move`, {
      method: "POST", body: { confirm: true, new_folder: folder },
    });
    toast("Moved");
    await openNote(state.note.id);
    loadTree();
  } catch (error) { toast(error.message, "error"); }
}

function deleteNote() {
  const note = state.note;
  const dialog = $("confirm-dialog");
  $("confirm-text").textContent =
    `This permanently deletes "${note.title}", its chunks, and its vault file.`;
  $("confirm-label").textContent = "Type the exact title to confirm";
  const input = $("confirm-input");
  const ok = $("confirm-ok");
  input.value = "";
  ok.disabled = true;

  /* The confirm button stays disabled until the typed title matches. The
     server checks it again — this is the convenience layer, not the control. */
  const onInput = () => { ok.disabled = input.value !== note.title; };
  input.addEventListener("input", onInput);

  dialog.addEventListener("close", async () => {
    input.removeEventListener("input", onInput);
    if (dialog.returnValue !== "ok") return;
    try {
      await api(`/api/notes/${encodeURIComponent(note.id)}`, {
        method: "DELETE",
        body: { confirm: true, expected_title: input.value },
      });
      dispatch({ note: null, selectedId: null });
      loadTree();
      toast("Deleted");
    } catch (error) { toast(error.message, "error"); }
  }, { once: true });

  dialog.showModal();
  input.focus();
}

/* ------------------------------------------------------------------ boot --- */

async function loadTree() {
  try { dispatch({ tree: await api("/api/tree") }); }
  catch (error) { toast(error.message, "error"); }
}

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

function wireKeys() {
  document.addEventListener("keydown", (event) => {
    const typing = event.target.closest("input, textarea, [contenteditable]");

    if ((event.metaKey || event.ctrlKey) && event.key === "k") {
      event.preventDefault(); $("q").focus(); $("q").select(); return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key === "s") {
      event.preventDefault(); if (state.editing) saveNote(); return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key === "e") {
      event.preventDefault();
      if (state.note && state.note.editable) dispatch({ editing: !state.editing });
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key === "b") {
      event.preventDefault(); $("tree").closest(".rail").classList.toggle("is-open"); return;
    }
    if (typing) return;                       /* single-key bindings only outside inputs */
    if (event.key === "/") { event.preventDefault(); $("q").focus(); }
    if (event.key === "Escape") { document.activeElement.blur(); }
  });
}

function wireControls() {
  $("q").addEventListener("input", (event) => {
    state.q = event.target.value; syncUrl(); scheduleSearch();
  });
  for (const [id, key] of [["f-source", "source"], ["f-type", "type"],
                           ["f-tag", "tag"], ["f-after", "after"], ["f-before", "before"]]) {
    $(id).addEventListener("change", (event) => {
      state.filters[key] = event.target.value; syncUrl(); runSearch();
    });
  }
  $("new-note").addEventListener("click", newNote);
  $("rail-toggle").addEventListener("click", () => {
    document.querySelector(".rail").classList.toggle("is-open");
  });
  $("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("brain-ui-theme", next); } catch (e) { /* ignore */ }
  });
}

async function boot() {
  readUrl();
  wireTabs(); wireKeys(); wireControls();
  subscribe(renderTree); subscribe(renderResults); subscribe(renderInspector);

  $("q").value = state.q;
  for (const [id, key] of [["f-source", "source"], ["f-type", "type"],
                           ["f-tag", "tag"], ["f-after", "after"], ["f-before", "before"]]) {
    $(id).value = state.filters[key];
  }

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
