// Sample Quartz v4 configuration for brain vaults.
//
// This file is a TEMPLATE. Copy it into your Quartz workspace as
// `<vault>/.quartz/quartz.config.ts` after cloning Quartz with
// `git clone https://github.com/jackyzha0/quartz.git <vault>/.quartz`.
// It does NOT compile or run from the brain repo itself; the imports
// below resolve against the dependencies Quartz pulls into the cloned
// workspace via `npm install`, not against any package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz version
// renames a plugin or its option shape, pull the latest config from
// https://quartz.jzhao.xyz/ and re-apply the brain-specific tweaks
// flagged below with `// brain:` comments.
//
// brain: this template wires up two brain-specific transformers:
//
//   * `Plugin.DerivedFenceMark` (defined in
//     `<vault>/.quartz/quartz/plugins/transformers/derivedFenceMark.ts`,
//     installed by `brain vault render --overlay`). It must run after
//     `Plugin.ObsidianFlavoredMarkdown()` so `[[wiki-link]]` syntax
//     has been converted into mdast `link` nodes by the time the
//     fence walker stamps `data-brain-derived` attributes on them.
//     See the transformer's top-of-file comment for the full contract.
//
//   * `Plugin.ReloadSignal` (defined in
//     `<vault>/.quartz/quartz/plugins/transformers/reloadSignal.ts`,
//     also installed by the overlay). Injects a polling reload
//     `<script>` into every page when `BRAIN_WIKI_RELOAD=1` is set in
//     the build env — replaces Quartz's `--serve` WebSocket reload
//     path, which is dead in the brain blue-green serve flow because
//     Caddy (not Quartz) serves the static output. See the
//     transformer's top-of-file comment for the full contract.

import { QuartzConfig } from "./quartz/cfg"
import * as Plugin from "./quartz/plugins"

const config: QuartzConfig = {
  configuration: {
    // brain: friendlier title than the default "Quartz 4".
    pageTitle: "🧠 Second Brain",
    pageTitleSuffix: "",
    enableSPA: true,
    enablePopovers: true,
    // brain: analytics off — the wiki is private and rendered locally.
    // If you deploy the static output somewhere public, set this to your
    // provider of choice (plausible, google, umami, etc.).
    analytics: null,
    locale: "en-US",
    // brain: localhost for dev; flip to your domain before deploying.
    baseUrl: "localhost:8080",
    // brain: skip tooling-managed folders. `_templates` / `_attachments`
    // are brain conventions; `.quartz` is the workspace itself; `dist` /
    // `public` are build outputs that must NOT be re-walked as content.
    ignorePatterns: [
      "private",
      "_templates",
      "_attachments",
      "dist",
      "public",
      ".obsidian",
      ".git",
      ".quartz",
    ],
    defaultDateType: "modified",
    theme: {
      fontOrigin: "googleFonts",
      cdnCaching: true,
      // brain: warmer typography for a less corporate feel. Fraunces is
      // characterful without being precious; Inter reads cleanly at body
      // size; JetBrains Mono has decent ligatures for code blocks.
      typography: {
        header: "Fraunces",
        body: "Inter",
        code: "JetBrains Mono",
      },
      // brain: warm cream + terracotta in light mode, deep espresso +
      // peach in dark. Easier to live in for long reading sessions than
      // the stock cool grays.
      colors: {
        lightMode: {
          light: "#fdfaf6",
          lightgray: "#ebe4d9",
          gray: "#a89f8c",
          darkgray: "#5c5340",
          dark: "#2a2418",
          secondary: "#c4602b",
          tertiary: "#a4ac86",
          highlight: "rgba(196, 96, 43, 0.12)",
          textHighlight: "#fde58a99",
        },
        darkMode: {
          light: "#1a1611",
          lightgray: "#2e2820",
          gray: "#5c544a",
          darkgray: "#c4b8a3",
          dark: "#f0e9da",
          secondary: "#e8a570",
          tertiary: "#b8c19c",
          highlight: "rgba(232, 165, 112, 0.15)",
          textHighlight: "#fde58a55",
        },
      },
    },
  },
  plugins: {
    transformers: [
      Plugin.FrontMatter(),
      Plugin.CreatedModifiedDate({
        priority: ["frontmatter", "git", "filesystem"],
      }),
      Plugin.SyntaxHighlighting({
        theme: { light: "github-light", dark: "github-dark" },
        keepBackground: false,
      }),
      Plugin.ObsidianFlavoredMarkdown({ enableInHtmlEmbed: false }),
      // brain: tag derived-edge `<a>` tags inside Phase D fences with
      // `data-brain-derived` / `data-brain-rule` / `data-brain-weight`
      // so the custom contentIndex emitter can classify them as
      // `kind: "derived"` in `static/contentIndex.json`. Must run after
      // ObsidianFlavoredMarkdown so wiki-link syntax has already been
      // converted to mdast link nodes.
      Plugin.DerivedFenceMark(),
      // brain: inject the polling reload watcher (`/static/reload.js`)
      // into every page when `BRAIN_WIKI_RELOAD=1` at build time.
      // Replaces Quartz's `--serve` WebSocket reload, which is dead in
      // our blue-green serve flow because Caddy (not Quartz) is the
      // static file server. `bin/brain-up` sets the env var; `brain
      // vault render` (prod path) leaves it unset so prod pages ship
      // without a polling client. The transformer has no markdown
      // ordering requirement — it only contributes a `<script>` tag
      // via `externalResources()` — but is grouped here next to
      // `DerivedFenceMark` so the two brain-extension transformers
      // stay co-located.
      Plugin.ReloadSignal(),
      Plugin.GitHubFlavoredMarkdown(),
      Plugin.TableOfContents(),
      Plugin.CrawlLinks({ markdownLinkResolution: "shortest" }),
      Plugin.Description(),
      Plugin.Latex({ renderEngine: "katex" }),
    ],
    filters: [Plugin.RemoveDrafts()],
    emitters: [
      Plugin.AliasRedirects(),
      Plugin.ComponentResources(),
      Plugin.ContentPage(),
      Plugin.FolderPage(),
      Plugin.TagPage(),
      Plugin.ContentIndex({
        enableSiteMap: true,
        enableRSS: true,
      }),
      Plugin.Assets(),
      Plugin.Static(),
      Plugin.NotFoundPage(),
      // brain: the graph view is the killer feature for an Obsidian-style
      // vault. Tweak its on-page behavior in quartz.layout.ts via
      // Component.Graph({ localGraph, globalGraph }).
      Plugin.Graph(),
    ],
  },
}

export default config
