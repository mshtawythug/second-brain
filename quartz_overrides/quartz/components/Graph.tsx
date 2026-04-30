// Custom Quartz v4 Graph component for brain vaults.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/Graph.tsx` by `brain vault
// render --overlay`, FULL-REPLACING upstream's stock Graph.tsx. It
// does NOT compile or run from the brain repo itself; the imports
// below resolve against the dependencies Quartz pulls into the
// cloned workspace via `npm install`, not against any package
// brain ships.
//
// Tested against Quartz v4.5.x (April 2026). When upstream churns,
// pull the latest Graph.tsx from
// https://github.com/jackyzha0/quartz/blob/v4/quartz/components/Graph.tsx
// and re-apply the brain tweaks below — `// brain:` for
// value/structural choices on upstream-supported fields, and
// `// brain-extension:` for keys/types that don't exist in stock
// Quartz. To enumerate every delta:
//
//   grep -n "brain:" Graph.tsx
//   grep -n "brain-extension:" Graph.tsx
//
// Strategy — full replacement (Option A): the brain modifications
// touch the renderer (color resolution, edge styling, recency
// sizing, filter pass) deeply enough that wrapping the upstream
// component is awkward. We vendor it verbatim and inline the
// brain deltas with `// brain:` comments so a future
// `diff -u <upstream> <ours>` is a usable upgrade tool.
//
// Responsibility (CLAUDE.md rule 8): this file owns the Preact
// component wrapper and the public `D3Config` type. Rendering
// logic lives in `scripts/graph.inline.ts`; the brain palette
// lives in `../styles/graph.scss`. Don't let them spill into each
// other.

import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
// @ts-ignore — esbuild-loader resolves .scss/.ts to bundled strings; TS doesn't know.
import script from "./scripts/graph.inline"
// @ts-ignore
import style from "./styles/graph.scss"
// brain: import the brain palette as a sibling stylesheet so it lands in the same
// component-css bundle as upstream's graph styles. Path resolves to
// `quartz/styles/graph.scss` after the overlay copy.
// @ts-ignore
import brainStyle from "../styles/graph.scss"
import { i18n } from "../i18n"
import { classNames } from "../util/lang"

// brain-extension: dashed/translucent stroke style applied to derived edges (Phase D
// `_ingested/` fence output). Width / dash pattern / alpha all tunable from
// `quartz.layout.ts` so visuals can be dialed in without code edits.
export interface DerivedEdgeStyle {
  dash: [number, number]
  width: number
  alpha: number
}

// brain: defining D3Config locally (rather than declaration-merging upstream) keeps
// the brain-extension surface in one file. `scripts/graph.inline.ts` imports this
// exported type to read the renderer config off the `data-cfg` attribute.
export interface D3Config {
  drag: boolean
  zoom: boolean
  depth: number
  scale: number
  repelForce: number
  centerForce: number
  linkDistance: number
  fontSize: number
  opacityScale: number
  removeTags: string[]
  showTags: boolean
  focusOnHover?: boolean
  enableRadial?: boolean
  // brain-extension: per-tier color overrides. Maps a doc's frontmatter `tier` value
  // to a CSS variable name (defined in `quartz/styles/graph.scss`). Falls through to
  // `sourceColors` when a tier isn't matched, then to `--gray`.
  tierColors?: Record<string, string>
  // brain-extension: per-source color overrides, keyed by frontmatter `source`. Used
  // as fallback when `tierColors` doesn't match.
  sourceColors?: Record<string, string>
  // brain-extension: drop nodes with degree 0 from the simulation. Stricter than
  // upstream's "render isolated nodes anyway" default.
  hideOrphans?: boolean
  // brain-extension: drop `tags/<tag>` nodes from the simulation. Stricter than
  // upstream's `showTags: false`, which only de-renders tag nodes (they still pull
  // every tagged doc into the neighbourhood for graph-traversal).
  hideTagNodes?: boolean
  // brain-extension: list of frontmatter keys; nodes whose contentIndex entry has
  // any matching truthy key are dropped. With the current contentIndex emitter
  // only `tier` / `source` are surfaced; matching against other keys (e.g.
  // `index`, `moc`) requires the emitter to widen its frontmatter passthrough.
  hideByFrontmatter?: string[]
  // brain-extension: dashed/translucent style for derived edges (Phase D fence
  // output). Renderer ignores when absent — wiki edges still get upstream's solid
  // stroke either way.
  derivedEdgeStyle?: DerivedEdgeStyle
  // brain-extension: scale node radius by recency of last edit so freshly-touched
  // notes pop visually. Decay window pinned to one year in the renderer; final
  // radius clamped to [1, 4].
  recencySizing?: boolean
  // brain-extension: render an in-graph search input. Type-only here; the runtime
  // wiring (input element, debounced filter, SPA-nav-on-enter) lands with the
  // upcoming search-and-filter customization.
  searchEnabled?: boolean
  // brain-extension: render filter chips for tier and/or source. Type-only here;
  // the runtime wiring lands with the upcoming search-and-filter customization.
  filterChips?: Array<"tier" | "source">
}

interface GraphOptions {
  localGraph: Partial<D3Config> | undefined
  globalGraph: Partial<D3Config> | undefined
}

// brain-extension: shared default mapping for tier / source palettes. Pulled into a
// const so both `localGraph` and `globalGraph` defaults reference the same source of
// truth — renaming a CSS variable in `graph.scss` only requires the change here, not
// in two places.
const defaultTierColors: Record<string, string> = {
  vault: "--brain-tier-vault",
  ingested: "--brain-tier-ingested",
}
const defaultSourceColors: Record<string, string> = {
  krisp: "--brain-source-krisp",
  slack: "--brain-source-slack",
  gmail: "--brain-source-gmail",
  manual: "--brain-source-manual",
}

const defaultOptions: GraphOptions = {
  localGraph: {
    drag: true,
    zoom: true,
    depth: 1,
    scale: 1.1,
    repelForce: 0.5,
    centerForce: 0.3,
    linkDistance: 30,
    fontSize: 0.6,
    opacityScale: 1,
    showTags: true,
    removeTags: [],
    focusOnHover: false,
    enableRadial: false,
    // brain-extension: defaults so a layout that omits these still gets brain
    // coloring out of the box. Layout-supplied values shallow-merge over these.
    tierColors: defaultTierColors,
    sourceColors: defaultSourceColors,
  },
  globalGraph: {
    drag: true,
    zoom: true,
    depth: -1,
    scale: 0.9,
    repelForce: 0.5,
    centerForce: 0.2,
    linkDistance: 30,
    fontSize: 0.6,
    opacityScale: 1,
    showTags: true,
    removeTags: [],
    focusOnHover: true,
    enableRadial: true,
    // brain-extension: same defaults as the local graph; layout config in
    // `quartz.layout.ts` can override either tier or source per-graph.
    tierColors: defaultTierColors,
    sourceColors: defaultSourceColors,
  },
}

export default ((opts?: Partial<GraphOptions>) => {
  const Graph: QuartzComponent = ({ displayClass, cfg }: QuartzComponentProps) => {
    // brain: defensive guard for catastrophic component-instantiation failure (cfg
    // missing or non-object). The brain-extension keys are all optional, so missing
    // or wrong-shape values fall through to gray-default in the renderer; this
    // throw is for the case where the overlay was misapplied and the component is
    // being constructed without Quartz's standard cfg context at all.
    if (!cfg || typeof cfg !== "object") {
      throw new Error(
        "brain Graph component instantiated without cfg — was the overlay applied to the right Quartz workspace?",
      )
    }
    const localGraph = { ...defaultOptions.localGraph, ...opts?.localGraph }
    const globalGraph = { ...defaultOptions.globalGraph, ...opts?.globalGraph }
    return (
      <div class={classNames(displayClass, "graph")}>
        <h3>{i18n(cfg.locale).components.graph.title}</h3>
        <div class="graph-outer">
          <div class="graph-container" data-cfg={JSON.stringify(localGraph)}></div>
          {/* brain-extension: second affordance that opens the global graph in
              "stock mode" — every brain renderer extension (tier/source colors,
              derived-edge styling, recency sizing, search input, filter chips,
              orphan/tag-node hiding) is suppressed so the user sees the corpus
              with stock-Quartz visual semantics. Renders to the left of the
              brain-customized globe icon; click handler in graph.inline.ts. */}
          <button class="brain-stock-graph-icon" aria-label="Global Graph (stock view)">
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="currentColor"
              xmlSpace="preserve"
            >
              <circle cx="5" cy="5" r="1.6" />
              <circle cx="12" cy="5" r="1.6" />
              <circle cx="19" cy="5" r="1.6" />
              <circle cx="5" cy="12" r="1.6" />
              <circle cx="12" cy="12" r="1.6" />
              <circle cx="19" cy="12" r="1.6" />
              <circle cx="5" cy="19" r="1.6" />
              <circle cx="12" cy="19" r="1.6" />
              <circle cx="19" cy="19" r="1.6" />
            </svg>
          </button>
          <button class="global-graph-icon" aria-label="Global Graph">
            <svg
              version="1.1"
              xmlns="http://www.w3.org/2000/svg"
              xmlnsXlink="http://www.w3.org/1999/xlink"
              x="0px"
              y="0px"
              viewBox="0 0 55 55"
              fill="currentColor"
              xmlSpace="preserve"
            >
              <path
                d="M49,0c-3.309,0-6,2.691-6,6c0,1.035,0.263,2.009,0.726,2.86l-9.829,9.829C32.542,17.634,30.846,17,29,17
                s-3.542,0.634-4.898,1.688l-7.669-7.669C16.785,10.424,17,9.74,17,9c0-2.206-1.794-4-4-4S9,6.794,9,9s1.794,4,4,4
                c0.74,0,1.424-0.215,2.019-0.567l7.669,7.669C21.634,21.458,21,23.154,21,25s0.634,3.542,1.688,4.897L10.024,42.562
                C8.958,41.595,7.549,41,6,41c-3.309,0-6,2.691-6,6s2.691,6,6,6s6-2.691,6-6c0-1.035-0.263-2.009-0.726-2.86l12.829-12.829
                c1.106,0.86,2.44,1.436,3.898,1.619v10.16c-2.833,0.478-5,2.942-5,5.91c0,3.309,2.691,6,6,6s6-2.691,6-6c0-2.967-2.167-5.431-5-5.91
                v-10.16c1.458-0.183,2.792-0.759,3.898-1.619l7.669,7.669C41.215,39.576,41,40.26,41,41c0,2.206,1.794,4,4,4s4-1.794,4-4
                s-1.794-4-4-4c-0.74,0-1.424,0.215-2.019,0.567l-7.669-7.669C36.366,28.542,37,26.846,37,25s-0.634-3.542-1.688-4.897l9.665-9.665
                C46.042,11.405,47.451,12,49,12c3.309,0,6-2.691,6-6S52.309,0,49,0z M11,9c0-1.103,0.897-2,2-2s2,0.897,2,2s-0.897,2-2,2
                S11,10.103,11,9z M6,51c-2.206,0-4-1.794-4-4s1.794-4,4-4s4,1.794,4,4S8.206,51,6,51z M33,49c0,2.206-1.794,4-4,4s-4-1.794-4-4
                s1.794-4,4-4S33,46.794,33,49z M29,31c-3.309,0-6-2.691-6-6s2.691-6,6-6s6,2.691,6,6S32.309,31,29,31z M47,41c0,1.103-0.897,2-2,2
                s-2-0.897-2-2s0.897-2,2-2S47,39.897,47,41z M49,10c-2.206,0-4-1.794-4-4s1.794-4,4-4s4,1.794,4,4S51.206,10,49,10z"
              />
            </svg>
          </button>
        </div>
        <div class="global-graph-outer">
          <div class="global-graph-container" data-cfg={JSON.stringify(globalGraph)}></div>
        </div>
      </div>
    )
  }

  // brain: concatenate upstream's component styles with the brain palette so both
  // ship through Quartz's component-css pipeline. Order doesn't matter — they
  // target disjoint selectors (`:root` palette vs `.graph` layout).
  Graph.css = style + "\n" + brainStyle
  Graph.afterDOMLoaded = script

  return Graph
}) satisfies QuartzComponentConstructor
