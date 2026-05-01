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

// components shared across all pages
export const sharedPageComponents: SharedLayout = {
  head: Component.Head(),
  header: [],
  afterBody: [],
  // brain: replace before deploy.
  footer: Component.Footer({ links: { GitHub: "#", "Source": "#" } }),
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
    Component.Explorer(),
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
        // brain-extension: SHOW orphans on the global graph. The local
        // graph hides them (focused view at depth 1 — orphans are noise),
        // but global is the "I want to see everything" view. With
        // hideOrphans=true we were hiding 62/68 Krisp transcripts (they
        // have no wiki-links because they're raw transcripts), making
        // the corpus appear sparser than it is when filtered by source.
        hideOrphans: false,
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
    Component.Explorer(),
  ],
  right: [],
}
