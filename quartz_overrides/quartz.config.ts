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
      // brain: commonplace-book typography pairing for the 2026 redesign.
      // Fraunces (display) carries headings + italics + display flourishes.
      // Newsreader (body) is Google Fonts' optical-sized serif designed for
      // long-form reading; it pairs with Fraunces' carved character for a
      // private-scriptorium voice. JetBrains Mono ships ligatures for code.
      // Inter was dropped (the `frontend-design` skill flags it as generic
      // / "AI-slop") — body type is now serif throughout.
      typography: {
        header: "Fraunces",
        body: "Newsreader",
        code: "JetBrains Mono",
      },
      // brain: warm cream + terracotta in light mode, deep espresso +
      // peach in dark. Easier to live in for long reading sessions than
      // the stock cool grays.
      //
      // 2026-05-02 redesign refinements (Lane A):
      //   * Light: secondary terracotta deepened (`#c4602b → #b85427`)
      //     for more saturation, tertiary cooled toward sage
      //     (`#a4ac86 → #7a8a6b`), `dark` darkened (`#2a2418 → #1f1a0e`)
      //     for crisper text contrast on the cream paper bg.
      //   * Dark: `light` (the dark-mode page bg) lifted to a richer
      //     espresso (`#1a1611 → #16110a`) and `gray` warmed
      //     (`#5c544a → #6b6354`) so muted UI text reads warm on the
      //     deeper background. Other dark tokens kept stable to avoid
      //     pulling on Lane C/D component work.
      colors: {
        lightMode: {
          light: "#fdfaf6",
          lightgray: "#ebe4d9",
          // brain: 2026-05-02 redesign — bumped from `#a89f8c` to
          // `#7a715f` so `--ink-2: var(--gray)` (muted metadata, dingbats,
          // chip labels per `_tokens.scss`) clears WCAG AA on the
          // `--surface-0` cream bg. Old value measured 2.52:1 (fails
          // even the 3:1 large-text floor); new value measures ~4.5:1.
          gray: "#7a715f",
          darkgray: "#5c5340",
          dark: "#1f1a0e",
          secondary: "#b85427",
          tertiary: "#7a8a6b",
          highlight: "rgba(184, 84, 39, 0.12)",
          textHighlight: "#fde58a99",
        },
        darkMode: {
          light: "#16110a",
          lightgray: "#2e2820",
          gray: "#6b6354",
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
      // brain: Lane B redesign — classify every mdast link node into
      // one of five kinds (wiki / external / tag / ingested / derived)
      // and stamp `data-brain-link-kind` on `node.data.hProperties`.
      // The Lane B `_links.scss` consumes that attribute to give each
      // kind a distinct visual treatment (terracotta underline / dotted
      // + arrow / pill chip / source-tinted left rail / italic dashed).
      // Must run AFTER `ObsidianFlavoredMarkdown` (so wiki-link mdast
      // nodes exist) AND AFTER `DerivedFenceMark` (so its
      // `data-brain-derived` stamp is already in place — the
      // classifier reads it to short-circuit the kind to `derived`).
      // See `quartz/plugins/transformers/linkKindMark.ts` for the
      // full classification contract.
      Plugin.LinkKindMark(),
      // brain: Lane B redesign — inject the runtime source-tagger
      // (`/static/linkSourceTag.js`) into every page. The tagger
      // extracts the source segment from `_ingested/<source>/...`
      // hrefs at `DOMContentLoaded` (and on Quartz SPA `nav` events)
      // and stamps `data-brain-source="krisp"` etc. on each ingested
      // link, so `_links.scss`'s source-tinted left-rail rules
      // (`&[data-brain-source="krisp"]`) can pick the right
      // `--brain-source-*` color from `_tokens.scss`. Always emits
      // (no env-var gate, unlike `ReloadSignal`) — the script is part
      // of the production redesign. No markdown ordering requirement;
      // grouped here next to `LinkKindMark` so the Lane B plugins
      // stay co-located. See `quartz/plugins/transformers/linkSourceTag.ts`
      // for the inject contract and the static script for the
      // tagging logic.
      Plugin.LinkSourceTag(),
      // brain: Lane C redesign — inject the runtime code-copy injector
      // (`/static/codeCopy.js`) into every page. The injector walks
      // every `<pre>` in the article body, lifts the `data-language`
      // attribute from the inner `<code>` to the outer `<pre>` (so
      // the CSS-only language label in `_code.scss` can read it via
      // `attr()`), and appends a brain-themed `.brain-code-copy`
      // button. Stock Quartz's `.clipboard-button` is hidden via
      // `_code.scss` so the two don't render side-by-side. Always
      // emits (no env-var gate, like `LinkSourceTag`) — the script is
      // part of the production redesign. No markdown ordering
      // requirement; grouped here next to `LinkSourceTag` so the
      // brain script-only transformers stay co-located. See
      // `quartz/plugins/transformers/codeCopy.ts` for the inject
      // contract and `quartz/static/codeCopy.js` for the injector.
      Plugin.CodeCopy(),
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
      // brain: Lane B redesign — `externalLinkIcon: false` disables
      // Quartz's stock inline-SVG `↗` arrow on external links. The Lane B
      // `_links.scss` paints its own `↗` via `::after` on
      // `a[data-brain-link-kind="external"]` so the color/opacity track
      // the redesign tokens; leaving stock's icon enabled would render
      // every external link with two arrows side-by-side.
      Plugin.CrawlLinks({ markdownLinkResolution: "shortest", externalLinkIcon: false }),
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
      // vault. The actual graph rendering happens via `Component.Graph(...)`
      // in `quartz.layout.ts`; there is no `Plugin.Graph` emitter in stock
      // Quartz v4.5.x — only the component is needed here.
    ],
  },
}

export default config
