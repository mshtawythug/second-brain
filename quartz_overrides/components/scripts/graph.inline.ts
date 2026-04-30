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

async function renderGraph(graph: HTMLElement, fullSlug: FullSlug) {
  const slug = simplifySlug(fullSlug)
  const visited = getVisited()
  removeAllChildren(graph)

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
    // to upstream-equivalent behavior. `searchEnabled` and `filterChips` are
    // type-only stubs and are intentionally not destructured here — the upcoming
    // search-and-filter customization will add them when their runtime wiring
    // lands.
    tierColors,
    sourceColors,
    hideOrphans,
    hideTagNodes,
    hideByFrontmatter,
    derivedEdgeStyle,
    recencySizing,
  } = JSON.parse(graph.dataset["cfg"]!) as D3Config

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
  if (hideOrphans) {
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

  const width = graph.offsetWidth
  const height = Math.max(graph.offsetHeight, 250)

  // we virtualize the simulation and use pixi to actually render it
  const simulation: Simulation<NodeData, LinkData> = forceSimulation<NodeData>(graphData.nodes)
    .force("charge", forceManyBody().strength(-100 * repelForce))
    .force("center", forceCenter().strength(centerForce))
    .force("link", forceLink(graphData.links).distance(linkDistance))
    .force("collide", forceCollide<NodeData>((n) => nodeRadius(n)).iterations(3))

  const radius = (Math.min(width, height) / 2) * 0.8
  if (enableRadial) simulation.force("radial", forceRadial(radius).strength(0.2))

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
    const base = 2 + Math.sqrt(numLinks)
    // brain-extension: scale by recency when enabled; clamp final radius to
    // [1, 4] per spec.
    const multiplier =
      recencySizing && d.mtime !== null && d.mtime !== undefined
        ? recencyMultiplier(d.mtime)
        : 1
    return Math.min(4, Math.max(1, base * multiplier))
  }

  let hoveredNodeId: string | null = null
  let hoveredNeighbours: Set<string> = new Set()
  const linkRenderData: LinkRenderData[] = []
  const nodeRenderData: NodeRenderData[] = []
  function updateHoverInfo(newHoveredId: string | null) {
    hoveredNodeId = newHoveredId

    if (newHoveredId === null) {
      hoveredNeighbours = new Set()
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
      // wiki edges keep upstream's full alpha. Hover dimming layers on top so a
      // hovered-neighbour derived edge doesn't fade further than its base.
      const baseAlpha =
        l.simulationData.kind === "derived" && derivedEdgeStyle
          ? derivedEdgeStyle.alpha
          : 1
      let alpha = baseAlpha

      // if we are hovering over a node, we want to highlight the immediate neighbours
      // with full alpha and the rest with default alpha
      if (hoveredNodeId) {
        alpha = l.active ? baseAlpha : baseAlpha * 0.2
      }

      l.color = l.active ? computedStyleMap["--gray"] : computedStyleMap["--lightgray"]
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
    for (const n of nodeRenderData) {
      const nodeId = n.simulationData.id

      if (hoveredNodeId === nodeId) {
        tweenGroup.add(
          new Tweened<Text>(n.label).to(
            {
              alpha: 1,
              scale: { x: activeScale, y: activeScale },
            },
            100,
          ),
        )
      } else {
        tweenGroup.add(
          new Tweened<Text>(n.label).to(
            {
              alpha: n.label.alpha,
              scale: { x: defaultScale, y: defaultScale },
            },
            100,
          ),
        )
      }
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
      if (hoveredNodeId !== null && focusOnHover) {
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
  graph.appendChild(app.canvas)

  const stage = app.stage
  stage.interactive = false

  const labelsContainer = new Container<Text>({ zIndex: 3, isRenderGroup: true })
  const nodesContainer = new Container<Graphics>({ zIndex: 2, isRenderGroup: true })
  const linkContainer = new Container<Graphics>({ zIndex: 1, isRenderGroup: true })
  stage.addChild(nodesContainer, labelsContainer, linkContainer)

  for (const n of graphData.nodes) {
    const nodeId = n.id

    const label = new Text({
      interactive: false,
      eventMode: "none",
      text: n.text,
      alpha: 0,
      anchor: { x: 0.5, y: 1.2 },
      style: {
        fontSize: fontSize * 15,
        fill: computedStyleMap["--dark"],
        fontFamily: computedStyleMap["--bodyFont"],
      },
      resolution: window.devicePixelRatio * 4,
    })
    label.scale.set(1 / scale)

    let oldLabelOpacity = 0
    const isTagNode = nodeId.startsWith("tags/")
    const gfx = new Graphics({
      interactive: true,
      label: nodeId,
      eventMode: "static",
      hitArea: new Circle(0, 0, nodeRadius(n)),
      cursor: "pointer",
    })
      .circle(0, 0, nodeRadius(n))
      .fill({ color: isTagNode ? computedStyleMap["--light"] : color(n) })
      .on("pointerover", (e) => {
        updateHoverInfo(e.target.label)
        oldLabelOpacity = label.alpha
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
      color: color(n),
      alpha: 1,
      active: false,
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
    select<HTMLCanvasElement, NodeData>(app.canvas).call(
      zoom<HTMLCanvasElement, NodeData>()
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
          const activeNodes = nodeRenderData.filter((n) => n.active).flatMap((n) => n.label)

          for (const label of labelsContainer.children) {
            if (!activeNodes.includes(label)) {
              label.alpha = scaleOpacity
            }
          }
        }),
    )
  }

  let stopAnimation = false
  function animate(time: number) {
    if (stopAnimation) return
    for (const n of nodeRenderData) {
      const { x, y } = n.simulationData
      if (!x || !y) continue
      n.gfx.position.set(x + width / 2, y + height / 2)
      if (n.label) {
        n.label.position.set(x + width / 2, y + height / 2)
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
  return () => {
    stopAnimation = true
    // brain-extension: tear down the tooltip element so re-renders (theme change,
    // SPA nav) don't accumulate orphaned tooltips in the DOM.
    if (tooltip.parentElement) tooltip.parentElement.removeChild(tooltip)
    app.destroy()
  }
}

let localGraphCleanups: (() => void)[] = []
let globalGraphCleanups: (() => void)[] = []

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

document.addEventListener("nav", async (e: CustomEventMap["nav"]) => {
  const slug = e.detail.url
  addToVisited(simplifySlug(slug))

  async function renderLocalGraph() {
    cleanupLocalGraphs()
    const localGraphContainers = document.getElementsByClassName("graph-container")
    for (const container of localGraphContainers) {
      localGraphCleanups.push(await renderGraph(container as HTMLElement, slug))
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
  async function renderGlobalGraph() {
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
        globalGraphCleanups.push(await renderGraph(graphContainer, slug))
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

  async function shortcutHandler(e: HTMLElementEventMap["keydown"]) {
    if (e.key === "g" && (e.ctrlKey || e.metaKey) && !e.shiftKey) {
      e.preventDefault()
      const anyGlobalGraphOpen = containers.some((container) =>
        container.classList.contains("active"),
      )
      anyGlobalGraphOpen ? hideGlobalGraph() : renderGlobalGraph()
    }
  }

  const containerIcons = document.getElementsByClassName("global-graph-icon")
  Array.from(containerIcons).forEach((icon) => {
    icon.addEventListener("click", renderGlobalGraph)
    window.addCleanup(() => icon.removeEventListener("click", renderGlobalGraph))
  })

  document.addEventListener("keydown", shortcutHandler)
  window.addCleanup(() => {
    document.removeEventListener("keydown", shortcutHandler)
    cleanupLocalGraphs()
    cleanupGlobalGraphs()
  })
})
