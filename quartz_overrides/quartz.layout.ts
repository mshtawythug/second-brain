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

export const sharedPageComponents: SharedLayout = {
  head: Component.Head(),
  header: [],
  // brain: replace before deploy.
  footer: Component.Footer({ links: { GitHub: "#", "Source": "#" } }),
}

export const defaultContentPageLayout: PageLayout = {
  beforeBody: [
    Component.Breadcrumbs(),
    Component.ArticleTitle(),
    Component.ContentMeta(),
    Component.TagList(),
  ],
  left: [
    Component.PageTitle(),
    Component.MobileOnly(Component.Spacer()),
    Component.Search(),
    Component.Darkmode(),
    Component.DesktopOnly(Component.Explorer()),
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
        // brain-extension: hide nodes with no edges so the local graph
        // does not clutter with isolated stubs.
        hideOrphans: true,
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
        // brain-extension: filter chips wired to frontmatter facets;
        // tier = vault-tier (a/b/c), source = ingest source (krisp,
        // slack, gmail, manual).
        filterChips: ["tier", "source"],
      },
      globalGraph: {
        depth: -1,
        scale: 0.9,
        repelForce: 0.8,
        linkDistance: 50,
        focusOnHover: true,
        enableRadial: true,
        // brain-extension: hide nodes with no edges. At depth: -1 the
        // hairball is bad enough without isolated stubs piling on.
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
      },
    }),
    Component.DesktopOnly(Component.TableOfContents()),
    Component.Backlinks(),
  ],
}
