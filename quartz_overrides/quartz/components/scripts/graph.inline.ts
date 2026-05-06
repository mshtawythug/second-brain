// Custom Quartz v4 graph renderer for brain vaults.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/scripts/graph.inline.ts` by
// `brain vault render --overlay`, FULL-REPLACING upstream's stock
// `graph.inline.ts`. It does NOT compile or run from the brain
// repo itself; the imports below resolve against the dependencies
// Quartz pulls into the cloned workspace via `npm install`, not
// against any package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). When upstream churns,
// pull the latest renderer from
// https://github.com/jackyzha0/quartz/blob/v4/quartz/components/scripts/graph.inline.ts
// and re-apply the brain tweaks below. Brain modifications are
// flagged with `// brain:` for value/structural choices on
// upstream-supported logic, and `// brain-extension:` for behavior
// that doesn't exist in stock Quartz. To enumerate every delta:
//
//   grep -n "brain:" graph.inline.ts
//   grep -n "brain-extension:" graph.inline.ts
//
// Strategy — full replacement (Option A): the brain modifications
// touch the heart of `renderGraph` (filter pass, color resolution,
// recency sizing, edge-style branch, derived-edge tooltip), and
// the upstream uses module-level state plus an unconditional
// `document.addEventListener("nav", ...)` that runs on import — so
// a wrapping overlay can't compose cleanly without dueling
// listeners. We vendor faithfully and patch in place. Upstream's
// function names, comment phrasing, and structural ordering are
// preserved verbatim wherever the brain delta isn't touching them,
// to keep `diff -u <upstream> <ours>` a useful upgrade tool.
//
// Type-narrowing note: every property access on a
// `BrainContentDetails` value (other than the named `tier` /
// `source` / `linkRecords` fields) reads through an index
// signature of `unknown`. Reads of `title`, `tags`, `links`, and
// `date` therefore narrow with explicit casts at the call site —
// see the inline `as` annotations below.

import type { BrainContentDetails, BrainLinkRecord } from "../../plugins/emitters/contentIndex"
import {
  SimulationNodeDatum,
  SimulationLinkDatum,
  Simulation,
  forceSimulation,
  forceManyBody,
  forceCenter,
  forceLink,
  forceCollide,
  forceRadial,
  zoomIdentity,
  select,
  drag,
  zoom,
} from "d3"
import { Text, Graphics, Application, Container, Circle } from "pixi.js"
import { Group as TweenGroup, Tween as Tweened } from "@tweenjs/tween.js"
import { registerEscapeHandler, removeAllChildren } from "./util"
import { FullSlug, SimpleSlug, getFullSlug, resolveRelative, simplifySlug } from "../../util/path"
import { D3Config } from "../Graph"

type GraphicsInfo = {
  color: string
  gfx: Graphics
  alpha: number
  active: boolean
}

// brain: extend NodeData with `tier`, `source`, `mtime` so the renderer can resolve
// color (tier > source > gray) and apply recency sizing without re-reading the
// `data` Map on every tick.
type NodeData = {
  id: SimpleSlug
  text: string
  tags: string[]
  // brain-extension: frontmatter `tier` from the contentIndex entry (e.g. "vault" /
  // "ingested"). Absent for older artifacts; falls through to source / gray.
  tier?: string
  // brain-extension: frontmatter `source` (e.g. "krisp" / "slack" / "gmail" /
  // "manual").
  source?: string
  // brain-extension: epoch millis of the doc's `date` field. `null` when the
  // contentIndex entry has no parseable date — recency sizing skips it.
  mtime?: number | null
} & SimulationNodeDatum

// brain: extend SimpleLinkData with `kind` / `rule` / `weight` so the link-render
// branch can pick the dashed-edge style and the hover tooltip can surface metadata.
type SimpleLinkData = {
  source: SimpleSlug
  target: SimpleSlug
  // brain-extension: "wiki" for authored Markdown links, "derived" for Phase D
  // fence output. Falls back to "wiki" when the contentIndex entry only carries the
  // legacy flat `links: SimpleSlug[]` shape (e.g. stock-Quartz build).
  kind: "wiki" | "derived"
  // brain-extension: derived-only metadata, populated by the contentIndex emitter.
  rule?: string
  weight?: number
}

type LinkData = {
  source: NodeData
  target: NodeData
  // brain-extension: see SimpleLinkData.kind / rule / weight.
  kind: "wiki" | "derived"
  rule?: string
  weight?: number
} & SimulationLinkDatum<NodeData>

type LinkRenderData = GraphicsInfo & {
  simulationData: LinkData
}

type NodeRenderData = GraphicsInfo & {
  simulationData: NodeData
  label: Text
  radius: number
  // brain-extension: cached at label-creation time so the per-frame
  // label-opacity loop can branch on hub-vs-leaf without recomputing
  // the link count every frame.
  isHub: boolean
}

type GraphRenderMode = "standard" | "workbench"
type GraphWorkbenchMode = "overview" | "incoming" | "outgoing" | "missing" | "suspicious" | "evidence"

type NodeDiagnostic = {
  incomingCount: number
  outgoingCount: number
  derivedCount: number
  authoredCount: number
  issueCount: number
  issueLabels: string[]
  strongRelations: SimpleSlug[]
}

const localStorageKey = "graph-visited"
function getVisited(): Set<SimpleSlug> {
  return new Set(JSON.parse(localStorage.getItem(localStorageKey) ?? "[]"))
}

function addToVisited(slug: SimpleSlug) {
  const visited = getVisited()
  visited.add(slug)
  localStorage.setItem(localStorageKey, JSON.stringify([...visited]))
}

type TweenNode = {
  update: (time: number) => void
  stop: () => void
}

// brain-extension: collapse long node titles to a glance-readable stub by
// default — the dense fullscreen modal otherwise renders as an unreadable
// wall of overlapping text (esp. with NFPA-style "Chapter X: ..." titles).
// The full title is restored on pointerover. The cap is per-render-context
// (cfg.labelMaxLength) so the dense modals can stay tight while the
// sparser sidebar panel keeps more characters visible.
const DEFAULT_LABEL_MAX = 10
function shortLabel(text: string, max: number): string {
  if (text.length <= max) return text
  return text.slice(0, max - 1).trimEnd() + "…"
}

// brain-extension: default minimum incident-link count to treat a node
// as a hub and bypass label truncation. Tuned for depth=1 contexts
// (sidebar / local-fullscreen) where ~6 connections genuinely mark a
// page as central. The global graph (depth=-1, ~1000 nodes) overrides
// this to a higher value via cfg.hubLabelThreshold so the central
// cluster doesn't wall-of-text.
const DEFAULT_HUB_LABEL_THRESHOLD = 6

// brain-extension: graph labels should read as attached to their node,
// Obsidian-style. Small labels sit directly under the circle; structurally
// important hub labels can sit centered inside the circle when the mark is big
// enough to carry the text.
const LABEL_NODE_GAP = 3

// brain-extension: ref pattern that lets a chip-driven rerender swap the live
// cleanup in place without dropping the cleanup-array entry the nav handler
// holds. The nav handler pushes `() => ref.current()` into the global cleanup
// arrays; renderGraph writes the latest cleanup into `ref.current` on every
// (re-)render. That way "dispose this graph" always means "run the most
// recent cleanup", whether that's the original render or any number of
// chip-toggle rebuilds since.
type CleanupRef = { current: () => void }

// brain-extension: chip vocabularies are pinned to the values brain emits
// (`tier: vault|ingested`, `source: krisp|slack|gmail|manual`). Hardcoding
// instead of deriving from `data` keeps the chip row stable when the loaded
// corpus happens to be missing one of the sources — the chip is still there
// to click later, and the order is deterministic.
//
// brain: known coordination point — if a new `source` value is ever added
// (e.g. brain grows a `notion` ingest extractor), it must be added BOTH
// here AND in `Graph.tsx`'s `defaultSourceColors` AND in `graph.scss`'s
// `--brain-source-*` palette. Missing any of those leaves the chip
// selectable but visually indistinguishable from the gray fallback.
const chipVocabularies = {
  tier: ["vault", "ingested"] as const,
  source: ["krisp", "slack", "gmail", "manual"] as const,
} as const

// brain-extension: chip filter state at module scope so a user's selection
// survives SPA navigation. Default = full vocabulary (everything visible).
//
// brain: empty-set-as-wildcard is an INTENTIONAL UX choice, not a bug. The
// acceptance text is literal: "flipping all chips off shows everything",
// which only makes sense if a fully-empty filter set is treated as
// "no filter on this dimension". A future maintainer might be tempted to
// "fix" this to mean "show nothing" (matching set-membership semantics
// strictly) — please don't, the acceptance test will then fail.
const activeChipFilters: { tier: Set<string>; source: Set<string> } = {
  tier: new Set<string>(chipVocabularies.tier),
  source: new Set<string>(chipVocabularies.source),
}

// brain-extension: persistent across SPA navigation. When true, the
// "show unconnected" chip is on and orphans (degree-0 nodes after the
// chip + tag + frontmatter passes) are NOT hidden even when the
// layout's `hideOrphans` cfg is true. Lives at module scope so a
// user's preference carries across hover-driven and chip-driven
// rerenders within a session.
let hideOrphansToggleOn = false

// brain-extension: live search query at module scope. Persists across
// chip-driven rerenders (so toggling a chip mid-search doesn't blow the
// query away) but is reset on SPA nav by the `nav` event handler. The
// initial-search-highlight branch in renderGraph reads this on mount.
let currentSearchQuery = ""

// brain-extension: module-level chip-rerender guard so a chip click that
// fires mid-rebuild (e.g. clicking an inner-render chip while the outer
// renderGraph is still awaiting `app.init`) doesn't double-tear-down. A
// per-instance flag wouldn't catch this case because the outer and inner
// renders each have their own. Module scope makes "is any rerender in
// flight" a single source of truth.
let chipRerenderBusy = false

// brain-extension: linear-decay multiplier for recency sizing. Notes touched today
// get 2.0×; notes >365 days old get 1.0×. Window pinned to one year so day-old vs
// week-old reads as a meaningful size delta. The radius clamp at the call site
// keeps catastrophic values bounded; tweak the constant here if it ever feels too
// aggressive.
function recencyMultiplier(mtime: number): number {
  const days = (Date.now() - mtime) / 86_400_000
  // Clamp on both ends — negative `days` from a future-dated note (e.g. a
  // draft with frontmatter `date: 2027-01-01`) would otherwise yield a
  // multiplier above 2.0 and break the [1.0, 2.0] contract the docstring
  // promises. The radius clamp at the call site is a safety net, not a
  // substitute for honest bounds here.
  const decay = Math.min(1, Math.max(0, 1 - days / 365))
  return 1 + decay
}

// brain-extension: parse a contentIndex entry's `date` into epoch millis. Tolerates
// ISO strings (Quartz default), numeric epochs, and missing values.
function parseMtime(d: unknown): number | null {
  // `typeof NaN === "number"` is true, so a bare type check would let NaN
  // through and propagate into recencyMultiplier → nodeRadius → a PixiJS
  // NaN-radius render. Number.isFinite rejects NaN and ±Infinity together.
  if (typeof d === "number") return Number.isFinite(d) ? d : null
  if (typeof d === "string") {
    const parsed = Date.parse(d)
    return Number.isNaN(parsed) ? null : parsed
  }
  return null
}

// brain-extension: HTML-escape a tooltip string. Frontmatter rule names and
// document titles are both untrusted-ish (authored content) — the tooltip uses
// innerHTML for line breaks so escaping prevents accidental markup injection.
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;")
}

// brain-extension: format derived-edge metadata for the hover tooltip.
//
// Spec deviation: the original design said "show rule + evidence" but the
// contentIndex emitter's BrainLinkRecord carries `rule` and `weight` (no evidence
// — that lives one level deeper, per-rule). We surface rule + weight here. If a
// future iteration threads evidence into BrainLinkRecord, this is the spot to
// extend.
function formatDerivedMeta(rule: string | undefined, weight: number | undefined): string {
  const parts: string[] = []
  if (rule) parts.push(`rule: ${rule}`)
  if (typeof weight === "number") parts.push(`weight ${weight.toFixed(2)}`)
  return parts.join(" · ")
}

// brain-extension: draw a dashed line between two points. PixiJS Graphics has no
// native dash support, so we manually emit segments. The on/off pair maps to "draw
// `on` units, skip `off` units" along the line. Falls back to a solid stroke when
// `period <= 0` to avoid an infinite loop on a misconfigured dash pattern.
function drawDashedLine(
  gfx: Graphics,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  dash: [number, number],
  width: number,
  color: string,
  alpha: number,
): void {
  const dx = x2 - x1
  const dy = y2 - y1
  const length = Math.hypot(dx, dy)
  if (length === 0) return
  const ux = dx / length
  const uy = dy / length
  const [on, off] = dash
  const period = on + off
  if (period <= 0) {
    gfx.moveTo(x1, y1).lineTo(x2, y2).stroke({ alpha, width, color })
    return
  }
  let traveled = 0
  while (traveled < length) {
    const segStart = traveled
    const segEnd = Math.min(traveled + on, length)
    gfx.moveTo(x1 + ux * segStart, y1 + uy * segStart)
    gfx.lineTo(x1 + ux * segEnd, y1 + uy * segEnd)
    traveled += period
  }
  gfx.stroke({ alpha, width, color })
}

function computeGraphDiagnostics(
  nodes: NodeData[],
  links: LinkData[],
): Map<SimpleSlug, NodeDiagnostic> {
  const diagnostics = new Map<SimpleSlug, NodeDiagnostic>()
  for (const node of nodes) {
    diagnostics.set(node.id, {
      incomingCount: 0,
      outgoingCount: 0,
      derivedCount: 0,
      authoredCount: 0,
      issueCount: 0,
      issueLabels: [],
      strongRelations: [],
    })
  }

  for (const link of links) {
    const source = diagnostics.get(link.source.id)
    const target = diagnostics.get(link.target.id)
    if (source) {
      source.outgoingCount += 1
      source.strongRelations.push(link.target.id)
      if (link.kind === "derived") source.derivedCount += 1
      else source.authoredCount += 1
      if (link.kind === "derived" && typeof link.weight === "number" && link.weight < 0.5) {
        source.issueLabels.push("low-confidence derived relation")
      }
    }
    if (target) {
      target.incomingCount += 1
      target.strongRelations.push(link.source.id)
      if (link.kind === "derived") target.derivedCount += 1
      else target.authoredCount += 1
      if (link.kind === "derived" && typeof link.weight === "number" && link.weight < 0.5) {
        target.issueLabels.push("low-confidence derived relation")
      }
    }
  }

  for (const [id, diagnostic] of diagnostics) {
    if (diagnostic.incomingCount === 0) diagnostic.issueLabels.push("no backlinks")
    if (diagnostic.outgoingCount === 0 && !id.startsWith("tags/")) {
      diagnostic.issueLabels.push("no outgoing links")
    }
    diagnostic.issueLabels = [...new Set(diagnostic.issueLabels)]
    diagnostic.issueCount = diagnostic.issueLabels.length
    diagnostic.strongRelations = [...new Set(diagnostic.strongRelations)].slice(0, 8)
  }

  return diagnostics
}

async function renderGraph(
  graph: HTMLElement,
  fullSlug: FullSlug,
  cleanupRef: CleanupRef,
  // brain-extension: when true, suppress every renderer extension (tier/source
  // colors, derived-edge styling, recency sizing, search input, filter chips,
  // orphan/tag-node hiding) so the graph renders with stock-Quartz visual
  // semantics. Wired to the `brain-stock-graph-icon` button; the standard
  // `global-graph-icon` and the local-graph render path leave this at false.
  stockMode = false,
) {
  // brain: hold the rebuild guard for the WHOLE render, not just the
  // chip-click trampoline. The trampoline already sets the flag for chip-
  // triggered rebuilds, but the INITIAL render (called directly from the
  // nav handler with no trampoline) wasn't covered. A chip click during
  // the initial render's await window (fetchData / app.init) would
  // otherwise spawn a second renderGraph that races the outer one and
  // leaks pixi state when the outer resumes. The trampoline sets the flag
  // too — re-setting here is a benign no-op in that path; both end up
  // false via either finally. Body is wrapped in try/finally below; the
  // body itself is left at its original indentation to keep the diff
  // small, since this is a structural wrap of an already-tested 1000-line
  // function.
  chipRerenderBusy = true
  try {
  const slug = simplifySlug(fullSlug)
  const visited = getVisited()
  removeAllChildren(graph)

  // brain-extension: chip-rebuild trampoline. Reads the module-level
  // `chipRerenderBusy` so an inner-render chip click that fires while an
  // outer render is still awaiting `app.init` is dropped on the floor —
  // double-tearing-down the same Pixi app would throw on the second
  // `app.destroy()` call.
  async function rerenderForChipChange() {
    if (chipRerenderBusy) return
    chipRerenderBusy = true
    try {
      cleanupRef.current()
      await renderGraph(graph, fullSlug, cleanupRef, stockMode)
    } finally {
      chipRerenderBusy = false
    }
  }

  let {
    drag: enableDrag,
    zoom: enableZoom,
    depth,
    scale,
    repelForce,
    centerForce,
    linkDistance,
    fontSize,
    opacityScale,
    removeTags,
    showTags,
    focusOnHover,
    enableRadial,
    // brain-extension: brain config knobs. All optional; absent values fall through
    // to upstream-equivalent behavior.
    tierColors,
    sourceColors,
    hideOrphans,
    hideTagNodes,
    hideByFrontmatter,
    derivedEdgeStyle,
    recencySizing,
    // brain-extension: when true, render an in-graph search input above the canvas.
    // Wired below; runtime debouncer + SPA-nav-on-Enter live in the controls block.
    searchEnabled,
    // brain-extension: which chip rows to render. Each named dimension renders one
    // row of chips ("All <dim>" plus one chip per value in `chipVocabularies[dim]`).
    filterChips,
    // brain-extension: scalar multiplier applied to nodeRadius() output and clamp
    // bounds. Falls back to 1 when absent / non-finite so existing render contexts
    // are unaffected. Resolved into `radiusMul` AFTER the stockMode override
    // block below so the dot-grid view stays at the stock-Quartz radius.
    nodeRadiusMultiplier,
    // brain-extension: per-context label truncation cap. Long titles are
    // truncated to `labelMaxLength - 1` chars + `…` and expanded on hover.
    // Defaults to DEFAULT_LABEL_MAX (10) when absent / non-finite — sidebar
    // panel overrides to a higher value so its sparse labels stay legible.
    labelMaxLength,
    // brain-extension: see D3Config docstrings. Both default to gentle
    // curves so existing render contexts are unaffected.
    nodeRadiusGrowthExponent,
    wikiEdgeBaseAlpha,
    hubLabelThreshold,
    nodeRadiusCeiling,
    hubLabelFontMultiplier,
    diagnosticWorkbench,
  } = JSON.parse(graph.dataset["cfg"]!) as D3Config
  const labelMax =
    typeof labelMaxLength === "number" &&
    Number.isFinite(labelMaxLength) &&
    labelMaxLength > 0
      ? Math.floor(labelMaxLength)
      : DEFAULT_LABEL_MAX
  const radiusExponent =
    typeof nodeRadiusGrowthExponent === "number" &&
    Number.isFinite(nodeRadiusGrowthExponent) &&
    nodeRadiusGrowthExponent > 0
      ? nodeRadiusGrowthExponent
      : 0.5
  const wikiEdgeAlpha =
    typeof wikiEdgeBaseAlpha === "number" &&
    Number.isFinite(wikiEdgeBaseAlpha) &&
    wikiEdgeBaseAlpha >= 0 &&
    wikiEdgeBaseAlpha <= 1
      ? wikiEdgeBaseAlpha
      : 1
  const hubThreshold =
    typeof hubLabelThreshold === "number" &&
    Number.isFinite(hubLabelThreshold) &&
    hubLabelThreshold > 0
      ? Math.floor(hubLabelThreshold)
      : DEFAULT_HUB_LABEL_THRESHOLD
  const radiusCeiling =
    typeof nodeRadiusCeiling === "number" &&
    Number.isFinite(nodeRadiusCeiling) &&
    nodeRadiusCeiling > 0
      ? nodeRadiusCeiling
      : 10
  const hubFontMul =
    typeof hubLabelFontMultiplier === "number" &&
    Number.isFinite(hubLabelFontMultiplier) &&
    hubLabelFontMultiplier > 0
      ? hubLabelFontMultiplier
      : 1

  // brain-extension: stock-mode override. When the user opened the global graph
  // via the dot-grid `brain-stock-graph-icon` button, every brain-extension
  // renderer knob is forced off so the graph renders with stock-Quartz visual
  // semantics (current/visited/gray colors, plain stroke for every edge,
  // fixed-radius nodes, no in-graph search input, no chip rows, no orphan
  // hiding). The chip-filter pass and search-highlight branches further down
  // also short-circuit on stockMode so any persisted module-state from a
  // local-graph interaction can't leak into the stock view.
  if (stockMode) {
    tierColors = undefined
    sourceColors = undefined
    hideOrphans = false
    hideTagNodes = false
    hideByFrontmatter = []
    derivedEdgeStyle = undefined
    recencySizing = false
    searchEnabled = false
    filterChips = []
    nodeRadiusMultiplier = undefined
    nodeRadiusGrowthExponent = undefined
    wikiEdgeBaseAlpha = undefined
    hubLabelThreshold = undefined
    nodeRadiusCeiling = undefined
    hubLabelFontMultiplier = undefined
    diagnosticWorkbench = false
  }
  // brain-extension: resolve the radius multiplier AFTER stockMode so the
  // dot-grid view stays at the stock-Quartz radius even when the layout
  // sets a non-1 multiplier on the underlying globalGraph cfg.
  const radiusMul =
    typeof nodeRadiusMultiplier === "number" && Number.isFinite(nodeRadiusMultiplier)
      ? nodeRadiusMultiplier
      : 1

  // brain-extension: build the search/chip controls UI when configured. The
  // elements are appended above the PixiJS canvas (which lands further down)
  // so they read top-to-bottom: search input, then chip rows, then graph.
  // Chip click handlers are wired here because they only need module-level
  // chip state + cleanupRef; the search input's listeners are deferred until
  // `applySearchHighlight` is in scope further down.
  const filterChipsList: ("tier" | "source")[] = Array.isArray(filterChips)
    ? (filterChips as ("tier" | "source")[]).filter(
        (d): d is "tier" | "source" => d === "tier" || d === "source",
      )
    : []
  let controlsEl: HTMLDivElement | null = null
  let searchInputEl: HTMLInputElement | null = null
  // brain-extension: render the "Show unconnected" chip only on graphs
  // whose layout actively hides orphans — otherwise the toggle has
  // nothing to flip. Keeps the sidebar (hideOrphans:false) chip-free.
  const showOrphanToggleChip = hideOrphans === true
  if (searchEnabled || filterChipsList.length > 0 || showOrphanToggleChip) {
    controlsEl = document.createElement("div")
    controlsEl.className = diagnosticWorkbench
      ? "brain-graph-controls brain-graph-toolbar"
      : "brain-graph-controls"
    graph.appendChild(controlsEl)
    if (searchEnabled) {
      const input = document.createElement("input")
      input.type = "search"
      input.className = "brain-graph-search"
      input.placeholder = "Search graph..."
      input.value = currentSearchQuery
      // brain-extension: spellcheck/autocomplete add nothing here and would
      // surface red squiggles under partial slugs — turn them off explicitly.
      input.spellcheck = false
      input.autocomplete = "off"
      controlsEl.appendChild(input)
      searchInputEl = input
    }
    for (const dimension of filterChipsList) {
      const row = document.createElement("div")
      row.className = "brain-graph-chip-row"
      row.dataset["dimension"] = dimension
      const label = document.createElement("span")
      label.className = "brain-graph-chip-label"
      // Capitalize the dimension name for the row label ("tier" → "Tier:").
      label.textContent = `${dimension[0]!.toUpperCase()}${dimension.slice(1)}:`
      row.appendChild(label)
      const allChip = document.createElement("button")
      allChip.type = "button"
      allChip.className = "brain-graph-chip brain-graph-chip-all"
      allChip.textContent = "all"
      // The "all" chip reads as active iff every value in the vocabulary is
      // selected — i.e. the dimension is currently unfiltered.
      if (
        activeChipFilters[dimension].size === chipVocabularies[dimension].length
      ) {
        allChip.classList.add("active")
      }
      allChip.addEventListener("click", () => {
        activeChipFilters[dimension] = new Set<string>(
          chipVocabularies[dimension],
        )
        void rerenderForChipChange()
      })
      row.appendChild(allChip)
      for (const value of chipVocabularies[dimension]) {
        const chip = document.createElement("button")
        chip.type = "button"
        chip.className = "brain-graph-chip"
        chip.textContent = value
        if (activeChipFilters[dimension].has(value)) chip.classList.add("active")
        chip.addEventListener("click", () => {
          const set = activeChipFilters[dimension]
          if (set.has(value)) {
            set.delete(value)
          } else {
            set.add(value)
          }
          void rerenderForChipChange()
        })
        row.appendChild(chip)
      }
      controlsEl.appendChild(row)
    }
    if (showOrphanToggleChip) {
      // brain-extension: standalone toggle chip — no "all" reset, no
      // dimension label. Reads as ON when orphans are currently
      // visible (i.e. the toggle has been flipped to override the
      // layout's hide-orphans default).
      const row = document.createElement("div")
      row.className = "brain-graph-chip-row"
      row.dataset["dimension"] = "orphans"
      const chip = document.createElement("button")
      chip.type = "button"
      chip.className = "brain-graph-chip"
      chip.textContent = "Show unconnected"
      if (hideOrphansToggleOn) chip.classList.add("active")
      chip.addEventListener("click", () => {
        hideOrphansToggleOn = !hideOrphansToggleOn
        void rerenderForChipChange()
      })
      row.appendChild(chip)
      controlsEl.appendChild(row)
    }
  }

  const data: Map<SimpleSlug, BrainContentDetails> = new Map(
    Object.entries<BrainContentDetails>(await fetchData).map(([k, v]) => [
      simplifySlug(k as FullSlug),
      v,
    ]),
  )
  const links: SimpleLinkData[] = []
  const tags: SimpleSlug[] = []
  const validLinks = new Set(data.keys())

  const tweens = new Map<string, TweenNode>()
  for (const [source, details] of data.entries()) {
    // brain: prefer `linkRecords` (the contentIndex emitter's brain output) for
    // kind/rule/weight; fall back to flat `links: SimpleSlug[]` from a
    // stock-Quartz contentIndex.json (e.g. when the overlay was bypassed).
    // Array.isArray narrows the index-signature `unknown` typing on
    // BrainContentDetails.
    let outgoingRecords: BrainLinkRecord[] = []
    if (Array.isArray(details.linkRecords)) {
      outgoingRecords = details.linkRecords as BrainLinkRecord[]
    } else if (Array.isArray(details.links)) {
      outgoingRecords = (details.links as SimpleSlug[]).map((target) => ({
        target,
        kind: "wiki" as const,
      }))
    }

    for (const r of outgoingRecords) {
      if (validLinks.has(r.target)) {
        // brain: carry kind/rule/weight onto SimpleLinkData so the render branch
        // and the tooltip can read them without re-resolving via data.get().
        links.push({
          source,
          target: r.target,
          kind: r.kind,
          rule: r.rule,
          weight: r.weight,
        })
      }
    }

    if (showTags) {
      const detailsTags = (details.tags as string[] | undefined) ?? []
      const localTags = detailsTags
        .filter((tag) => !removeTags.includes(tag))
        .map((tag) => simplifySlug(("tags/" + tag) as FullSlug))

      tags.push(...localTags.filter((tag) => !tags.includes(tag)))

      for (const tag of localTags) {
        // brain: tag-edges are conceptually wiki — they reflect the doc's authored
        // tag list, not derived inference.
        links.push({ source, target: tag, kind: "wiki" })
      }
    }
  }

  const neighbourhood = new Set<SimpleSlug>()
  const wl: (SimpleSlug | "__SENTINEL")[] = [slug, "__SENTINEL"]
  if (depth >= 0) {
    while (depth >= 0 && wl.length > 0) {
      // compute neighbours
      const cur = wl.shift()!
      if (cur === "__SENTINEL") {
        depth--
        wl.push("__SENTINEL")
      } else {
        neighbourhood.add(cur)
        const outgoing = links.filter((l) => l.source === cur)
        const incoming = links.filter((l) => l.target === cur)
        wl.push(...outgoing.map((l) => l.target), ...incoming.map((l) => l.source))
      }
    }
  } else {
    validLinks.forEach((id) => neighbourhood.add(id))
    if (showTags) tags.forEach((tag) => neighbourhood.add(tag))
  }

  // brain-extension: filter pass before simulation. Drop tag nodes (stricter than
  // showTags=false; that only de-renders tag nodes, this removes them from the
  // simulation entirely), frontmatter-flagged nodes, and orphans. Order matters —
  // orphans is computed last because the prior two filters can demote a node to
  // degree 0. This intentionally diverges from the plan's listing order
  // (orphans-first); doing orphans last makes `hideOrphans + hideTagNodes` cut
  // notes that were only linked through a tag node, which matches the
  // user-facing intent ("hide stubs whose only connection was a tag hub")
  // rather than the literal "drop nodes orphaned in the original graph". With
  // the current contentIndex emitter, hideByFrontmatter only catches keys the
  // emitter surfaces (`tier` / `source`); expanding to other keys (e.g.
  // `index`, `moc`) requires the emitter to widen its frontmatter passthrough.
  const filtered = new Set(neighbourhood)
  // brain-extension: chip filter runs before the tag/frontmatter/orphan
  // passes so subsequent passes only see nodes that survive the user's
  // tier/source selection — orphan computation in particular has to be
  // post-chip, otherwise hiding (say) every krisp node could leave nodes
  // that were only linked through krisp pinned in place as "non-orphans".
  // Empty chipFilter set is treated as a wildcard ("flipping all chips off
  // shows everything", per acceptance), so deselecting every chip in a row
  // disables that dimension's filter entirely.
  //
  // brain: forgiving rule — a node missing the relevant frontmatter field
  // ALWAYS passes (we don't filter what we can't measure). Brain has
  // legacy notes with patchy frontmatter; a strict filter would yank them
  // from the graph the moment any chip is on, even though the user's
  // selection has nothing to say about them. Don't tighten this without
  // backfilling the corpus first.
  //
  // Tag aggregates also bypass — they're synthesized hubs, not docs.
  for (const id of [...filtered]) {
    if (id.startsWith("tags/")) continue
    const details = data.get(id)
    if (!details) continue
    const nodeTier = typeof details.tier === "string" ? details.tier : undefined
    const nodeSource =
      typeof details.source === "string" ? details.source : undefined
    // brain-extension: the persisted module-level chip filter is intentionally
    // ignored in stockMode — the user clicked the dot-grid affordance to escape
    // brain semantics, including any chip selection they had left active on
    // the local graph.
    if (
      !stockMode &&
      nodeTier &&
      activeChipFilters.tier.size > 0 &&
      !activeChipFilters.tier.has(nodeTier)
    ) {
      filtered.delete(id)
      continue
    }
    if (
      !stockMode &&
      nodeSource &&
      activeChipFilters.source.size > 0 &&
      !activeChipFilters.source.has(nodeSource)
    ) {
      filtered.delete(id)
    }
  }
  if (hideTagNodes) {
    for (const id of [...filtered]) {
      if (id.startsWith("tags/")) filtered.delete(id)
    }
  }
  if (hideByFrontmatter && hideByFrontmatter.length > 0) {
    for (const id of [...filtered]) {
      const details = data.get(id) as Record<string, unknown> | undefined
      if (!details) continue
      for (const key of hideByFrontmatter) {
        if (details[key]) {
          filtered.delete(id)
          break
        }
      }
    }
  }
  // Recompute the link list against the surviving node set first so degree-0
  // computation reflects the post-filter graph.
  const survivingLinks = links.filter(
    (l) => filtered.has(l.source) && filtered.has(l.target),
  )
  // brain-extension: the "Show unconnected" chip toggles the orphan
  // filter at runtime. When ON, orphans are kept regardless of the
  // layout's `hideOrphans` cfg.
  const effectiveHideOrphans = hideOrphans && !hideOrphansToggleOn
  if (effectiveHideOrphans) {
    const degree = new Map<SimpleSlug, number>()
    for (const id of filtered) degree.set(id, 0)
    for (const l of survivingLinks) {
      degree.set(l.source, (degree.get(l.source) ?? 0) + 1)
      degree.set(l.target, (degree.get(l.target) ?? 0) + 1)
    }
    // brain: never drop the current page even if it's an orphan in the filtered
    // view — a graph rendered for page X with X missing from it would be
    // confusing.
    for (const [id, d] of degree) {
      if (d === 0 && id !== slug) filtered.delete(id)
    }
  }

  const nodes = [...filtered].map((url) => {
    const details = data.get(url)
    const text = url.startsWith("tags/")
      ? "#" + url.substring(5)
      : ((details?.title as string | undefined) ?? url)
    return {
      id: url,
      text,
      tags: (details?.tags as string[] | undefined) ?? [],
      // brain-extension: pull tier/source/mtime onto NodeData for color and
      // recency. `tier` and `source` are typed `string | undefined` on
      // BrainContentDetails; runtime-narrow defensively in case the on-disk JSON
      // carries something unexpected.
      tier: typeof details?.tier === "string" ? details.tier : undefined,
      source: typeof details?.source === "string" ? details.source : undefined,
      mtime: parseMtime(details?.date),
    } as NodeData
  })
  const graphData: { nodes: NodeData[]; links: LinkData[] } = {
    nodes,
    // brain: use the post-filter `survivingLinks` (not the unfiltered `links`)
    // so dropped nodes don't reappear via their edges.
    links: survivingLinks
      .filter((l) => filtered.has(l.source) && filtered.has(l.target))
      .map((l) => ({
        source: nodes.find((n) => n.id === l.source)!,
        target: nodes.find((n) => n.id === l.target)!,
        // brain: propagate kind/rule/weight from SimpleLinkData onto LinkData.
        kind: l.kind,
        rule: l.rule,
        weight: l.weight,
      })),
  }

  const renderMode: GraphRenderMode = diagnosticWorkbench ? "workbench" : "standard"
  let selectedNodeId: SimpleSlug | null = slug
  let workbenchMode: GraphWorkbenchMode = "overview"
  const diagnostics = computeGraphDiagnostics(graphData.nodes, graphData.links)
  let workbenchCanvasPane: HTMLDivElement | null = null
  let workbenchInspector: HTMLElement | null = null
  let workbenchRail: HTMLElement | null = null

  function titleFor(id: SimpleSlug): string {
    return ((data.get(id)?.title as string | undefined) ?? id).toString()
  }

  function renderWorkbenchShell(): HTMLElement | null {
    if (renderMode !== "workbench") return null
    graph.classList.add("brain-graph-workbench-shell")

    const rail = document.createElement("div")
    rail.className = "brain-graph-mode-rail"
    const modes: [GraphWorkbenchMode, string, string][] = [
      ["overview", "Overview", "All useful relations"],
      ["incoming", "Incoming", "Who points here"],
      ["outgoing", "Outgoing", "What this references"],
      ["missing", "Missing", "Thin or unlinked nodes"],
      ["suspicious", "Suspicious", "Low-confidence edges"],
      ["evidence", "Evidence", "Derived relations"],
    ]
    for (const [mode, label, hint] of modes) {
      const button = document.createElement("button")
      button.type = "button"
      button.className = `brain-graph-mode${mode === workbenchMode ? " active" : ""}`
      button.innerHTML = `<span>${escapeHtml(label)}</span><small>${escapeHtml(hint)}</small>`
      button.addEventListener("click", () => {
        workbenchMode = mode
        for (const child of rail.querySelectorAll(".brain-graph-mode")) {
          child.classList.toggle("active", child === button)
        }
        applyWorkbenchMode()
        updateWorkbenchInspector(selectedNodeId)
        renderPixiFromD3()
      })
      rail.appendChild(button)
    }

    const inspector = document.createElement("aside")
    inspector.className = "brain-graph-inspector"
    const canvasPane = document.createElement("div")
    canvasPane.className = "brain-graph-canvas-pane"
    graph.prepend(rail)
    graph.appendChild(canvasPane)
    graph.appendChild(inspector)
    workbenchCanvasPane = canvasPane
    workbenchRail = rail
    workbenchInspector = inspector
    return inspector
  }

  function updateWorkbenchInspector(nodeId: SimpleSlug | null) {
    if (!workbenchInspector) return
    if (!nodeId) {
      workbenchInspector.innerHTML = "<h3>Graph Inspector</h3><p>Select a node to inspect relations.</p>"
      return
    }

    const node = graphData.nodes.find((n) => n.id === nodeId)
    const diagnostic = diagnostics.get(nodeId)
    if (!node || !diagnostic) {
      workbenchInspector.innerHTML = "<h3>Graph Inspector</h3><p>No details available.</p>"
      return
    }

    const relationItems = diagnostic.strongRelations
      .slice(0, 5)
      .map((id) => `<li>${escapeHtml(titleFor(id))}</li>`)
      .join("")
    const issueItems = diagnostic.issueLabels.length
      ? diagnostic.issueLabels.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")
      : "<li>No obvious graph issues from available data.</li>"
    const sourceText = node.source ? ` / ${escapeHtml(node.source)}` : ""

    workbenchInspector.innerHTML = `
      <h3>${escapeHtml(node.text)}</h3>
      <p class="brain-graph-inspector-meta">${escapeHtml(node.tier ?? "unknown")}${sourceText}</p>
      <div class="brain-graph-metrics">
        <div><strong>${diagnostic.incomingCount}</strong><span>incoming</span></div>
        <div><strong>${diagnostic.outgoingCount}</strong><span>outgoing</span></div>
        <div><strong>${diagnostic.derivedCount}</strong><span>derived</span></div>
        <div><strong>${diagnostic.issueCount}</strong><span>issues</span></div>
      </div>
      <h4>Needs Review</h4>
      <ul>${issueItems}</ul>
      <h4>Strong Relations</h4>
      <ul>${relationItems || "<li>No relations in this filtered view.</li>"}</ul>
    `
  }

  function applyWorkbenchMode() {
    if (renderMode !== "workbench") return
    for (const n of nodeRenderData) {
      const diagnostic = diagnostics.get(n.simulationData.id)
      if (!diagnostic) {
        n.active = false
        continue
      }
      if (workbenchMode === "missing") {
        n.active = diagnostic.incomingCount === 0 || diagnostic.outgoingCount === 0
      } else if (workbenchMode === "suspicious") {
        n.active = diagnostic.issueLabels.some((label) => label.includes("low-confidence"))
      } else if (workbenchMode === "evidence") {
        n.active = diagnostic.derivedCount > 0
      } else if (workbenchMode === "incoming" && selectedNodeId) {
        n.active = graphData.links.some(
          (l) => l.target.id === selectedNodeId && l.source.id === n.simulationData.id,
        )
      } else if (workbenchMode === "outgoing" && selectedNodeId) {
        n.active = graphData.links.some(
          (l) => l.source.id === selectedNodeId && l.target.id === n.simulationData.id,
        )
      } else {
        n.active =
          selectedNodeId === null ||
          n.simulationData.id === selectedNodeId ||
          hoveredNeighbours.has(n.simulationData.id)
      }
    }
    for (const l of linkRenderData) {
      if (workbenchMode === "evidence") l.active = l.simulationData.kind === "derived"
      else if (selectedNodeId) {
        l.active =
          l.simulationData.source.id === selectedNodeId ||
          l.simulationData.target.id === selectedNodeId
      }
    }
  }

  // brain-extension: size the canvas to fill the panel below the
  // controls. `graph.offsetHeight` (the inner `.graph-container`) lags
  // layout when the canvas hasn't been appended yet — at init time it
  // can report just the controls' height, which would lock the canvas
  // to a tiny size for the lifetime of the page. Read the parent
  // `.graph-outer` instead: that's the 250px panel upstream sizes
  // explicitly via CSS, so it's the authoritative measure. Subtract
  // the controls' offsetHeight + margin-bottom (offsetHeight excludes
  // margin) to get the canvas allowance, with a small floor so a
  // pathological narrow sidebar can't produce a zero-height canvas
  // (PixiJS rejects 0).
  const outerEl = graph.closest(
    ".graph-outer, .global-graph-container, .local-graph-container, .brain-graph-workbench-container",
  ) as HTMLElement | null
  renderWorkbenchShell()
  updateWorkbenchInspector(selectedNodeId)

  const controlsHeight = controlsEl
    ? controlsEl.offsetHeight + parseFloat(getComputedStyle(controlsEl).marginBottom || "0")
    : 0
  const canvasHost = workbenchCanvasPane ?? graph
  const width = Math.max(canvasHost.clientWidth || graph.offsetWidth, 60)
  const panelHeight =
    renderMode === "workbench"
      ? canvasHost.clientHeight
      : outerEl
        ? outerEl.clientHeight
        : graph.offsetHeight
  const height = Math.max(
    renderMode === "workbench" ? panelHeight : panelHeight - controlsHeight,
    60,
  )

  // we virtualize the simulation and use pixi to actually render it
  const simulation: Simulation<NodeData, LinkData> = forceSimulation<NodeData>(graphData.nodes)
    .force("charge", forceManyBody().strength(-100 * repelForce))
    .force("center", forceCenter().strength(centerForce))
    .force("link", forceLink(graphData.links).distance(linkDistance))
    .force("collide", forceCollide<NodeData>((n) => nodeRadius(n)).iterations(3))

  const radius = (Math.min(width, height) / 2) * 0.8
  if (enableRadial) simulation.force("radial", forceRadial(radius).strength(0.2))

  // brain-extension: pre-tick the simulation so the first frame renders a
  // converged layout instead of d3-force's default phyllotaxis spiral
  // (which initializes nodes in the lower-right quadrant of the canvas
  // and looks like "all my nodes loaded in the top-right corner" until
  // the user drags one and the simulation re-energizes). 300 ticks is
  // enough for both the local graph (≤ a few dozen nodes) and the
  // global graph (~1300 nodes at current corpus size) to settle into
  // a converged cluster; runtime cost is ~50-100ms on init, paid once
  // before first paint.
  simulation.tick(300)

  // brain-extension: explicitly recenter the bounding box of all settled
  // nodes around (0, 0). `forceCenter` only equalizes the *mean* of node
  // positions — for asymmetric graphs (a dense central cluster plus a
  // handful of outlier leaves pulled outward by `forceManyBody`) the
  // mean and the bounding-box center diverge by tens of pixels. The
  // user's eye reads the bounding-box center against the panel's visual
  // center, so without this pass the cluster appears pushed to one side
  // even after the simulation has converged. This is layout-independent
  // and provably symmetric: maxX-minX horizontal extent / 2 and same
  // for vertical, then shift every node by the negation. Skipped on
  // empty graphs (no nodes → nothing to recenter).
  if (graphData.nodes.length > 0) {
    let minX = Infinity
    let maxX = -Infinity
    let minY = Infinity
    let maxY = -Infinity
    for (const n of graphData.nodes) {
      if (n.x === undefined || n.y === undefined) continue
      if (n.x < minX) minX = n.x
      if (n.x > maxX) maxX = n.x
      if (n.y < minY) minY = n.y
      if (n.y > maxY) maxY = n.y
    }
    if (Number.isFinite(minX) && Number.isFinite(minY)) {
      const cx = (minX + maxX) / 2
      const cy = (minY + maxY) / 2
      for (const n of graphData.nodes) {
        if (n.x !== undefined) n.x -= cx
        if (n.y !== undefined) n.y -= cy
      }
    }
  }

  // brain-extension: freeze the simulation after the recenter pass.
  // `forceSimulation` keeps an internal d3-timer running even after
  // `tick(N)` returns — and on each tick `forceCenter` re-equalizes
  // the *mean* of positions to (0, 0), which immediately undoes the
  // bounding-box recenter above (mean and bbox center diverge for
  // asymmetric graphs). Stopping the timer pins the layout to the
  // recentered state. The drag handler below calls
  // `simulation.alphaTarget(1).restart()` on pointerdown, so user
  // interaction still re-energizes the simulation as before — only
  // the idle drift between renders is suppressed.
  simulation.stop()

  // precompute style prop strings as pixi doesn't support css variables
  // brain: extend the precompute list with the brain palette CSS vars so color()
  // can resolve them without re-reading getComputedStyle on every frame. Variable
  // names must match `quartz/styles/graph.scss`; renaming there requires updating
  // here.
  const cssVars = [
    "--secondary",
    "--tertiary",
    "--gray",
    "--light",
    "--lightgray",
    "--dark",
    "--darkgray",
    "--bodyFont",
    // brain-extension: brain palette.
    "--brain-tier-vault",
    "--brain-tier-ingested",
    "--brain-source-krisp",
    "--brain-source-slack",
    "--brain-source-gmail",
    "--brain-source-manual",
  ] as const
  const computedStyleMap = cssVars.reduce(
    (acc, key) => {
      acc[key] = getComputedStyle(document.documentElement).getPropertyValue(key)
      return acc
    },
    {} as Record<(typeof cssVars)[number], string>,
  )

  // brain-extension: resolve a CSS variable name (with leading "--") to its
  // computed value. Falls back to runtime getComputedStyle when the var wasn't
  // precomputed (e.g. user-supplied tierColors pointing at a non-default var).
  const cssVar = (name: string): string => {
    const known = (computedStyleMap as Record<string, string>)[name]
    if (known) return known
    return getComputedStyle(document.documentElement).getPropertyValue(name) || ""
  }

  // brain: replace upstream's 3-way visited/current/gray heuristic with a
  // tier > source > gray fallback chain. The current page still gets the secondary
  // color (consistent with Quartz's "you are here" cue) and tag nodes still bias
  // to tertiary (consistent with their bordered-circle treatment further below).
  // Visited stays as the last fallback before --gray so the navigation memory
  // upstream provides isn't lost. This intentionally extends the plan's literal
  // `tier ?? source ?? gray` formula by inserting `visited` before the gray
  // fallback — losing upstream's navigation cue would be a regression for users
  // who rely on it to track where they've been in the graph.
  const color = (d: NodeData) => {
    const isCurrent = d.id === slug
    if (isCurrent) {
      return computedStyleMap["--secondary"]
    }
    if (d.id.startsWith("tags/")) {
      return computedStyleMap["--tertiary"]
    }
    // brain-extension: tier wins over source. Both fall back to visited / gray.
    if (d.tier && tierColors && tierColors[d.tier]) {
      const resolved = cssVar(tierColors[d.tier])
      if (resolved) return resolved
    }
    if (d.source && sourceColors && sourceColors[d.source]) {
      const resolved = cssVar(sourceColors[d.source])
      if (resolved) return resolved
    }
    if (visited.has(d.id)) {
      return computedStyleMap["--tertiary"]
    }
    return computedStyleMap["--gray"]
  }

  function nodeRadius(d: NodeData) {
    const numLinks = graphData.links.filter(
      (l) => l.source.id === d.id || l.target.id === d.id,
    ).length
    // brain-extension: configurable degree → radius curve. The default
    // exponent (0.5 = sqrt) gives a gentle hub/leaf ratio; bumping the
    // exponent toward 1 widens the spread so hubs visibly dominate
    // (matches Obsidian's "Settings" being ~5× a leaf node).
    const base = 2 + Math.pow(numLinks, radiusExponent)
    // brain-extension: scale by recency when enabled; clamp final radius to
    // [2, 10]. The previous [1, 4] clamp was too tight — any node with ≥ 4
    // links pinned to the ceiling, and any node touched today (recency
    // multiplier 2×) hit the ceiling at base 2, so the global graph rendered
    // every node at the same size. Lifting the ceiling to 10 lets degree
    // differences read visually (e.g. a hub with 25 links is ~3× larger
    // than a leaf with 1 link).
    const multiplier =
      recencySizing && d.mtime !== null && d.mtime !== undefined
        ? recencyMultiplier(d.mtime)
        : 1
    // brain-extension: `radiusMul` (from the `nodeRadiusMultiplier` cfg knob)
    // scales both the radius value AND the clamp bounds, so a 2× multiplier
    // produces dots that are uniformly twice as big without compressing
    // hub/leaf size differences against the ceiling.
    return Math.min(
      radiusCeiling * radiusMul,
      Math.max(2 * radiusMul, base * multiplier * radiusMul),
    )
  }

  function unscaledLabelSize(label: Text): { width: number; height: number } {
    const scaleX = Math.abs(label.scale.x)
    const scaleY = Math.abs(label.scale.y)
    const width =
      scaleX === 0 || !Number.isFinite(scaleX) ? label.width : label.width / scaleX
    const height =
      scaleY === 0 || !Number.isFinite(scaleY) ? label.height : label.height / scaleY
    return { width, height }
  }

  function placeNodeLabel(
    label: Text,
    node: NodeRenderData,
    x: number,
    y: number,
  ) {
    const labelSize = unscaledLabelSize(label)
    const diameter = node.radius * 2
    if (
      node.isHub &&
      labelSize.height <= diameter &&
      labelSize.width <= diameter
    ) {
      label.anchor.set(0.5, 0.5)
      label.position.set(x, y)
      return
    }

    label.anchor.set(0.5, 0)
    label.position.set(x, y + node.radius + LABEL_NODE_GAP)
  }

  let hoveredNodeId: string | null = null
  let hoveredNeighbours: Set<string> = new Set()
  // brain-extension: latest zoom-driven label opacity. The d3-zoom callback
  // writes here on every zoom event; `renderLabels` reads it to compute the
  // baseline alpha for non-active labels during hover (so they fade to a
  // small fraction of their natural opacity rather than disappearing /
  // staying full-bright).
  let currentScaleOpacity = 0
  const linkRenderData: LinkRenderData[] = []
  const nodeRenderData: NodeRenderData[] = []
  // brain-extension: search highlight state. When true, the renderers dim
  // non-active nodes/links the same way `focusOnHover` does — `n.active` is
  // populated by `applySearchHighlight` (defined below) from the search
  // input's debounced match pass. Hover and search both write to the same
  // `n.active` field; whichever fires last wins for that node, which matches
  // user intent (a hover after typing reveals the hovered node's neighbours,
  // not the search hits).
  let searchActive = false
  // brain-extension: debounce timer handle for the search input. Cleared on
  // cleanup so a pending fire after teardown doesn't reach a destroyed app.
  let searchDebounceTimer: number | null = null
  function updateHoverInfo(newHoveredId: string | null) {
    hoveredNodeId = newHoveredId

    if (newHoveredId === null) {
      hoveredNeighbours = new Set()
      // brain: when a search highlight is active, mouseleave shouldn't
      // blow the matched-set away. Without this re-apply, hovering a node
      // and then leaving clears every n.active and dims the WHOLE graph
      // until the next keystroke. Re-applying the search rebuilds the
      // matched-set state in place; the pointerleave handler will
      // renderPixiFromD3 right after returning, which makes the (very
      // slight) double-render harmless — both calls render the same final
      // alpha state.
      if (searchActive) {
        applySearchHighlight(currentSearchQuery)
        return
      }
      for (const n of nodeRenderData) {
        n.active = false
      }

      for (const l of linkRenderData) {
        l.active = false
      }
    } else {
      hoveredNeighbours = new Set()
      for (const l of linkRenderData) {
        const linkData = l.simulationData
        if (linkData.source.id === newHoveredId || linkData.target.id === newHoveredId) {
          hoveredNeighbours.add(linkData.source.id)
          hoveredNeighbours.add(linkData.target.id)
        }

        l.active = linkData.source.id === newHoveredId || linkData.target.id === newHoveredId
      }

      for (const n of nodeRenderData) {
        n.active = hoveredNeighbours.has(n.simulationData.id)
      }
    }
    if (renderMode === "workbench") {
      applyWorkbenchMode()
    }
  }

  // brain-extension: tooltip element for derived-edge metadata.
  //
  // Spec deviation: the original design said "show rule + evidence on
  // derived-edge hover", but PixiJS edges have no native hit-test for thin lines,
  // so we surface the metadata on NODE hover instead — when a node has any
  // incident derived edges, the tooltip lists each edge's `rule` and `weight`.
  // The user gets the same info without the cost of per-frame polygon hit areas.
  // Documented here so a future maintainer revisiting the UX knows why the
  // tooltip is on nodes rather than edges. (And per the formatDerivedMeta
  // comment: design said "evidence" but the link record carries "weight" —
  // that's a separate spec/contentIndex-emitter reconciliation.)
  const tooltip = document.createElement("div")
  tooltip.className = "brain-graph-tooltip"
  tooltip.style.position = "absolute"
  tooltip.style.display = "none"
  tooltip.style.pointerEvents = "none"
  tooltip.style.background = computedStyleMap["--light"] || "#fff"
  tooltip.style.color = computedStyleMap["--dark"] || "#000"
  tooltip.style.border = `1px solid ${computedStyleMap["--lightgray"] || "#ccc"}`
  tooltip.style.borderRadius = "4px"
  tooltip.style.padding = "4px 8px"
  tooltip.style.fontSize = "0.75rem"
  tooltip.style.lineHeight = "1.3"
  tooltip.style.maxWidth = "260px"
  tooltip.style.zIndex = "10"
  if (!graph.style.position) graph.style.position = "relative"
  graph.appendChild(tooltip)

  function showTooltipForNode(nodeId: string, clientX: number, clientY: number) {
    const derivedHere = graphData.links.filter(
      (l) =>
        l.kind === "derived" && (l.source.id === nodeId || l.target.id === nodeId),
    )
    if (derivedHere.length === 0) {
      tooltip.style.display = "none"
      return
    }
    const lines = derivedHere.map((l) => {
      const otherId = l.source.id === nodeId ? l.target.id : l.source.id
      const otherText =
        ((data.get(otherId)?.title as string | undefined) ?? otherId).toString()
      const meta = formatDerivedMeta(l.rule, l.weight)
      return meta ? `${otherText} — ${meta}` : otherText
    })
    tooltip.innerHTML = lines
      .map((line) => `<div>${escapeHtml(line)}</div>`)
      .join("")
    const rect = graph.getBoundingClientRect()
    tooltip.style.left = `${clientX - rect.left + 12}px`
    tooltip.style.top = `${clientY - rect.top + 12}px`
    tooltip.style.display = "block"
  }

  function hideTooltip() {
    tooltip.style.display = "none"
  }

  let dragStartTime = 0
  let dragging = false

  function renderLinks() {
    tweens.get("link")?.stop()
    const tweenGroup = new TweenGroup()

    for (const l of linkRenderData) {
      // brain: derived edges start at the configured (typically <1) base alpha;
      // wiki edges use `wikiEdgeAlpha` (default 1, lowered on dense fullscreen
      // views so the graph reads "labels first, structure second"). Hover
      // dimming layers on top so a hovered-neighbour derived edge doesn't
      // fade further than its base.
      const baseAlpha =
        l.simulationData.kind === "derived" && derivedEdgeStyle
          ? derivedEdgeStyle.alpha
          : wikiEdgeAlpha
      let alpha = baseAlpha

      // if we are hovering over a node, we want to highlight the immediate neighbours
      // with full alpha and the rest with default alpha
      // brain: extend the dim trigger to cover search-highlight too — when
      // the user has typed a query, non-matching links fade the same way
      // hover-non-neighbours do.
      if (hoveredNodeId || searchActive || renderMode === "workbench") {
        alpha = l.active ? baseAlpha : baseAlpha * 0.2
      }

      // brain-extension: paint hover-active edges in the brain accent
      // (`--secondary`, indigo-violet) instead of `--gray` — matches
      // Obsidian's behavior where selecting a node lights up its
      // immediate connections in a vivid color so the user can trace
      // them at a glance against the dimmed-down rest of the graph.
      // Non-active edges still fade to the very-light gray.
      l.color = l.active ? computedStyleMap["--secondary"] : computedStyleMap["--lightgray"]
      tweenGroup.add(new Tweened<LinkRenderData>(l).to({ alpha }, 200))
    }

    tweenGroup.getAll().forEach((tw) => tw.start())
    tweens.set("link", {
      update: tweenGroup.update.bind(tweenGroup),
      stop() {
        tweenGroup.getAll().forEach((tw) => tw.stop())
      },
    })
  }

  function renderLabels() {
    tweens.get("label")?.stop()
    const tweenGroup = new TweenGroup()

    const defaultScale = 1 / scale
    const activeScale = defaultScale * 1.1
    // brain-extension: when a hover or search is active, dim every
    // non-active label to a small fraction of its natural opacity and
    // bring the active labels (hovered node + its neighbours / search
    // matches) to full brightness — matches Obsidian's behavior where
    // selecting a node lights up its neighbourhood and fades the rest
    // of the canvas. When nothing is active, every label tweens back
    // toward `currentScaleOpacity` (the zoom-driven baseline) so labels
    // return to their natural visibility on pointerleave.
    const hoverOrSearchActive =
      hoveredNodeId !== null || searchActive || renderMode === "workbench"
    const dimAlpha = currentScaleOpacity * 0.15
    for (const n of nodeRenderData) {
      const nodeId = n.simulationData.id
      let targetAlpha: number
      let targetScale: number
      if (hoveredNodeId === nodeId) {
        targetAlpha = 1
        targetScale = activeScale
      } else if (hoverOrSearchActive && n.active) {
        targetAlpha = 1
        targetScale = defaultScale
      } else if (hoverOrSearchActive) {
        targetAlpha = dimAlpha
        targetScale = defaultScale
      } else if (n.isHub) {
        // brain-extension: hub labels pinned to full alpha at rest so
        // they read at the same brightness as a hovered label — the
        // zoom-driven `currentScaleOpacity` was leaving them at ~0.3–0.5
        // at fit zoom, which felt washed out compared to stock Quartz.
        targetAlpha = 1
        targetScale = defaultScale
      } else {
        // brain-extension: non-hub labels dim by default — they reveal
        // on hover (via the hovered + n.active branches above) but
        // otherwise stay quiet so the canvas reads as "hubs first,
        // detail on demand."
        targetAlpha = dimAlpha
        targetScale = defaultScale
      }
      tweenGroup.add(
        new Tweened<Text>(n.label).to(
          {
            alpha: targetAlpha,
            scale: { x: targetScale, y: targetScale },
          },
          100,
        ),
      )
    }

    tweenGroup.getAll().forEach((tw) => tw.start())
    tweens.set("label", {
      update: tweenGroup.update.bind(tweenGroup),
      stop() {
        tweenGroup.getAll().forEach((tw) => tw.stop())
      },
    })
  }

  function renderNodes() {
    tweens.get("hover")?.stop()

    const tweenGroup = new TweenGroup()
    for (const n of nodeRenderData) {
      let alpha = 1

      // if we are hovering over a node, we want to highlight the immediate neighbours
      // brain: extend the dim trigger to cover search-highlight. Search
      // bypasses the `focusOnHover` gate intentionally — it's an explicit
      // user action that should always dim non-matches, regardless of
      // whether the layout opted into hover focus.
      if ((hoveredNodeId !== null && focusOnHover) || searchActive || renderMode === "workbench") {
        alpha = n.active ? 1 : 0.2
      }

      tweenGroup.add(new Tweened<Graphics>(n.gfx, tweenGroup).to({ alpha }, 200))
    }

    tweenGroup.getAll().forEach((tw) => tw.start())
    tweens.set("hover", {
      update: tweenGroup.update.bind(tweenGroup),
      stop() {
        tweenGroup.getAll().forEach((tw) => tw.stop())
      },
    })
  }

  function renderPixiFromD3() {
    renderNodes()
    renderLinks()
    renderLabels()
  }

  tweens.forEach((tween) => tween.stop())
  tweens.clear()

  const app = new Application()
  await app.init({
    width,
    height,
    antialias: true,
    autoStart: false,
    autoDensity: true,
    backgroundAlpha: 0,
    preference: "webgpu",
    resolution: window.devicePixelRatio,
    eventMode: "static",
  })
  canvasHost.appendChild(app.canvas)
  let resizeObserver: ResizeObserver | null = null
  let resizeRerenderTimer: number | null = null
  let observedCanvasWidth = width
  let observedCanvasHeight = height
  if (renderMode === "workbench" && workbenchCanvasPane && "ResizeObserver" in window) {
    resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      const nextWidth = Math.round(entry.contentRect.width)
      const nextHeight = Math.round(entry.contentRect.height)
      if (
        Math.abs(nextWidth - observedCanvasWidth) < 2 &&
        Math.abs(nextHeight - observedCanvasHeight) < 2
      ) {
        return
      }
      observedCanvasWidth = nextWidth
      observedCanvasHeight = nextHeight
      if (resizeRerenderTimer !== null) window.clearTimeout(resizeRerenderTimer)
      resizeRerenderTimer = window.setTimeout(() => {
        resizeRerenderTimer = null
        void rerenderForChipChange()
      }, 120)
    })
    resizeObserver.observe(workbenchCanvasPane)
  }

  const stage = app.stage
  stage.interactive = false

  const labelsContainer = new Container<Text>({ zIndex: 3, isRenderGroup: true })
  const nodesContainer = new Container<Graphics>({ zIndex: 2, isRenderGroup: true })
  const linkContainer = new Container<Graphics>({ zIndex: 1, isRenderGroup: true })
  stage.addChild(nodesContainer, labelsContainer, linkContainer)

  for (const n of graphData.nodes) {
    const nodeId = n.id

    // brain-extension: cache the full + truncated label strings on the
    // closure so the pointer handlers below can flip between them without
    // recomputing on every hover. When the title already fits inside
    // `labelMax`, both strings are identical and the value-equality guard
    // in the handlers short-circuits the swap (no Pixi atlas churn).
    const fullText = n.text
    // brain-extension: any node above HUB_LABEL_THRESHOLD links is treated
    // as "big enough to deserve its full title" — the layout's truncation
    // cap is bypassed for these nodes (in every render context, sidebar
    // and modals alike) so the structural anchors of the graph stay
    // immediately readable without requiring a hover.
    const numLinks = graphData.links.filter(
      (l) => l.source.id === n.id || l.target.id === n.id,
    ).length
    const isHubNode = numLinks >= hubThreshold
    const truncatedText = isHubNode ? fullText : shortLabel(fullText, labelMax)
    // brain-extension: hub nodes get a larger label fontSize (× hubFontMul)
    // so they read as "this is a structurally important page" at a glance.
    const labelFontSize = (isHubNode ? fontSize * hubFontMul : fontSize) * 15

    const label = new Text({
      interactive: false,
      eventMode: "none",
      text: truncatedText,
      alpha: 0,
      anchor: { x: 0.5, y: 0.5 },
      style: {
        fontSize: labelFontSize,
        fill: computedStyleMap["--dark"],
        fontFamily: computedStyleMap["--bodyFont"],
      },
      resolution: window.devicePixelRatio * 4,
    })
    label.scale.set(1 / scale)

    let oldLabelOpacity = 0
    const isTagNode = nodeId.startsWith("tags/")
    const radius = nodeRadius(n)
    const gfx = new Graphics({
      interactive: true,
      label: nodeId,
      eventMode: "static",
      hitArea: new Circle(0, 0, radius),
      cursor: "pointer",
    })
      .circle(0, 0, radius)
      .fill({ color: isTagNode ? computedStyleMap["--light"] : color(n) })
      .on("pointerover", (e) => {
        selectedNodeId = e.target.label as SimpleSlug
        updateWorkbenchInspector(selectedNodeId)
        updateHoverInfo(e.target.label)
        oldLabelOpacity = label.alpha
        // brain-extension: swap to the full title on hover. Pixi's Text
        // regenerates its glyph atlas on `.text =` mutation, so we only
        // assign when the value differs (no-op when already full).
        if (fullText !== truncatedText && label.text !== fullText) {
          label.text = fullText
        }
        // brain-extension: surface derived-edge metadata when this node has any
        // incident derived edges. Spec deviation rationale documented at the
        // tooltip-create site above. Pixi pointer events expose clientX/clientY
        // on FederatedPointerEvent — narrow defensively in case a future Pixi
        // version reshapes the event.
        const ev = e as unknown as { clientX?: number; clientY?: number }
        const cx = typeof ev.clientX === "number" ? ev.clientX : 0
        const cy = typeof ev.clientY === "number" ? ev.clientY : 0
        showTooltipForNode(e.target.label, cx, cy)
        if (!dragging) {
          renderPixiFromD3()
        }
      })
      .on("pointerleave", () => {
        updateHoverInfo(null)
        label.alpha = oldLabelOpacity
        // brain-extension: restore the truncated label when the cursor leaves.
        if (fullText !== truncatedText && label.text !== truncatedText) {
          label.text = truncatedText
        }
        // brain-extension: hide the derived-edge tooltip when the cursor leaves.
        hideTooltip()
        if (!dragging) {
          renderPixiFromD3()
        }
      })

    if (isTagNode) {
      gfx.stroke({ width: 2, color: computedStyleMap["--tertiary"] })
    }

    nodesContainer.addChild(gfx)
    labelsContainer.addChild(label)

    const nodeRenderDatum: NodeRenderData = {
      simulationData: n,
      gfx,
      label,
      radius,
      color: color(n),
      alpha: 1,
      active: false,
      isHub: isHubNode,
    }

    nodeRenderData.push(nodeRenderDatum)
  }

  for (const l of graphData.links) {
    const gfx = new Graphics({ interactive: false, eventMode: "none" })
    linkContainer.addChild(gfx)

    const linkRenderDatum: LinkRenderData = {
      simulationData: l,
      gfx,
      color: computedStyleMap["--lightgray"],
      alpha: 1,
      active: false,
    }

    linkRenderData.push(linkRenderDatum)
  }

  if (renderMode === "workbench") {
    applyWorkbenchMode()
    renderPixiFromD3()
  }

  // brain-extension: apply a debounced search query against the loaded
  // contentIndex. Matches against `title` and `tags` only — we deliberately
  // skip the full `content` field (Quartz embeds the rendered body there)
  // because substring-matching every doc body on every keystroke is slow
  // for personal corpora >1k docs and adds noise to the highlight set.
  // Active links are only the ones whose endpoints are BOTH matched, which
  // means a hop between two hits stays bright while pendant hits stand
  // alone. Empty/whitespace query → clear highlights and let the existing
  // hover machinery take over.
  function applySearchHighlight(query: string) {
    const trimmed = query.trim()
    if (!trimmed) {
      searchActive = false
      for (const n of nodeRenderData) n.active = false
      for (const l of linkRenderData) l.active = false
      renderPixiFromD3()
      return
    }
    const q = trimmed.toLowerCase()
    const matched = new Set<SimpleSlug>()
    for (const n of nodeRenderData) {
      const id = n.simulationData.id
      const details = data.get(id)
      const titleStr =
        typeof details?.title === "string" ? details.title.toLowerCase() : ""
      const rawTags = details?.tags
      const tagsList: string[] = Array.isArray(rawTags)
        ? (rawTags as unknown[])
            .filter((t): t is string => typeof t === "string")
            .map((t) => t.toLowerCase())
        : []
      if (titleStr.includes(q) || tagsList.some((t) => t.includes(q))) {
        matched.add(id)
      }
    }
    searchActive = true
    for (const n of nodeRenderData) {
      n.active = matched.has(n.simulationData.id)
    }
    // brain: extends the plan's literal "dim non-matching nodes" to also
    // light up edges where BOTH endpoints matched. The plan only specifies
    // node-level highlighting, but a search where every node fades and
    // every edge fades looks broken — keeping cluster-internal edges
    // bright preserves the "cluster of matches" gestalt. Pendant edges
    // (one endpoint matched, one not) still dim along with the unmatched
    // node, which is the desired UX.
    for (const l of linkRenderData) {
      const ld = l.simulationData
      l.active = matched.has(ld.source.id) && matched.has(ld.target.id)
    }
    renderPixiFromD3()
  }

  if (searchInputEl) {
    const inputEl = searchInputEl
    inputEl.addEventListener("input", () => {
      currentSearchQuery = inputEl.value
      if (searchDebounceTimer !== null) {
        window.clearTimeout(searchDebounceTimer)
      }
      // 100ms debounce — fast enough that typing feels live, slow enough
      // that holding a key doesn't fire the match pass per repeat tick.
      searchDebounceTimer = window.setTimeout(() => {
        searchDebounceTimer = null
        applySearchHighlight(currentSearchQuery)
      }, 100)
    })
    inputEl.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return
      e.preventDefault()
      const q = inputEl.value.trim().toLowerCase()
      if (!q) return
      // brain: top-hit picker — title-priority with lowest-substring-index
      // wins. Two intentional rules:
      //   1. TITLE matches only — tag-only matches keep the highlight but
      //      don't qualify for nav. A "tags" hit means the QUERY hit a
      //      tag, not that the user wants to land on the tag-aggregate
      //      page; navigating there would feel wrong.
      //   2. LOWEST INDEX wins — "person-a" matching "person-x last-c" (idx 0)
      //      beats "ASKING_PERSON-A.md" (idx 7). Substring position is a rough
      //      "is this the document about X" heuristic; the alternative
      //      (alphabetical, recency, link-degree) all need more state and
      //      don't measurably improve the top-pick for a personal corpus.
      // Tag-aggregate slugs (`tags/foo`) are skipped explicitly so the
      // picker never lands on one even if its slug substring matches.
      let bestId: SimpleSlug | null = null
      let bestIdx = Number.POSITIVE_INFINITY
      for (const n of nodeRenderData) {
        const id = n.simulationData.id
        if (id.startsWith("tags/")) continue
        const details = data.get(id)
        const titleStr =
          typeof details?.title === "string" ? details.title.toLowerCase() : ""
        const idx = titleStr.indexOf(q)
        if (idx >= 0 && idx < bestIdx) {
          bestIdx = idx
          bestId = id
        }
      }
      if (bestId) {
        const targ = resolveRelative(fullSlug, bestId)
        window.spaNavigate(new URL(targ, window.location.toString()))
      }
    })
    // Replay any persisted query on chip-driven rerender so flipping a chip
    // mid-search keeps the highlight intact rather than blowing it away.
    // brain-extension: stockMode opted out of search above (no input element
    // was rendered), so don't replay a leftover module-level query — the
    // stock view should look identical regardless of prior search state.
    if (currentSearchQuery && !stockMode) {
      applySearchHighlight(currentSearchQuery)
    }
  }

  let currentTransform = zoomIdentity
  if (enableDrag) {
    select<HTMLCanvasElement, NodeData | undefined>(app.canvas).call(
      drag<HTMLCanvasElement, NodeData | undefined>()
        .container(() => app.canvas)
        .subject(() => graphData.nodes.find((n) => n.id === hoveredNodeId))
        .on("start", function dragstarted(event) {
          if (!event.active) simulation.alphaTarget(1).restart()
          event.subject.fx = event.subject.x
          event.subject.fy = event.subject.y
          event.subject.__initialDragPos = {
            x: event.subject.x,
            y: event.subject.y,
            fx: event.subject.fx,
            fy: event.subject.fy,
          }
          dragStartTime = Date.now()
          dragging = true
        })
        .on("drag", function dragged(event) {
          const initPos = event.subject.__initialDragPos
          event.subject.fx = initPos.x + (event.x - initPos.x) / currentTransform.k
          event.subject.fy = initPos.y + (event.y - initPos.y) / currentTransform.k
        })
        .on("end", function dragended(event) {
          if (!event.active) simulation.alphaTarget(0)
          event.subject.fx = null
          event.subject.fy = null
          dragging = false

          // if the time between mousedown and mouseup is short, we consider it a click
          if (Date.now() - dragStartTime < 500) {
            const node = graphData.nodes.find((n) => n.id === event.subject.id) as NodeData
            const targ = resolveRelative(fullSlug, node.id)
            window.spaNavigate(new URL(targ, window.location.toString()))
          }
        }),
    )
  } else {
    for (const node of nodeRenderData) {
      node.gfx.on("click", () => {
        const targ = resolveRelative(fullSlug, node.simulationData.id)
        window.spaNavigate(new URL(targ, window.location.toString()))
      })
    }
  }

  if (enableZoom) {
    const zoomBehavior = zoom<HTMLCanvasElement, NodeData>()
      .extent([
        [0, 0],
        [width, height],
      ])
      .scaleExtent([0.25, 4])
      .on("zoom", ({ transform }) => {
        currentTransform = transform
        stage.scale.set(transform.k, transform.k)
        stage.position.set(transform.x, transform.y)

        // zoom adjusts opacity of labels too
        const scale = transform.k * opacityScale
        let scaleOpacity = Math.max((scale - 1) / 3.75, 0)
        currentScaleOpacity = scaleOpacity
        // brain-extension: respect both the hover-dim state AND the
        // hub-vs-non-hub baseline when re-applying the zoom-driven
        // opacity. Without this, zooming would clobber both the
        // hover-dim effect (renderLabels applied during hover) and
        // the hub-bright / non-hub-dim baseline that's the steady
        // state when nothing is hovered.
        const hoverOrSearchActive =
          hoveredNodeId !== null || searchActive || renderMode === "workbench"
        const dimAlpha = scaleOpacity * 0.15
        const activeLabels = nodeRenderData.filter((n) => n.active).flatMap((n) => n.label)
        const hubLabels = new Set(nodeRenderData.filter((n) => n.isHub).map((n) => n.label))

        for (const label of labelsContainer.children) {
          if (activeLabels.includes(label)) {
            // Active labels are pinned at full alpha by renderLabels;
            // don't overwrite them on zoom.
            continue
          }
          if (hoverOrSearchActive) {
            label.alpha = dimAlpha
          } else {
            // brain-extension: hub labels pinned to full alpha at rest
            // (matches the renderLabels branch); non-hub labels track
            // the zoom-driven dim baseline.
            label.alpha = hubLabels.has(label) ? 1 : dimAlpha
          }
        }
      })

    const zoomSelection = select<HTMLCanvasElement, NodeData>(app.canvas).call(zoomBehavior)

    // brain-extension: fit the cluster to the canvas with 12% padding on
    // every side. After pre-tick + bbox-recenter the cluster is centered
    // at sim (0, 0); render translation puts that at canvas center
    // (width/2, height/2). For dense local graphs (COMPANY_REDACTED Hub: ~12
    // neighbors radiating from a hub at ~30px linkDistance) the cluster
    // diameter exceeds the canvas height, so the bottom row gets clipped
    // even when perfectly centered. Compute an initial zoom transform
    // that scales the cluster to fit within (1 - 2 * pad) of each axis,
    // capped at scale=1 (never zoom IN beyond identity for small
    // clusters that already fit). Calling `zoomBehavior.transform(...)`
    // rather than mutating stage directly keeps d3-zoom's internal
    // state in sync, so subsequent user pan/zoom is relative to the
    // fit position, not identity.
    if (graphData.nodes.length > 0) {
      let bMinX = Infinity
      let bMaxX = -Infinity
      let bMinY = Infinity
      let bMaxY = -Infinity
      for (const n of graphData.nodes) {
        if (n.x === undefined || n.y === undefined) continue
        if (n.x < bMinX) bMinX = n.x
        if (n.x > bMaxX) bMaxX = n.x
        if (n.y < bMinY) bMinY = n.y
        if (n.y > bMaxY) bMaxY = n.y
      }
      if (Number.isFinite(bMinX) && Number.isFinite(bMinY)) {
        // bbox half-extent in render coords (cluster is recentered so
        // bMinX = -bMaxX and bMinY = -bMaxY, but compute via max(|min|, |max|)
        // for safety against the symmetric-recenter assumption breaking).
        const halfW = Math.max(Math.abs(bMinX), Math.abs(bMaxX), 1)
        const halfH = Math.max(Math.abs(bMinY), Math.abs(bMaxY), 1)
        const padFraction = 0.12
        const fitScale = Math.min(
          ((1 - 2 * padFraction) * width) / (2 * halfW),
          ((1 - 2 * padFraction) * height) / (2 * halfH),
          1.0,
        )
        // Scale around canvas center so the cluster (which is at canvas
        // center pre-zoom) stays centered post-zoom.
        const tx = (width / 2) * (1 - fitScale)
        const ty = (height / 2) * (1 - fitScale)
        zoomBehavior.transform(zoomSelection, zoomIdentity.translate(tx, ty).scale(fitScale))
      }
    }
  }

  let stopAnimation = false
  function animate(time: number) {
    if (stopAnimation) return
    for (const n of nodeRenderData) {
      const { x, y } = n.simulationData
      // brain: gate on "actually has a position" (not "position is truthy"):
      // `!x || !y` is true when EITHER coordinate equals 0, so a node that
      // settles exactly at (0, 0) — common for sparse graphs whose bounding
      // box gets recentered to origin, or for a single-node local graph
      // initialized at the phyllotaxis origin — never has its position
      // updated and stays at the Pixi default (canvas top-left).
      if (x === undefined || y === undefined) continue
      n.gfx.position.set(x + width / 2, y + height / 2)
      if (n.label) {
        placeNodeLabel(n.label, n, x + width / 2, y + height / 2)
      }
    }

    for (const l of linkRenderData) {
      const linkData = l.simulationData
      l.gfx.clear()
      const x1 = linkData.source.x! + width / 2
      const y1 = linkData.source.y! + height / 2
      const x2 = linkData.target.x! + width / 2
      const y2 = linkData.target.y! + height / 2

      // brain: branch on link kind. Derived edges (Phase D fence) get the
      // configured dashed/translucent style; wiki edges keep upstream's solid
      // stroke. Until the derived-edge transformer ships every record will
      // arrive with kind: "wiki" and the derived branch never triggers — that's
      // expected.
      if (linkData.kind === "derived" && derivedEdgeStyle) {
        drawDashedLine(
          l.gfx,
          x1,
          y1,
          x2,
          y2,
          derivedEdgeStyle.dash,
          derivedEdgeStyle.width,
          l.color,
          l.alpha,
        )
      } else {
        l.gfx
          .moveTo(x1, y1)
          .lineTo(x2, y2)
          .stroke({ alpha: l.alpha, width: 1, color: l.color })
      }
    }

    tweens.forEach((t) => t.update(time))
    app.renderer.render(stage)
    requestAnimationFrame(animate)
  }

  requestAnimationFrame(animate)
  const cleanup = () => {
    stopAnimation = true
    // brain-extension: cancel any pending search-debounce so a fire after
    // teardown can't reach a destroyed Pixi app or stale render data.
    if (searchDebounceTimer !== null) {
      window.clearTimeout(searchDebounceTimer)
      searchDebounceTimer = null
    }
    if (resizeRerenderTimer !== null) {
      window.clearTimeout(resizeRerenderTimer)
      resizeRerenderTimer = null
    }
    resizeObserver?.disconnect()
    // brain-extension: tear down the tooltip element so re-renders (theme change,
    // SPA nav) don't accumulate orphaned tooltips in the DOM.
    if (tooltip.parentElement) tooltip.parentElement.removeChild(tooltip)
    // brain-extension: same for the controls row — `removeAllChildren` on
    // the next render would clear it, but a cleanup-without-rerender (e.g.
    // global-graph hideOnEscape) needs the explicit removal so the chip
    // row doesn't linger as a detached child.
    if (controlsEl && controlsEl.parentElement) {
      controlsEl.parentElement.removeChild(controlsEl)
    }
    if (workbenchRail && workbenchRail.parentElement) {
      workbenchRail.parentElement.removeChild(workbenchRail)
    }
    if (workbenchCanvasPane && workbenchCanvasPane.parentElement) {
      workbenchCanvasPane.parentElement.removeChild(workbenchCanvasPane)
    }
    if (workbenchInspector && workbenchInspector.parentElement) {
      workbenchInspector.parentElement.removeChild(workbenchInspector)
    }
    graph.classList.remove("brain-graph-workbench-shell")
    app.destroy()
  }
  // brain-extension: write the live cleanup back into the shared ref so a
  // chip-driven rerender (which mutates `cleanupRef.current` mid-render)
  // and the cleanup arrays in the nav handler both reach the latest
  // teardown. The nav handler's cleanup-array entry is `() => ref.current()`,
  // so this assignment is what makes "dispose this graph" do the right
  // thing after any number of rerenders.
  cleanupRef.current = cleanup
  return cleanup
  } finally {
    // brain: closing brace for the renderGraph-wide try/finally that
    // guards `chipRerenderBusy`. Always clears the flag regardless of
    // throw/return path so a render error doesn't leave the graph
    // permanently locked out of chip toggles.
    chipRerenderBusy = false
  }
}

let localGraphCleanups: (() => void)[] = []
let globalGraphCleanups: (() => void)[] = []
// brain-extension: separate cleanup bucket for the fullscreen-local-graph
// modal so opening the global modal afterwards doesn't tear down its Pixi
// app, and vice versa. Both modals share the same Esc handler shape, so
// each one's hide function only clears its own bucket.
let localFullscreenCleanups: (() => void)[] = []
let workbenchGraphCleanups: (() => void)[] = []

function cleanupLocalGraphs() {
  for (const cleanup of localGraphCleanups) {
    cleanup()
  }
  localGraphCleanups = []
}

function cleanupGlobalGraphs() {
  for (const cleanup of globalGraphCleanups) {
    cleanup()
  }
  globalGraphCleanups = []
}

function cleanupLocalFullscreenGraphs() {
  for (const cleanup of localFullscreenCleanups) {
    cleanup()
  }
  localFullscreenCleanups = []
}

function cleanupWorkbenchGraphs() {
  for (const cleanup of workbenchGraphCleanups) {
    cleanup()
  }
  workbenchGraphCleanups = []
}

document.addEventListener("nav", async (e: CustomEventMap["nav"]) => {
  const slug = e.detail.url
  addToVisited(simplifySlug(slug))
  // brain-extension: search query is per-page — clear it on every SPA
  // navigation so a query typed on page A doesn't carry over to page B.
  // Chip filters intentionally persist across nav (they're a deliberate
  // user-applied lens), so they live on at module scope without a reset.
  currentSearchQuery = ""

  async function renderLocalGraph() {
    cleanupLocalGraphs()
    const localGraphContainers = document.getElementsByClassName("graph-container")
    for (const container of localGraphContainers) {
      // brain-extension: each container gets its own cleanup ref. The array
      // entry resolves through the ref, so chip-driven rerenders that mutate
      // `ref.current` still get torn down correctly on next nav.
      const ref: CleanupRef = { current: () => {} }
      await renderGraph(container as HTMLElement, slug, ref)
      localGraphCleanups.push(() => ref.current())
    }
  }

  await renderLocalGraph()
  const handleThemeChange = () => {
    void renderLocalGraph()
  }

  document.addEventListener("themechange", handleThemeChange)
  window.addCleanup(() => {
    document.removeEventListener("themechange", handleThemeChange)
  })

  const containers = [...document.getElementsByClassName("global-graph-outer")] as HTMLElement[]
  // brain-extension: `stockMode` flag threads through to renderGraph so the
  // dot-grid `brain-stock-graph-icon` button can open the same global-graph
  // modal but with every renderer extension disabled. The default-false branch
  // (called from the brain-customized globe icon and the Cmd/Ctrl-G shortcut)
  // is functionally identical to the pre-stockMode behavior.
  async function renderGlobalGraph(stockMode = false) {
    const slug = getFullSlug(window)
    for (const container of containers) {
      container.classList.add("active")
      const sidebar = container.closest(".sidebar") as HTMLElement
      if (sidebar) {
        sidebar.style.zIndex = "1"
      }

      const graphContainer = container.querySelector(".global-graph-container") as HTMLElement
      registerEscapeHandler(container, hideGlobalGraph)
      if (graphContainer) {
        // brain-extension: same cleanup-ref pattern as the local graph so
        // chip-driven rerenders inside the global modal stay disposable.
        const ref: CleanupRef = { current: () => {} }
        await renderGraph(graphContainer, slug, ref, stockMode)
        globalGraphCleanups.push(() => ref.current())
      }
    }
  }

  function hideGlobalGraph() {
    cleanupGlobalGraphs()
    for (const container of containers) {
      container.classList.remove("active")
      const sidebar = container.closest(".sidebar") as HTMLElement
      if (sidebar) {
        sidebar.style.zIndex = ""
      }
    }
  }

  // brain-extension: fullscreen-LOCAL-graph modal. Uses its own outer
  // container (`.local-graph-outer`) so it can co-exist with the global
  // modal's open/close state without sharing cleanup buckets. The inner
  // `.local-graph-container` carries a data-cfg that's a clone of the
  // sidebar's localGraph config + `filterChips: ["tier", "source"]`
  // forced on, so depth=1 (current page + neighbours) survives but the
  // user gets the same search + chip rail as the global modal.
  const localFullscreenContainers = [
    ...document.getElementsByClassName("local-graph-outer"),
  ] as HTMLElement[]
  const workbenchContainers = [
    ...document.getElementsByClassName("brain-graph-workbench-outer"),
  ] as HTMLElement[]

  async function renderLocalFullscreenGraph() {
    const slug = getFullSlug(window)
    for (const container of localFullscreenContainers) {
      container.classList.add("active")
      const sidebar = container.closest(".sidebar") as HTMLElement
      if (sidebar) {
        sidebar.style.zIndex = "1"
      }
      const graphContainer = container.querySelector(
        ".local-graph-container",
      ) as HTMLElement
      registerEscapeHandler(container, hideLocalFullscreenGraph)
      if (graphContainer) {
        const ref: CleanupRef = { current: () => {} }
        await renderGraph(graphContainer, slug, ref)
        localFullscreenCleanups.push(() => ref.current())
      }
    }
  }

  function hideLocalFullscreenGraph() {
    cleanupLocalFullscreenGraphs()
    for (const container of localFullscreenContainers) {
      container.classList.remove("active")
      const sidebar = container.closest(".sidebar") as HTMLElement
      if (sidebar) {
        sidebar.style.zIndex = ""
      }
    }
  }

  async function renderWorkbenchGraph() {
    const slug = getFullSlug(window)
    for (const container of workbenchContainers) {
      container.classList.add("active")
      const sidebar = container.closest(".sidebar") as HTMLElement
      if (sidebar) {
        sidebar.style.zIndex = "1"
      }
      const graphContainer = container.querySelector(
        ".brain-graph-workbench-container",
      ) as HTMLElement
      registerEscapeHandler(container, hideWorkbenchGraph)
      if (graphContainer) {
        const ref: CleanupRef = { current: () => {} }
        await renderGraph(graphContainer, slug, ref)
        workbenchGraphCleanups.push(() => ref.current())
      }
    }
  }

  function hideWorkbenchGraph() {
    cleanupWorkbenchGraphs()
    for (const container of workbenchContainers) {
      container.classList.remove("active")
      const sidebar = container.closest(".sidebar") as HTMLElement
      if (sidebar) {
        sidebar.style.zIndex = ""
      }
    }
  }

  async function shortcutHandler(e: HTMLElementEventMap["keydown"]) {
    if (e.key === "g" && (e.ctrlKey || e.metaKey) && !e.shiftKey) {
      e.preventDefault()
      // brain: Cmd/Ctrl+G drives the fullscreen-LOCAL-graph modal (current
      // page + its neighbourhood at depth=1, with search + filter chips).
      // The global graph still has its own globe/dot-grid icons in the
      // sidebar; the keyboard shortcut intentionally addresses the more
      // common "show me what THIS page is connected to" intent.
      const anyLocalFullscreenOpen = localFullscreenContainers.some((c) =>
        c.classList.contains("active"),
      )
      anyLocalFullscreenOpen ? hideLocalFullscreenGraph() : renderLocalFullscreenGraph()
    }
  }

  const containerIcons = document.getElementsByClassName("global-graph-icon")
  // brain: wrap the renderGlobalGraph call so the click event isn't passed
  // through as the stockMode argument (event objects are truthy → would
  // accidentally enable stockMode for the brain-customized globe icon too).
  const handleGlobalGraphClick = () => {
    void renderGlobalGraph(false)
  }
  Array.from(containerIcons).forEach((icon) => {
    icon.addEventListener("click", handleGlobalGraphClick)
    window.addCleanup(() => icon.removeEventListener("click", handleGlobalGraphClick))
  })

  // brain-extension: the dot-grid affordance opens the same global-graph modal
  // but with stockMode=true — every renderer extension is suppressed so the
  // user sees the corpus with stock-Quartz visual semantics.
  const stockGraphIcons = document.getElementsByClassName("brain-stock-graph-icon")
  const handleStockGraphClick = () => {
    void renderGlobalGraph(true)
  }
  Array.from(stockGraphIcons).forEach((icon) => {
    icon.addEventListener("click", handleStockGraphClick)
    window.addCleanup(() => icon.removeEventListener("click", handleStockGraphClick))
  })

  // brain-extension: the maximize affordance opens the fullscreen-local-graph
  // modal — same scope as the sidebar's inline local graph (depth=1) but
  // rendered into the centered modal panel with search + tier/source chips
  // on. Same handler shape as the other two icons.
  const localFullscreenIcons = document.getElementsByClassName(
    "local-graph-fullscreen-icon",
  )
  const handleLocalFullscreenClick = () => {
    void renderLocalFullscreenGraph()
  }
  Array.from(localFullscreenIcons).forEach((icon) => {
    icon.addEventListener("click", handleLocalFullscreenClick)
    window.addCleanup(() =>
      icon.removeEventListener("click", handleLocalFullscreenClick),
    )
  })

  const workbenchIcons = document.getElementsByClassName("brain-graph-workbench-icon")
  const handleWorkbenchClick = () => {
    void renderWorkbenchGraph()
  }
  Array.from(workbenchIcons).forEach((icon) => {
    icon.addEventListener("click", handleWorkbenchClick)
    window.addCleanup(() => icon.removeEventListener("click", handleWorkbenchClick))
  })

  document.addEventListener("keydown", shortcutHandler)
  window.addCleanup(() => {
    document.removeEventListener("keydown", shortcutHandler)
    cleanupLocalGraphs()
    cleanupGlobalGraphs()
    cleanupLocalFullscreenGraphs()
    cleanupWorkbenchGraphs()
  })
})
