// Sample Quartz v4 layout for brain vaults.
//
// This file is a TEMPLATE. Copy it into your Quartz workspace as
// `<vault>/.quartz/quartz.layout.ts` after cloning Quartz with
// `git clone https://github.com/jackyzha0/quartz.git <vault>/.quartz`.
// It does NOT compile or run from the brain repo itself; the imports
// below resolve against the dependencies Quartz pulls into the cloned
// workspace via `npm install`, not against any package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz version
// renames a component or its option shape, pull the latest layout from
// https://quartz.jzhao.xyz/ and re-apply the brain-specific tweaks
// flagged below with `// brain:` comments.
//
// The brain customizations layer cleanly on top of stock Quartz: the
// graph component is moved into the right sidebar as a *local* graph
// only, while the global graph stays reachable via Quartz's built-in
// Cmd/Ctrl+G modal (wired up by `shortcutHandler` in graph.inline.ts).
// Several option keys below are brain-only extensions to Quartz's
// `D3Config` — those are clearly marked so a future maintainer (or
// somebody running a stock Quartz build) knows they are not upstream.

import { PageLayout, SharedLayout } from "./quartz/cfg"
import * as Component from "./quartz/components"
import { FileTrieNode } from "./quartz/util/fileTrie"

// brain (People Hub, 2026-05-07): pin the `people/` directory to the
// top of the Explorer tree so Ali can reach the per-person hub pages
// from anywhere on the site in one click. Mirrors the upstream sort
// rule (folders before files, then alpha) but with a deterministic
// override for the `people` slug-segment that bumps it ahead of every
// other folder. Not a contractual schema field — Quartz options accept
// any function as `sortFn`, and the inline script serializes it via
// `.toString()` for client-side execution. We pass the same function
// to every Explorer slot in the layout (default content + list
// pages) so the pin behavior is consistent across page types.
const PINNED_EXPLORER_FOLDER_SLUG = "people"

// NOTE: this comparator is serialized via `.toString()` and re-evaluated
// in the browser, where module-level identifiers are not in scope. The
// pinned slug literal must therefore be inlined inside the function
// body — keep `PINNED_EXPLORER_FOLDER_SLUG` in sync with the literals
// below (the static test in `tests/test_quartz_people_hub_static.py`
// pins both halves).
function explorerSortPinningPeople(a: FileTrieNode, b: FileTrieNode): number {
  // Pin `people/` ahead of every other folder. The slug-segment is the
  // last path-component of the trie node; a pinned folder always wins
  // over any non-pinned sibling regardless of type. When neither is the
  // pinned folder, fall through to upstream's "folders first, alpha"
  // rule so the rest of the tree behaves identically to stock Quartz.
  if (a.isFolder && a.slugSegment === "people") return -1
  if (b.isFolder && b.slugSegment === "people") return 1
  if ((!a.isFolder && !b.isFolder) || (a.isFolder && b.isFolder)) {
    return a.displayName.localeCompare(b.displayName, undefined, {
      numeric: true,
      sensitivity: "base",
    })
  }
  return !a.isFolder && b.isFolder ? 1 : -1
}

// components shared across all pages
//
// brain: Lane C redesign — `CommandPalette` lives in `afterBody` so
// the modal markup ships with every page exactly once (the script
// listener at `Cmd/Ctrl+P` is then reachable from any route). The
// component is hidden by default (sets the `hidden` attribute on its
// root `<div>`) and only revealed when the inline script removes the
// attribute on a Cmd-P press. See
// `quartz/components/CommandPalette.tsx` for the markup and
// `quartz/components/scripts/commandPalette.inline.ts` for the
// runtime open/close + fuzzy search logic.
export const sharedPageComponents: SharedLayout = {
  head: Component.Head(),
  header: [],
  afterBody: [Component.CommandPalette()],
  // brain: single GitHub link to the repo. Was previously a placeholder
  // pair of "GitHub: #" + "Source: #" — both pointed nowhere and were
  // redundant against each other.
  footer: Component.Footer({
    links: { GitHub: "https://github.com/mshtawythug/second-brain" },
  }),
}

// components for pages that display a single page (e.g. a single note)
export const defaultContentPageLayout: PageLayout = {
  beforeBody: [
    Component.ConditionalRender({
      component: Component.Breadcrumbs(),
      condition: (page) => page.fileData.slug !== "index",
    }),
    Component.ArticleTitle(),
    Component.ContentMeta(),
    Component.TagList(),
    // brain (Wave Q2-SUMMARY-WIKI): render the Q1-D auto-summary as a
    // TL;DR block above the article body. Sits below the meta strip
    // (title, tags, dates) and above Breadcrumbs's natural reading
    // position — once the reader has identified the page, the lede
    // gives them the gist before the body. Renders `null` when
    // `fileData.frontmatter.summary` is missing or non-string, so
    // unenriched docs never see an empty aside.
    Component.SummaryLede(),
  ],
  left: [
    Component.PageTitle(),
    Component.MobileOnly(Component.Spacer()),
    Component.Flex({
      components: [
        {
          Component: Component.Search(),
          grow: true,
        },
        { Component: Component.Darkmode() },
        { Component: Component.ReaderMode() },
      ],
    }),
    Component.Explorer({ sortFn: explorerSortPinningPeople }),
  ],
  right: [
    // brain: render the local graph inline in the right sidebar so it is
    // always visible while reading. The global graph is intentionally
    // omitted from this layout — Quartz still wires up the Cmd/Ctrl+G
    // modal via `shortcutHandler` in graph.inline.ts, which opens the
    // global graph on demand.
    Component.Graph({
      localGraph: {
        depth: 1,
        scale: 1.1,
        focusOnHover: true,
        enableRadial: false,
        // brain-extension: SHOW orphans — ingested transcripts (krisp
        // especially) often have no wiki-links and would otherwise vanish
        // from view, undercounting the corpus.
        hideOrphans: false,
        // brain-extension: collapse #tag nodes into chips on the side of
        // the graph instead of rendering them as first-class nodes that
        // pull every tagged doc into a hairball.
        hideTagNodes: true,
        // brain-extension: dashed/translucent styling for derived edges
        // (the `_ingested/` fence Phase D writes), so they read as
        // softer "evidence" links rather than authored wiki-links.
        derivedEdgeStyle: { dash: [4, 3], width: 0.5, alpha: 0.4 },
        // brain-extension: scale node radius by recency of last edit so
        // freshly-touched notes pop visually.
        recencySizing: true,
        // brain-extension: in-graph search box that filters visible
        // nodes by title substring.
        searchEnabled: true,
        // brain-extension: tier/source chips are deliberately empty for
        // the local (sidebar) graph. At depth=1 there are usually only a
        // handful of nodes — slicing 5 nodes by tier or source nearly
        // always empties the view, and the filters eat 60+ vertical
        // pixels of sidebar space that's better spent on the canvas.
        // The full filter chip rail still appears in the global modal
        // graph below, where it actually slices a useful number of
        // nodes.
        filterChips: [],
        // brain-extension: looser label cap for the sidebar panel — at
        // depth=1 there are typically only a handful of nodes, so 30
        // chars fits a full short-thread reply ("Apr 28 — Re: Ali Sarkis
        // × vendor-ev") without crowding. The fullscreen modal (Graph.tsx
        // override) and the global graph (defaulted to 10) both keep
        // the tighter cap since their dense canvases would otherwise
        // wall-of-text.
        labelMaxLength: 30,
      },
      globalGraph: {
        depth: -1,
        scale: 0.9,
        // brain-extension: corpus-scale Obsidian-grade tunings — even
        // bigger spread than the local-fullscreen modal because the
        // node count is ~30× higher (1000+ docs vs ~30 neighbours at
        // depth=1). Without this the central cluster compresses into
        // an unreadable blob.
        repelForce: 4.5,
        linkDistance: 280,
        centerForce: 0.05,
        focusOnHover: true,
        enableRadial: true,
        // brain-extension: hide orphans (degree-0 nodes after the chip
        // + tag passes) by default — at corpus scale the floating
        // unconnected dots add visual noise without conveying
        // structure. Users who want to see them can flip the
        // "Show unconnected" chip in the controls rail; the toggle
        // persists across SPA navigation within the session.
        hideOrphans: true,
        // brain-extension: collapse #tag nodes into chips so a single
        // popular tag does not pull every tagged doc into the center.
        hideTagNodes: true,
        // brain-extension: dashed/translucent styling for derived edges
        // (the `_ingested/` fence Phase D writes), so they read as
        // softer "evidence" links rather than authored wiki-links.
        derivedEdgeStyle: { dash: [4, 3], width: 0.5, alpha: 0.4 },
        // brain-extension: in-graph search box that filters visible
        // nodes by title substring. Note: recencySizing is intentionally
        // omitted here — at depth: -1 the radius variance reads as
        // noise rather than signal.
        searchEnabled: true,
        // brain-extension: filter chips wired to frontmatter facets;
        // tier = vault-tier (a/b/c), source = ingest source (krisp,
        // slack, gmail, manual).
        filterChips: ["tier", "source"],
        // brain-extension: 2× node radius — matches the fullscreen-local
        // modal's sizing so the brain-customized globe view reads with
        // comfortable, glance-sized dots. The dot-grid (stock-mode) view
        // strips this back to 1× via the stockMode override in
        // graph.inline.ts so that affordance keeps stock-Quartz visuals.
        nodeRadiusMultiplier: 2,
        // brain-extension: same steeper degree → radius curve as the
        // local-fullscreen view so corpus-level hubs (COMPANY_REDACTED,
        // COMPANY_REDACTED Hub, COMPANY_REDACTED) visibly dominate vs leaves.
        nodeRadiusGrowthExponent: 0.85,
        // brain-extension: fade wiki edges to 25% — slightly fainter
        // than local-fullscreen because at 1000+ nodes the edge density
        // is much higher and would otherwise dominate the canvas.
        wikiEdgeBaseAlpha: 0.25,
        // brain-extension: bump opacityScale so labels stay visible at
        // the wide-spread fit zoom (which lands at k < 1 and would
        // otherwise clamp every label to alpha 0).
        opacityScale: 6,
        // brain-extension: bump label size from the 0.6 default to 1.0
        // — at 80vw × 80vh the canvas has plenty of room and the sidebar
        // panel's small-text constraint doesn't apply. Matches the
        // local-fullscreen modal's fontSize override in Graph.tsx so the
        // two fullscreen views read with consistent typography.
        fontSize: 1.0,
        // brain-extension: any node with ≥ 12 incident links skips
        // truncation in the global view. Was 25 — too restrictive
        // (COMPANY_REDACTED Hub, Interview Prep, COMPANY_REDACTED only had ~10–20 links
        // each, so they got truncated despite being clearly hubs).
        // Lower threshold lets the structural anchors of the corpus
        // read at full length without flooding the canvas with text.
        hubLabelThreshold: 12,
        // brain-extension: non-hub leaf labels in the global view show
        // up to 20 chars (was 10, default). At depth=-1 the cluster is
        // already broken into named regions by the hub labels above, so
        // a longer leaf cap helps disambiguate adjacent nodes (e.g.
        // "Re: VP of Engineerin…" vs "Re: VP of Engineering — Eve…")
        // without restoring the wall-of-text problem.
        labelMaxLength: 20,
        // brain-extension: lift the radius ceiling further than the
        // local-fullscreen view (25 → 30) because at corpus scale the
        // dynamic range between leaves (1 link) and mega-hubs (200+
        // links) is much wider — letting hubs render to ~60px gives
        // the visual hierarchy room to breathe.
        nodeRadiusCeiling: 30,
        // brain-extension: 2× hub-label fontSize — slightly more
        // dramatic than local-fullscreen's 1.7× because at depth=-1
        // there are many more leaves and the size delta needs to be
        // bigger to read as a distinct tier.
        hubLabelFontMultiplier: 2.0,
      },
      workbenchGraph: {
        depth: -1,
        scale: 0.9,
        repelForce: 4.5,
        linkDistance: 280,
        centerForce: 0.05,
        focusOnHover: true,
        enableRadial: true,
        hideOrphans: false,
        hideTagNodes: true,
        derivedEdgeStyle: { dash: [4, 3], width: 0.5, alpha: 0.4 },
        searchEnabled: true,
        filterChips: ["tier", "source"],
        diagnosticWorkbench: true,
        nodeRadiusMultiplier: 2,
        nodeRadiusGrowthExponent: 0.85,
        wikiEdgeBaseAlpha: 0.25,
        opacityScale: 6,
        fontSize: 1.0,
        hubLabelThreshold: 8,
        labelMaxLength: 24,
        nodeRadiusCeiling: 30,
        hubLabelFontMultiplier: 1.8,
      },
    }),
    // brain: ToC sits directly under the graph (page-utility outranks
    // exploration). Stock Quartz styles the ToC as a flex column with
    // `flex: 0 0.5 auto` and a self-scrolling `<ul>` — when RelatedDocs
    // ran above it the related list (often 12+ rows on hub pages) ate
    // the available column height and squeezed the ToC's inner list to
    // ~0px, making the entries unreachable. Putting the ToC first lets
    // it claim its natural height before the related list grows.
    Component.DesktopOnly(Component.TableOfContents()),
    Component.RelatedDocs(),
    Component.Backlinks(),
  ],
}

// brain: the list-page layout is what Quartz's tag-page and folder-page
// emitters import (`quartz/plugins/emitters/{tagPage,folderPage}.tsx`).
// Without this export the build fails before producing a single page.
// Mirrors upstream's structure: same left sidebar (search/explorer/
// darkmode), no right sidebar (the local graph is meaningless on
// aggregate index pages).
export const defaultListPageLayout: PageLayout = {
  beforeBody: [Component.Breadcrumbs(), Component.ArticleTitle(), Component.ContentMeta()],
  left: [
    Component.PageTitle(),
    Component.MobileOnly(Component.Spacer()),
    Component.Flex({
      components: [
        {
          Component: Component.Search(),
          grow: true,
        },
        { Component: Component.Darkmode() },
      ],
    }),
    Component.Explorer({ sortFn: explorerSortPinningPeople }),
  ],
  right: [],
}
