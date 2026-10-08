/* The local graph: the open note and the notes it links with, as a small SVG.
 *
 * SAME SHAPE AS marginalia.js, and every contract in that module's header
 * applies here verbatim — read it first. In short:
 *
 * APPENDED LAST to `#inspector`. `.inspector` is `grid-template-rows: auto 1fr`;
 * `.note-head` must keep `auto` and `.note-body`/`.editor` must keep `1fr`.
 * Any child appended after them lands on an implicit `auto` row, which is the
 * only safe place for a new block.
 *
 * SUBSCRIBER ORDER IS A WIRING CONTRACT. `renderInspector` wipes `#inspector`
 * (`host.textContent = ""`) on every dispatch, so this renderer must be
 * registered AFTER it. main.js calls `wireGraph()` after `wireMarginalia()`
 * (on its own line, after `wireThread()` — see the comment in boot()), which
 * also makes the figure the LAST child after any dispatch that rebuilds the
 * inspector: inspector, then marginalia, then this.
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
import { $, el } from "/static/js/dom.js";
import { state, subscribe } from "/static/js/store.js";

/* An XML namespace is an identifier, not a fetch: nothing is requested. */
const SVG_NS = "http://www.w3.org/2000/svg";

/* Labels longer than this are cut and given an ellipsis; the full title stays
   available in the node's <title> child, which is what hover shows. */
const LABEL_MAX_CHARS = 28;

/* Fallback canvas size, used only if the payload omits one. */
const DEFAULT_SIZE = 320;

/* Gap between a node's circle and its label, in viewBox units. */
const LABEL_GAP = 10;

let wired = false;

/* noteId -> the graph payload already fetched for it, or `null`.
 *
 * Same reasoning as marginalia.js's backlinkCache: renderGraph runs on EVERY
 * dispatch, so without a cache each toggle of the editor would re-request the
 * graph. A FAILED fetch caches `null` deliberately — retrying on every later
 * dispatch turns one failing endpoint into a request storm. A transient failure
 * therefore stays blank until the page is reloaded, which is the right trade
 * for a block that is supplementary to the note.
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
  if (block) host.appendChild(block);
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
  figure.setAttribute("aria-label", "Links around this note");

  if (isolated) {
    figure.appendChild(
      el("p", "graph-empty", "No links in this vault yet — run brain vault sync."),
    );
    return figure;
  }

  figure.appendChild(buildSvg(graph));
  if (graph.truncated > 0) {
    figure.appendChild(el("figcaption", null, `+${graph.truncated} more in the links rail`));
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
  const svg = svgEl("svg", {
    viewBox: `0 0 ${graph.width} ${graph.height}`,
    role: "img",
  });
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));

  /* Edges FIRST, so every node is painted over the lines that meet it. */
  for (const edge of graph.edges) {
    const a = byId.get(edge.src);
    const b = byId.get(edge.dst);
    if (!a || !b) continue;
    svg.appendChild(svgEl("line", {
      class: "edge",
      "data-kind": edge.kind === "derived" ? "derived" : "wiki",
      "data-src": edge.src,
      "data-dst": edge.dst,
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
    }));
  }

  /* The root is nodes[0] by contract. It is not a link: it is the note the
     reader already has open. */
  const [root, ...neighbours] = graph.nodes;
  const rootGroup = svgEl("g", { class: "node node-root", "data-note-id": root.id });
  appendGlyph(rootGroup, root);
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
  const on = () => setHover(svg, link, node.id, true);
  const off = () => setHover(svg, link, node.id, false);
  link.addEventListener("mouseenter", on);
  link.addEventListener("focus", on);
  link.addEventListener("mouseleave", off);
  link.addEventListener("blur", off);
  return link;
}

function appendGlyph(parent, node) {
  const r = positive(node.r, 6);
  const title = String(node.title ?? "");
  /* <title> first: it is the accessible name of the group/link and the tooltip
     hover shows, carrying the FULL title the label below may have cut. */
  const tip = svgEl("title");
  tip.textContent = title;
  parent.appendChild(tip);
  parent.appendChild(svgEl("circle", { cx: node.x, cy: node.y, r }));
  const label = svgEl("text", { x: node.x, y: node.y + r + LABEL_GAP, "text-anchor": "middle" });
  label.textContent = shorten(title);
  parent.appendChild(label);
}

function shorten(title) {
  return title.length > LABEL_MAX_CHARS ? `${title.slice(0, LABEL_MAX_CHARS)}…` : title;
}

/* Highlight the node and every edge that touches it. Attribute comparison in
   JS rather than a CSS attribute selector built from the id, so an id can never
   be interpreted as selector syntax. */
function setHover(svg, nodeEl, id, on) {
  const targets = [nodeEl];
  for (const line of svg.querySelectorAll("line.edge")) {
    if (line.getAttribute("data-src") === id || line.getAttribute("data-dst") === id) {
      targets.push(line);
    }
  }
  for (const target of targets) {
    if (on) target.setAttribute("data-hover", "");
    else target.removeAttribute("data-hover");
  }
}

/* Imported lazily, at click time, to keep this module out of the
   inspector <-> tree import cycle that store.js's header documents — the same
   pattern, for the same reason, as marginalia.js. */
async function openNoteById(id) {
  const inspector = await import("/static/js/inspector.js");
  inspector.openNote(id);
}
