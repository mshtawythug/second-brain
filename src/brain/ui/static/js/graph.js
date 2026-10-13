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

import { $, el, placeInspectorBlock } from "/static/js/dom.js";
import { perNoteFetch } from "/static/js/note_fetch.js";
import {
  isConfidentialNote, servesConfidential, state, subscribe,
} from "/static/js/store.js";

/* An XML namespace is an identifier, not a fetch: nothing is requested. */
const SVG_NS = "http://www.w3.org/2000/svg";

/* Labels longer than this are cut and given an ellipsis; the full title stays
   available in the node's <title> child, which is what hover shows. A label
   can be cut SHORTER than this to fit its slot — see fitLabels. CHARACTERS
   are grapheme clusters, what a reader counts as one character: an emoji ZWJ
   sequence or an astral letter is one, and a cut never splits it (cut). For
   ASCII titles that is the same count as code units. */
const LABEL_MAX_CHARS = 28;

/* Clearance, in viewBox units, that fitLabels keeps between a label and the
   inspector's edge and between two labels sharing a row. It covers the
   3-unit halo graph.css strokes round every glyph, with room to spare.
   Pinned against that halo — read from the label's computed stroke-width,
   not from here — at both edges by test_the_edge_clearance_covers_the_label_halo
   and between two labels sharing a row by
   test_the_row_clearance_covers_the_label_halo; the layout tests' oracle
   holds every cut to the specified 4, not to whatever this file says. */
const LABEL_CLEARANCE = 4;

/* Fallback canvas size, used only if the payload omits one. */
const DEFAULT_SIZE = 320;

/* Above this many neighbours, neighbour labels are NOT drawn at rest: each
   one shows only while its node is hovered or focused (graph.css,
   `svg[data-crowded]`). Presentation, not layout — the server still places
   every node; the full title stays in each node's <title>, which is its
   accessible name, so nothing is lost to a screen reader.
   WHAT THE THRESHOLD DOES AND DOES NOT DO. It does NOT keep labels apart:
   fitLabels does, at every count, by cutting each label to its measured
   slot — so no font can make labels meet, short of a slot narrower than
   "…" (the bare ellipsis is fitText's floor), which the server's ring never
   produces: the narrowest shared-row slot it makes is ~186 units (the
   ~190-unit pair below, less LABEL_CLEARANCE), and on a crowded ring no
   two labels show together at all. The floor is still pinned, on a slot a
   test places below it — on the inspector's edge — by
   test_a_slot_narrower_than_the_ellipsis_leaves_the_bare_ellipsis, which
   requires the bare "…" there. The threshold decides when that
   slot is too narrow to be worth reading. On the server's ring
   (graph_layout.RING_RADIUS = 110 on 320, label font 13.5 units) labels
   share a row only in pairs at up to 4 neighbours (at 3, the 4 and 8 o'clock
   pair, ~190 units apart; at 4, the 3 and 9 o'clock pair, 220 apart). At 5
   the 2 and 10 o'clock labels hang at the ROOT label's height, ~105 units
   either side of it, so three labels share ~210 units and each is cut to
   about half a 28-char title; more neighbours only narrow the slots. A
   threshold must hold for EVERY count beneath it, so it is 4.
   (First measured 2026-10-08 against macOS font metrics ALONE, as "every
   pair disjoint" with no fit — which a wider default sans, DejaVu Sans on
   the GitHub Linux runner, falsified at 3 neighbours.)
   Pinned at 4 by test_a_ring_is_crowded_from_one_past_the_specified_threshold
   (a ring of 4 shows every label at rest, a ring of 5 only the root's), run
   at the platform's default font only; and by
   test_every_ring_up_to_the_threshold_is_fully_labelled_and_legible and
   test_a_crowded_ring_labels_only_the_neighbour_in_hand, those two each run
   at the platform's default font AND at a deliberately wide one. All three
   hold the SPEC's 4 (tests/ui_graph_geometry.py, _SPEC_LABEL_THRESHOLD) and
   never read this constant, so changing it cannot move them with it. */
const MAX_LABELLED_NEIGHBOURS = 4;

/* What stands in the graph's place when this server refuses it. One line of
   text, no markup — it names no neighbour, only that the block is hidden. */
const GRAPH_REFUSED_NOTICE = "The graph is hidden for confidential notes on this server.";

/* What a node with an empty title is called, on its label AND in its <title>:
   a link with no text has no accessible name. */
const UNTITLED = "Untitled";

/* The accessible name of the graph. The figure and the svg both carry it, so
   no role in this block is ever unnamed. */
const GRAPH_LABEL = "Links around this note";

/* Gap, in viewBox units, between a NEIGHBOUR's circle and the TOP of its
   label's box. It is held by measurement, not by the baseline: fitLabels hangs
   each label so that its box (getBBox) starts exactly this far below the
   circle. A baseline offset cannot do this, because the box's ascent belongs
   to the device scale and the title, not to this file. Chromium rounds it to
   whole DEVICE PIXELS, so in viewBox units it is not whole and moves with the
   width: 13.23 / 13.46 / 13.32 units at 320 / 400 / 1280px, the same at the
   platform face and at Verdana, and 15.87 / 15.53 / 15.54 for any title that
   draws an emoji, whose fallback face is taller (measured 2026-10-13 in the
   drawn graph). The old baseline gap of 10 left a plain label's box 3.2 to
   3.5 units INSIDE its circle, and an emoji label's 5.5 to 5.9.
   4 so that no label's PAINTED extent, its ink plus the 3-unit halo graph.css
   strokes round it (1.5 beyond the ink), reaches the circle's widest stroke,
   the focus ring (graph.css, 3, so 1.5 outside `r`). Ink can reach the very
   TOP of its own box but not past it. A glyph taller than the rounded ascent
   grows the box: a label with Å in Verdana has a box that rises 10.205px
   above its baseline, where a plain label's rises 10px, at 320px. Measured on accented and stacked-diacritic
   labels (Å É Ö À, Ǻ Ấ Ǖ Ḗ Ṍ) at both faces and three widths, ink never rose
   over its box. So the painted label reaches 0 + 1.5 above the box, and the
   ring 1.5 below it at gap 0. At gap 3 the two touch, and 4 is the smallest
   whole value that clears them (by 1.0, measured 2026-10-13; plan §8.6).
   Pinned by test_a_neighbour_label_hangs_its_gap_below_its_own_circle, which
   checks the exact gap AND the ink-plus-halo clearance on an accented ring. */
const LABEL_GAP = 4;

/* The ROOT's label sits ABOVE the root circle, its baseline this far over the
   circle's top — enough to clear descenders. Where a label sits relative to
   its OWN node is presentation, not layout: the server still places every
   node. Below the root it shared a row with the server ring's 3 and 9
   o'clock neighbours (whose labels hang below them at the root's height),
   and collided with both; above, it shares nothing — the 12 o'clock label
   hangs BELOW its node, its box ending 42 to 53px above the root label's at
   the three tested widths (graph.css has the measured table). Pinned by
   test_the_root_label_sits_above_the_root_on_a_row_of_its_own. NOT by
   test_no_two_labels_overlap_on_the_server_ring: with the root's label
   below, fitLabels cuts it and the 3 and 9 o'clock labels until none
   overlaps, so that test stays green. */
const ROOT_LABEL_GAP = 6;

let wired = false;

/* Refits the drawn graph's labels whenever its svg OR the inspector changes
   size: a label's slot is partly the inspector's edge in viewBox units, which
   moves when either box does. BOTH, because neither box tracks the other for
   certain: the svg is capped at 24rem (graph.css), so on a wide desktop the
   inspector can resize while the svg's border box does not. Today the svg's
   content box still moves (its gutter is a percentage of the figure), but a
   fixed gutter would end that. Each is pinned by its own test. One callback per
   delivery refits the one drawn svg ONCE, however many of the two boxes
   moved; fitting edits only text inside the svg, which resizes neither box,
   so it cannot loop. One graph is drawn at a time, so `fitted` is the svg
   being watched, and null whenever nothing is: it is cleared with every
   disconnect, so a replaced or withdrawn svg is not kept alive by this
   module (pinned by test_the_refit_observer_watches_only_the_drawn_graph,
   which collects it). The callback never sees the null: disconnect() also
   drops any delivery not yet made. ResizeObserver is ASSUMED, not
   feature-tested, like the unguarded `??` in inspector.js and related.js:
   every browser that can parse these modules has it. */
let fitted = null;
const refit = new ResizeObserver(() => fitLabels(fitted));

/* The graph payload for a note, through the shared per-note fetch
 * (note_fetch.js — read its header for the cache, the revision key, the
 * failure sentinel and the stale-response guard). A FAILED fetch caches
 * `null`, which draws nothing, as does a payload `normalise` rejects. */
const fetchGraph = perNoteFetch({
  endpoint: "graph",
  normalise: (payload) => normalise(payload),
  failed: null,
  rerender: () => renderGraph(),
});

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
  refit.disconnect();
  fitted = null;

  const note = state.note;
  /* Editing: the editor owns the 1fr track, and a graph beside raw markdown
     describes a rendering the reader is not looking at. Withheld: nothing about
     a withheld note is drawn, including who it links with. */
  if (!note || state.editing || note.withheld) return;

  /* A confidential note this server will not graph: say so, and do NOT ask.
     The request could only come back 403 `graph_withheld`, leaving a blank
     block and a console error where the reader deserves a reason. */
  if (graphRefusedHere(note)) {
    placeInspectorBlock(host, buildRefusedNotice());
    return;
  }

  /* `undefined` means the request is in flight (NOT awaited — the note is
     already painted, and the graph must never stand between the reader and
     it); this renders again when it lands. */
  const cached = fetchGraph(note);
  if (cached === undefined) return;

  const block = buildBlock(cached);
  if (!block) return;
  placeInspectorBlock(host, block);
  /* Fitted NOW, synchronously, not only from the observer: the observer's
     first callback lands after this dispatch, and nothing may read (or
     paint) a label that has not been fitted. */
  const svg = block.querySelector("svg");
  if (!svg) return;
  fitted = svg;
  fitLabels(svg);
  refit.observe(svg);
  refit.observe(host);
}

/* MIRRORS routes_graph.note_graph's gate EXACTLY, and the two change together:
   the route refuses the graph of a confidential root unless the session serves
   confidential TITLES (`strict = not ctx.serve_confidential_titles`). The
   bodies gate is not part of it — a confidential note whose body is served is
   still refused its graph while titles are not. This is the ONE place the
   client decides it. */
function graphRefusedHere(note) {
  return isConfidentialNote(note) && !servesConfidential("serve_confidential_titles");
}

/* The block element itself, so the notice keeps the graph's slot in the
   canonical order (placeInspectorBlock) and is removed with it on the next
   render. Same shape as the "no links yet" notice in buildBlock. */
function buildRefusedNotice() {
  const figure = el("figure", "local-graph");
  figure.setAttribute("aria-label", GRAPH_LABEL);
  figure.appendChild(el("p", "graph-withheld", GRAPH_REFUSED_NOTICE));
  return figure;
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
  /* A neighbour's `y` here is only a starting baseline: fitLabels hangs the
     label by its measured box before anything paints it (hangBelowCircle). */
  const labelY = above ? node.y - r - ROOT_LABEL_GAP : node.y + r + LABEL_GAP;
  const label = svgEl("text", { x: node.x, y: labelY, "text-anchor": "middle" });
  label.textContent = shorten(title);
  parent.appendChild(label);
}

function shorten(title) {
  return cut(title, LABEL_MAX_CHARS);
}

/* Splits a title into grapheme clusters. Feature-tested, unlike
   ResizeObserver: graph.css's :has() admits Firefox 121-124, which lacks
   Intl.Segmenter, and a constructor that throws at module load would take
   the whole graph with it. Without it a title splits into CODE POINTS. That
   still never leaves half a surrogate pair, but it can split an emoji ZWJ
   sequence at a joint. Both paths are pinned by
   test_a_label_is_cut_at_the_cap_by_whole_characters. */
const GRAPHEMES = typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
  : null;

function characters(title) {
  return GRAPHEMES
    ? Array.from(GRAPHEMES.segment(title), (part) => part.segment)
    : Array.from(title);
}

/* The first `chars` characters of `title`, with an ellipsis if any were cut.
   Whitespace before the ellipsis is dropped: "Planning …" reads as two
   words, not one cut short. So a cut that ends on a space shows the same
   label as the cut one shorter, and fitText's search still finds the longest
   distinct one. */
function cut(title, chars) {
  const all = characters(title);
  return all.length > chars ? `${all.slice(0, chars).join("").trimEnd()}…` : title;
}

/* LABEL WIDTH IS MEASURED, NOT ASSUMED. A label's rendered width is a
 * property of whichever font the reader's machine resolves --font-ui to, and
 * those differ by more than the ring has to spare: a 28-char title that fits
 * between two nodes in SF Pro does not in DejaVu Sans. So every label gets a
 * SLOT, in viewBox units, from geometry alone —
 *   - centred on its node, it must stay inside the inspector, whose
 *     `overflow-y: auto` would clip it mid-word: twice the distance from the
 *     node to the nearer edge;
 *   - on a ring where labels show together (not data-crowded), it must not
 *     meet a label whose row it shares: the distance between the two nodes,
 *     which gives each label at most half the gap on its side;
 * and is cut, character by character, until its MEASURED length fits. Its
 * full title stays in the node's <title>. On a crowded ring no two labels
 * show together (graph.css): the root's alone at rest, one neighbour's alone
 * in hand — so only the inspector's edge binds there.
 * Idempotent: each pass starts again from the full label. */
function fitLabels(svg) {
  /* No box (a hidden inspector — the phone list view sets it display:none —
     or an svg already detached from it): nothing to measure, so leave the
     labels alone. Tested on the svg's BOX, not its CTM: Chromium returns a
     non-null identity getScreenCTM() for a display:none svg, and fitting to
     the all-zero inspector rect would make every slot negative and every
     label a bare "…". The observer refits when the box returns.
     The ONE guard. An svg with a box is rendered, so it is still inside the
     #inspector it was placed in (nothing moves it), that inspector has a box
     too, and the svg's CTM is non-null; no stylesheet scales or mirrors the
     svg or an ancestor, so the CTM's `a` is its positive scale. */
  if (!svg.getBoundingClientRect().width) return;
  const host = svg.closest("#inspector");
  const ctm = svg.getScreenCTM();
  const edge = host.getBoundingClientRect().left + host.clientLeft;
  const left = (edge - ctm.e) / ctm.a + LABEL_CLEARANCE;
  const right = (edge + host.clientWidth - ctm.e) / ctm.a - LABEL_CLEARANCE;
  const crowded = svg.hasAttribute("data-crowded");

  const labels = [...svg.querySelectorAll(".node > text")].map((text) => {
    const title = text.parentNode.querySelector("title").textContent;
    text.textContent = shorten(title);
    if (!text.parentNode.classList.contains("node-root")) hangBelowCircle(text);
    const box = text.getBBox();
    return {
      text, title, x: Number(text.getAttribute("x")), top: box.y, bottom: box.y + box.height,
    };
  });
  for (const label of labels) {
    let slot = 2 * Math.min(label.x - left, right - label.x);
    if (!crowded) {
      for (const other of labels) {
        const apart = other.bottom + LABEL_CLEARANCE <= label.top
          || label.bottom + LABEL_CLEARANCE <= other.top;
        if (other !== label && !apart) {
          slot = Math.min(slot, Math.abs(label.x - other.x) - LABEL_CLEARANCE);
        }
      }
    }
    fitText(label.text, label.title, slot);
  }
}

/* Move a neighbour's label so that the top of its box sits LABEL_GAP below
   its circle. The box is measured at the CAPPED title, the same box rows are
   judged on. Every cut fitText may then make is no taller: a cut draws a
   subset of the same glyphs plus "…", which the primary face draws inside its
   ascent (measured: appending "…" moves no box top, at both faces and three
   widths, for plain, accented, emoji and CJK titles), and the primary face's
   ascent counts whether or not it draws a glyph (measured with all-Hebrew,
   all-Devanagari and all-CJK titles). So the label as shown clears the
   circle at least as well.
   Idempotent: the ascent is the baseline less the box's top, whatever `y` is. */
function hangBelowCircle(text) {
  const circle = text.parentNode.querySelector("circle");
  const below = Number(circle.getAttribute("cy")) + Number(circle.getAttribute("r"));
  const ascent = Number(text.getAttribute("y")) - text.getBBox().y;
  text.setAttribute("y", String(below + LABEL_GAP + ascent));
}

/* Cut `text` to the longest prefix of `title` (at most LABEL_MAX_CHARS) whose
   measured length fits `slot`. Binary search: lengths only grow with the
   prefix. A slot too narrow for any prefix leaves the bare ellipsis — the
   floor, which itself overruns a slot narrower than "…". */
function fitText(text, title, slot) {
  if (text.getComputedTextLength() <= slot) return;
  let lo = 0;
  let hi = Math.min(characters(title).length, LABEL_MAX_CHARS) - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    text.textContent = cut(title, mid);
    if (text.getComputedTextLength() <= slot) lo = mid;
    else hi = mid - 1;
  }
  text.textContent = cut(title, lo);
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
