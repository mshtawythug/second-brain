/* The ledger's idle state: what you captured recently, and what you tag.
 *
 * NEW DESIGN, not a port. The wiki had a home page; `brain ui` has a ledger
 * that is BLANK until you type — `renderResults` returns early on
 * `searchStatus === "idle"` after clearing the host, so the widest column of
 * the app shows nothing at all to a reader who has not yet asked a question.
 * These two surfaces are that column's answer to "what do I have?".
 *
 * WHY THE LEDGER AND NOT THE RAIL. The left rail is the vault TREE, with a
 * roving tabindex and its own keyboard model; a tag list bolted underneath it
 * would either join that model or sit beside it as a second, differently-driven
 * widget. More decisively: a tag's COUNT and the documents that count refers to
 * have to be readable together, because they do not always agree (see
 * `tagScopeNote`). Putting the index in the ledger keeps the number and its
 * click-through in one column, which is the only place a reader can notice the
 * difference — and noticing it is the point.
 *
 * WIRED FROM index.html, NOT FROM main.js's boot(). `js/main.js` is owned by
 * another task for the whole of this phase, so this module is loaded by its own
 * <script type="module"> and mounts itself at evaluation time. `wireDiscovery`
 * is exported and IDEMPOTENT for the same reason `wirePalette` is: the browser
 * suite calls it directly, and a second call must be a no-op rather than a
 * second subscriber drawing a second copy.
 *
 * ES modules are keyed by URL, so the `store.js` this imports is the SAME
 * module instance main.js imports — one `state`, one listener list, two entry
 * points. Registration ORDER does not matter here the way it does for the
 * marginalia: this draws into `#discovery`, which `renderInspector` never
 * touches.
 *
 * NO CLIENT-SIDE DRAFT FILTER, DELIBERATELY. The route payload is
 * `{id, title, vault_path, source_kind, date}` — it carries no `draft` flag, so
 * a client-side draft guard is not merely redundant, it is unimplementable.
 * Drafts and the People-Hub namespace are excluded by `_DISCOVERABLE` in
 * ui/queries.py and asserted against the real route payload by
 * `test_recent_hides_drafts_and_the_people_hub` and
 * `test_tag_page_hides_drafts_and_the_people_hub`. A filter here would be inert
 * code that reads as load-bearing — the defect this codebase keeps deleting.
 */

import { api } from "/static/js/api.js";
import { $, el } from "/static/js/dom.js";
import { state, subscribe } from "/static/js/store.js";
import { openNote } from "/static/js/inspector.js";

/* Ported from results.js's table rather than re-invented, and deliberately not
   imported from it: results.js does not export it, and widening another task's
   module surface to save five lines is not a trade this phase should make. The
   vocabulary itself is `quartz/util/sourceIcons.ts`, which four overlay
   components already share. */
const SOURCE_ICONS = {
  krisp: "🎙️", slack: "💬", gmail: "📧", manual: "✍️", vault: "🌱",
};
const sourceIconFor = (kind) => SOURCE_ICONS[kind] || SOURCE_ICONS.vault;

let wired = false;
let recent = null;
let tagIndex = null;
let tagView = null;
let loadError = null;

/* The count a tag carries in the index, or null when the index has not loaded.
   Looked up rather than threaded through the click handler so the tag view can
   state both numbers even when it is reached before the index finishes. */
function indexedCountFor(tag) {
  if (!tagIndex) return null;
  const bucket = tagIndex.find((entry) => entry.value === tag);
  return bucket ? bucket.count : null;
}

/* THE HONEST LINE — and it no longer names a cause, deliberately.
 *
 * `/api/tags` now serves `browseable_tag_counts`, scoped to the same
 * `_DISCOVERABLE` predicates as the tag page, so the count and its
 * click-through describe the SAME corpus. The old divergence — drafts,
 * generated pages, sensitivity — is gone at the source rather than explained
 * away here.
 *
 * WHAT REMAINS is the server's page limit (`routes_discovery.TAG_PAGE_LIMIT`),
 * so a shortfall today has exactly one cause. This line still does not SAY so,
 * and that is the same restraint as before rather than a leftover: the payload
 * does not carry the cap, so "capped at 50" would be a guess rendered as a
 * fact — and it would silently become a WRONG guess the moment another
 * predicate diverges. Stating both numbers is true under every scope this
 * surface might later be given.
 *
 * The earlier version of this string named "drafts and generated pages", which
 * was already incomplete when the sensitivity predicate landed: a confidential
 * document tagged `hr` made it read "4 tagged · 3 shown — browsing hides drafts
 * and generated pages", which was false about that document. An attribution has
 * to be re-audited every time the scope moves; a bare statement of both numbers
 * does not.
 */
function tagScopeNote(tag, shown) {
  const indexed = indexedCountFor(tag);
  if (indexed == null || shown >= indexed) return null;
  return `${indexed} tagged · ${shown} shown on this page.`;
}

function documentRow(doc) {
  const item = document.createElement("li");
  const row = el("a", "disc-row");
  /* Same href shape as the ledger's result rows, so a middle-click or
     Cmd-click opens a real, shareable URL instead of being swallowed. */
  row.href = `?id=${encodeURIComponent(doc.id)}`;
  row.title = doc.id;

  const icon = el("span", "disc-icon", sourceIconFor(doc.source_kind));
  /* aria-hidden for the same reason as the ledger's gutter: "studio
     microphone" announced beside the word "krisp" is duplication. */
  icon.setAttribute("aria-hidden", "true");
  row.appendChild(icon);

  const main = el("div", "disc-main");
  main.appendChild(el("div", "disc-title", doc.title));
  main.appendChild(el("div", "disc-meta", doc.date || "—"));
  row.appendChild(main);

  row.addEventListener("click", (event) => {
    if (event.metaKey || event.ctrlKey || event.button !== 0) return;
    event.preventDefault();
    openNote(doc.id);
  });
  item.appendChild(row);
  return item;
}

function renderRecent(host) {
  const section = el("section", "disc-panel");
  section.appendChild(el("h2", "disc-head", "Recently captured"));
  if (!recent) {
    section.appendChild(el("p", "disc-empty", "Loading…"));
  } else if (recent.length === 0) {
    section.appendChild(el("p", "disc-empty", "Nothing captured yet."));
  } else {
    const list = el("ol", "disc-list");
    for (const doc of recent) list.appendChild(documentRow(doc));
    section.appendChild(list);
  }
  host.appendChild(section);
}

function renderTagIndex(host) {
  const section = el("section", "disc-panel");
  section.appendChild(el("h2", "disc-head", "Tags"));
  if (!tagIndex) {
    section.appendChild(el("p", "disc-empty", "Loading…"));
    host.appendChild(section);
    return;
  }
  if (tagIndex.length === 0) {
    section.appendChild(el("p", "disc-empty", "No tags yet."));
    host.appendChild(section);
    return;
  }
  /* #31 offered two ways to stop a count from lying about its own
     click-through: derive it from a browse-consistent query, or label it. This
     used to say "counts cover the whole corpus, including drafts and generated
     pages that browsing hides" — the LABEL option, taken because the query did
     not exist and was not this task's to add.
     `browseable_tag_counts` now exists, so the counts below ARE the rows a
     click produces, and that sentence would be false. The label is now a
     statement of scope rather than an apology for it. */
  section.appendChild(el("p", "disc-note",
    "Counts are the documents you can browse to."));
  const list = el("ul", "disc-tags");
  for (const bucket of tagIndex) {
    const item = document.createElement("li");
    const button = el("button", "disc-tag");
    button.type = "button";
    button.dataset.tag = bucket.value;
    button.appendChild(el("span", "disc-tag-name", bucket.value));
    button.appendChild(el("span", "disc-tag-count", String(bucket.count)));
    button.addEventListener("click", () => { openTag(bucket.value); });
    item.appendChild(button);
    list.appendChild(item);
  }
  section.appendChild(list);
  host.appendChild(section);
}

function renderTagView(host) {
  const section = el("section", "disc-panel");
  const back = el("button", "disc-back", "← All tags");
  back.type = "button";
  back.addEventListener("click", () => { tagView = null; render(); });
  section.appendChild(back);
  section.appendChild(el("h2", "disc-head", `#${tagView.tag}`));

  const note = tagScopeNote(tagView.tag, tagView.documents.length);
  if (note) section.appendChild(el("p", "disc-note", note));

  if (tagView.documents.length === 0) {
    section.appendChild(el("p", "disc-empty", "No browseable documents carry this tag."));
  } else {
    const list = el("ol", "disc-list");
    for (const doc of tagView.documents) list.appendChild(documentRow(doc));
    section.appendChild(list);
  }
  host.appendChild(section);
}

export function render() {
  const host = $("discovery");
  if (!host) return;
  host.textContent = "";

  /* The ledger has ONE occupant at a time. Anything other than `idle` means
     results.js is drawing into #results directly below, and two lists of
     documents in one column is not a richer page, it is an ambiguous one. */
  if (state.searchStatus !== "idle") { host.hidden = true; return; }
  host.hidden = false;

  if (loadError) {
    host.appendChild(el("p", "disc-empty", loadError));
    return;
  }
  if (tagView) { renderTagView(host); return; }
  renderRecent(host);
  renderTagIndex(host);
}

async function openTag(tag) {
  try {
    const payload = await api(`/api/tags/${encodeURIComponent(tag)}`);
    /* The response's OWN `tag` is used, not the clicked label: the route
       canonicalizes, so echoing the click would title the page with a spelling
       the rows were never matched on. */
    tagView = { tag: payload.tag, documents: payload.documents };
  } catch (error) {
    tagView = { tag, documents: [] };
    loadError = error.message;
  }
  render();
}

async function load() {
  try {
    const [recentPayload, tagPayload] = await Promise.all([
      api("/api/recent"), api("/api/tags"),
    ]);
    recent = recentPayload.documents;
    tagIndex = tagPayload.tags;
  } catch (error) {
    /* A dead discovery rail must not take the search box with it. The ledger
       still works; this column says so and stops. */
    loadError = "Could not load the recent rail.";
  }
  render();
}

export function wireDiscovery() {
  if (wired) return;
  wired = true;
  subscribe(render);
  render();
  load();
}

wireDiscovery();
