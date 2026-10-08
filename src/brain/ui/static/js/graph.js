/* The local graph: the open note and the notes it links with, as a small SVG.
 *
 * SAME SHAPE AS marginalia.js, and every contract in that module's header
 * applies here verbatim — read it first. In short:
 *
 * PLACED AFTER THE NOTE in `#inspector`. `.inspector` is `grid-template-rows:
 * auto 1fr`; `.note-head` must keep `auto` and `.note-body`/`.editor` must keep
 * `1fr`. Any child after them lands on an implicit `auto` row, which is the
 * only safe place for a new block. The figure goes in through
 * `placeInspectorBlock` (dom.js), which holds the ONE order of the blocks —
 * marginalia, this graph, the related rail — whatever order their fetches
 * resolve in. It is no longer "appended last": the related rail follows it.
 *
 * SUBSCRIBER ORDER IS A WIRING CONTRACT. `renderInspector` wipes `#inspector`
 * (`host.textContent = ""`) on every dispatch, so this renderer must be
 * registered AFTER it. Where the figure lands no longer depends on the order
 * the three block renderers are registered in — `placeInspectorBlock` decides.
 *
 * GEOMETRY IS THE SERVER'S. `GET /api/notes/{id}/graph` returns every node
 * already placed (root at the centre, neighbours on a ring). Nothing here
 * computes a layout; this module only draws what it is given.
 *
 * NO innerHTML, NO insertAdjacentHTML, NO outerHTML, NO DOMParser. The SVG is
 * built node by node with `createElementNS` + `setAttribute`, and every title
 * reaches the DOM through `textContent`. Titles are document names authored by
 * whoever wrote the note, and a title is never markup.
 */

import { api } from "/static/js/api.js";
import { $, el, placeInspectorBlock } from "/static/js/dom.js";
import { state, subscribe } from "/static/js/store.js";

/* An XML namespace is an identifier, not a fetch: nothing is requested. */
const SVG_NS = "http://www.w3.org/2000/svg";

/* Labels longer than this are cut and given an ellipsis; the full title stays
   available in the node's <title> child, which is what hover shows. */
const LABEL_MAX_CHARS = 28;

/* Fallback canvas size, used only if the payload omits one. */
const DEFAULT_SIZE = 320;

/* Above this many neighbours, neighbour labels are NOT drawn at rest: each
   one shows only while its node is hovered or focused (graph.css,
   `svg[data-crowded]`). Presentation, not layout — the server still places
   every node; the full title stays in each node's <title>, which is its
   accessible name, so nothing is lost to a screen reader.
   MEASURED on the server's ring (graph_layout.RING_RADIUS = 110 on 320,
   28-char titles, label font 13.5 units) at 320, 400 and 1280px viewports:
   every pair of label boxes is disjoint for 1, 2, 3 and 4 neighbours; at 5
   the 2 and 10 o'clock labels hang at the root label's height and meet it
   (and at 8 and over, neighbours meet each other — at the 24 cap they are
   ~29 units apart and each label is ~150 wide). 6 happens to be clean, but a
   threshold must hold for EVERY count beneath it, so it is 4.
   Pinned by test_every_ring_up_to_the_threshold_is_fully_labelled_and_legible
   and test_a_crowded_ring_labels_only_the_neighbour_in_hand, which reads
   this constant from this file. */
const MAX_LABELLED_NEIGHBOURS = 4;

/* What a node with an empty title is called, on its label AND in its <title>:
   a link with no text has no accessible name. */
const UNTITLED = "Untitled";

/* The accessible name of the graph. The figure and the svg both carry it, so
   no role in this block is ever unnamed. */
const GRAPH_LABEL = "Links around this note";

/* Gap between a node's circle and its label, in viewBox units. A label below
   its node is placed by its BASELINE, so the gap clears the cap height. */
const LABEL_GAP = 10;

/* The ROOT's label sits ABOVE the root circle, its baseline this far over the
   circle's top — enough to clear descenders. Where a label sits relative to
   its OWN node is presentation, not layout: the server still places every
   node. Below the root it shared a row with the server ring's 3 and 9
   o'clock neighbours (whose labels hang below them at the root's height),
   and collided with both; above, it shares nothing — the 12 o'clock label
   hangs BELOW its node, ~60 units higher. Pinned by
   test_no_two_labels_overlap_on_the_server_ring. */
const ROOT_LABEL_GAP = 6;

let wired = false;

/* noteKey(id) -> the graph payload already fetched for that note, or `null`.
 *
 * Same reasoning as marginalia.js's backlinkCache: renderGraph runs on EVERY
 * dispatch, so without a cache each toggle of the editor would re-request the
 * graph. A FAILED fetch caches `null` deliberately — retrying on every later
 * dispatch turns one failing endpoint into a request storm. A transient failure
 * therefore stays blank until the page is reloaded or the note is saved, which
 * is the right trade for a block that is supplementary to the note.
 *
 * Keyed on `noteKey(id)` (store.js), so a save of the note drops its entry and
 * no other note's — see marginalia.js's backlinkCache.
 */
const graphCache = new Map();
const inFlight = new Set();

/* Idempotent, like wireMarginalia(): a second call must not register a second
   subscriber drawing a second figure over the first. */
export function wireGraph() {
  if (wired) return;
  wired = true;
  subscribe(renderGraph);
  renderGraph();
}

export function renderGraph() {
  const host = $("inspector");
  if (!host) return;

  /* Remove our own previous block BEFORE deciding whether to draw. Not every
     dispatch rebuilds the inspector, and a graph centred on the previous note
     is worse than none. */
  const stale = host.querySelector(".local-graph");
  if (stale) stale.remove();

  const note = state.note;
  /* Editing: the editor owns the 1fr track, and a graph beside raw markdown
     describes a rendering the reader is not looking at. Withheld: nothing about
     a withheld note is drawn, including who it links with. */
  if (!note || state.editing || note.withheld) return;

  const cached = graphCache.get(note.id);
  if (cached === undefined) {
    /* NOT awaited. The note is already painted; the graph is a second request
       that must never stand between the reader and the note. */
    fetchGraph(note);
    return;
  }

  const block = buildBlock(cached);
  if (block) placeInspectorBlock(host, block);
}

/* SILENT ON FAILURE — no toast, no placeholder. `api()` throws on a non-2xx and
   does not toast; this caller declines to, for the same reason the backlinks
   rail does: the reader never asked for this request. */
function fetchGraph(note) {
  if (inFlight.has(note.id)) return;
  inFlight.add(note.id);

  api(`/api/notes/${encodeURIComponent(note.id)}/graph`).then(
    (payload) => {
      inFlight.delete(note.id);
      graphCache.set(note.id, normalise(payload));
      /* The stale-response guard: by the time this resolves the reader may have
         opened another note, and a slow response for A must not paint under B. */
      if (state.note && state.note.id === note.id) renderGraph();
    },
    () => {
      inFlight.delete(note.id);
      graphCache.set(note.id, null);
    },
  );
}

/* The payload, validated at the boundary. Anything that is not a drawable graph
   becomes `null`, which draws nothing. */
function normalise(payload) {
  if (!payload || !Array.isArray(payload.nodes) || !payload.nodes.length) return null;
  const nodes = payload.nodes.filter(
    (n) => n && typeof n.id === "string" && Number.isFinite(n.x) && Number.isFinite(n.y),
  );
  if (!nodes.length) return null;
  return {
    width: positive(payload.width, DEFAULT_SIZE),
    height: positive(payload.height, DEFAULT_SIZE),
    nodes,
    edges: Array.isArray(payload.edges) ? payload.edges.filter(Boolean) : [],
    truncated: positive(payload.truncated, 0),
    corpusLinked: payload.corpus_linked !== false,
  };
}

function positive(value, fallback) {
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

/* Ruling R5, the degraded states:
 *  - one node and a vault with NO links at all: say so, and name the command
 *    that builds them;
 *  - one node in a linked vault: draw NOTHING. An isolated note is not an
 *    error, and an empty canvas that looks like a bug is forbidden. */
function buildBlock(graph) {
  if (!graph) return null;

  const isolated = graph.nodes.length === 1;
  if (isolated && graph.corpusLinked) return null;

  const figure = el("figure", "local-graph");
  figure.setAttribute("aria-label", GRAPH_LABEL);

  if (isolated) {
    figure.appendChild(
      el("p", "graph-empty", "No links in this vault yet — run brain vault sync."),
    );
    return figure;
  }

  figure.appendChild(buildSvg(graph));
  /* "not shown", not "in the links rail": the only rail is the marginalia's
     backlinks ("Linked from"), and a truncated neighbour can be outgoing-only,
     so pointing at the rail would send the reader somewhere it is not. */
  if (graph.truncated > 0) {
    figure.appendChild(el("figcaption", null, `+${graph.truncated} more not shown`));
  }
  return figure;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    node.setAttribute(name, String(value));
  }
  return node;
}

function buildSvg(graph) {
  /* role="group", NOT "img": an img's descendants are presentational, so the
     neighbour links inside it would vanish from the accessibility tree, and an
     unnamed img is itself a defect. A named group keeps every link reachable. */
  const svg = svgEl("svg", {
    viewBox: `0 0 ${graph.width} ${graph.height}`,
    role: "group",
    "aria-label": GRAPH_LABEL,
  });
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));

  /* Edges FIRST, so every node is painted over the lines that meet it. */
  for (const edge of graph.edges) {
    const a = byId.get(edge.src);
    const b = byId.get(edge.dst);
    if (!a || !b) continue;
    svg.appendChild(svgEl("line", {
      class: "edge",
      /* The server's kind, unchanged: `wiki`, `embed` or `derived`. Only
         `derived` is styled differently (dashed), and that is graph.css's call. */
      "data-kind": String(edge.kind ?? ""),
      "data-src": edge.src,
      "data-dst": edge.dst,
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
    }));
  }

  /* The root is nodes[0] by contract. It is not a link: it is the note the
     reader already has open. */
  const [root, ...neighbours] = graph.nodes;
  if (neighbours.length > MAX_LABELLED_NEIGHBOURS) svg.setAttribute("data-crowded", "");
  const rootGroup = svgEl("g", { class: "node node-root", "data-note-id": root.id });
  /* Same as a neighbour, so an ingested root is styled like an ingested node. */
  if (root.kind) rootGroup.setAttribute("data-kind", String(root.kind));
  appendGlyph(rootGroup, root, { above: true });
  svg.appendChild(rootGroup);

  for (const node of neighbours) svg.appendChild(buildNeighbour(svg, node));
  return svg;
}

function buildNeighbour(svg, node) {
  /* The same href the backlinks rail uses, so a middle-click or a copied link
     opens the same note a plain click does. */
  const link = svgEl("a", {
    class: "node",
    href: `?id=${encodeURIComponent(node.id)}`,
    "data-note-id": node.id,
  });
  if (node.kind) link.setAttribute("data-kind", String(node.kind));
  appendGlyph(link, node);

  link.addEventListener("click", (event) => {
    event.preventDefault();
    openNoteById(node.id);
  });
  /* Pointer and keyboard share ONE highlight. Starting either replaces whatever
     is lit; ending either falls back to the focused node, if there is one, so a
     mouse leaving a node never darkens the node the keyboard is still on. */
  const on = () => showHover(svg, link);
  const off = () => endHover(svg);
  link.addEventListener("mouseenter", on);
  link.addEventListener("focus", on);
  link.addEventListener("mouseleave", off);
  link.addEventListener("blur", off);
  return link;
}

function appendGlyph(parent, node, { above = false } = {}) {
  const r = positive(node.r, 6);
  const title = String(node.title ?? "").trim() || UNTITLED;
  /* <title> first: it is the accessible name of the group/link and the tooltip
     hover shows, carrying the FULL title the label below may have cut. */
  const tip = svgEl("title");
  tip.textContent = title;
  parent.appendChild(tip);
  parent.appendChild(svgEl("circle", { cx: node.x, cy: node.y, r }));
  const labelY = above ? node.y - r - ROOT_LABEL_GAP : node.y + r + LABEL_GAP;
  const label = svgEl("text", { x: node.x, y: labelY, "text-anchor": "middle" });
  label.textContent = shorten(title);
  parent.appendChild(label);
}

function shorten(title) {
  return title.length > LABEL_MAX_CHARS ? `${title.slice(0, LABEL_MAX_CHARS)}…` : title;
}

/* Light the node and every edge that touches it, after clearing whatever was
   lit before. Attribute comparison in JS rather than a CSS attribute selector
   built from the id, so an id can never be interpreted as selector syntax. */
function showHover(svg, nodeEl) {
  clearHover(svg);
  const id = nodeEl.getAttribute("data-note-id");
  nodeEl.setAttribute("data-hover", "");
  for (const line of svg.querySelectorAll("line.edge")) {
    if (line.getAttribute("data-src") === id || line.getAttribute("data-dst") === id) {
      line.setAttribute("data-hover", "");
    }
  }
}

function clearHover(svg) {
  for (const lit of svg.querySelectorAll("[data-hover]")) lit.removeAttribute("data-hover");
}

/* During `blur` the active element is already <body>, so a blur clears; a
   mouseleave while a node in THIS graph has focus hands the highlight back. */
function endHover(svg) {
  const focused = document.activeElement;
  if (focused && focused.matches("a.node") && svg.contains(focused)) {
    showHover(svg, focused);
  } else {
    clearHover(svg);
  }
}

/* Imported lazily, at click time, to keep this module out of the
   inspector <-> tree import cycle that store.js's header documents — the same
   pattern, for the same reason, as marginalia.js. */
async function openNoteById(id) {
  const inspector = await import("/static/js/inspector.js");
  inspector.openNote(id);
}
