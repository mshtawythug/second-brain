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
//     also installed by the overlay). Injects an ETag-aware polling reload
//     `<script>` into every page when `BRAIN_WIKI_RELOAD=1` is set in
//     the build env — replaces Quartz's `--serve` WebSocket reload
//     path, which is dead in the brain blue-green serve flow because
//     Caddy (not Quartz) serves the static output. See the
//     transformer's top-of-file comment for the full contract.

import { QuartzConfig } from "./quartz/cfg"
import * as Plugin from "./quartz/plugins"
// brain-extension (F6): imported by PATH rather than through the `Plugin`
// barrel, so the overlay does not have to replace upstream's
// `quartz/plugins/filters/index.ts` — one fewer upstream file to keep in sync.
import { RemoveConfidential } from "./quartz/plugins/filters/sensitivity"

const config: QuartzConfig = {
  configuration: {
    // brain: friendlier title than the default "Quartz 4".
    pageTitle: "Second Brain",
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
      // brain: P4.7 — server-rendered partials (e.g. `_partials/recent.md`,
      // the source-of-truth for the home-page "Recently captured" rail) live
      // outside the public surface. The home note renders them inline via
      // a fence; the partial itself must never appear as its own page.
      "_partials",
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
      // brain: Linear-style typography for the tech-forward variant of
      // the 2026 redesign. Geist (Vercel's grotesk) for both display and
      // body — clean, neutral, modern; Geist Mono for code and metadata.
      // Both are on Google Fonts; Quartz's Google Fonts loader uses the
      // family name verbatim.
      typography: {
        header: "Geist",
        body: "Geist",
        code: "Geist Mono",
      },
      // brain: Linear-style charcoal + indigo-violet palette. Dark mode
      // is the headline aesthetic — Linear lives in dark — but light mode
      // is a clean pure-neutral zinc scale for daytime reading.
      colors: {
        lightMode: {
          light: "#ffffff",
          lightgray: "#e4e4e7",
          // brain: zinc-500 (#71717a) clears WCAG AA against pure white
          // bg for muted text, metadata, dingbats.
          gray: "#71717a",
          darkgray: "#3f3f46",
          dark: "#09090b",
          secondary: "#5e6ad2",
          tertiary: "#8b5cf6",
          highlight: "rgba(94, 106, 210, 0.12)",
          textHighlight: "#fde047b3",
        },
        darkMode: {
          light: "#0a0a0a",
          lightgray: "#27272a",
          // brain: zinc-400 (#a1a1aa) clears WCAG AA against #0a0a0a.
          gray: "#a1a1aa",
          darkgray: "#d4d4d8",
          dark: "#fafafa",
          secondary: "#7170ff",
          tertiary: "#a78bfa",
          highlight: "rgba(113, 112, 255, 0.18)",
          textHighlight: "#fde04766",
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
      // brain: Recent rail — inject the live relative-date recomputer
      // (`/static/relativeDate.js`) into every page. The home-page Recent
      // rail emits `<span class="brain-rel-date" data-date="<ISO>">{abs}</span>`
      // (see `brain.wiki.build_homepage`); this script reads each
      // `data-date` and recomputes the relative text ("3d ago") live on
      // `DOMContentLoaded` + Quartz SPA `nav`, so the rail never decays
      // between daily builds. Bucket logic mirrors
      // `build_homepage._format_relative_date`. Always emits (no env-var
      // gate, like `LinkSourceTag`); no markdown ordering requirement;
      // grouped here next to `LinkSourceTag` so the brain script-only
      // transformers stay co-located. See
      // `quartz/plugins/transformers/relativeDate.ts` for the inject
      // contract and `quartz/static/relativeDate.js` for the recomputer.
      Plugin.RelativeDate(),
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
      // brain: P4.4 — inject the email-thread reading-mode runtime
      // (`/static/emailThread.js`) plus an inline `window
      // .BRAIN_USER_EMAIL` global (read from
      // `process.env.BRAIN_USER_EMAIL` at build time). The runtime
      // (a) annotates each per-message section in an `email_thread`
      // body with `data-brain-thread-from` + `data-brain-is-mine`,
      // and (b) renders a "Show only my replies" filter button at
      // the top of `<article>` that toggles the
      // `body.brain-replies-only` class — the SCSS partial
      // `_email_thread.scss` then hides every
      // `[data-brain-is-mine="false"]` section. Always emits (no
      // env-var gate, like `LinkSourceTag` and `CodeCopy`) — the
      // runtime is part of the production redesign and self-gates on
      // an `_ingested/gmail/` URL pathname. No markdown ordering
      // requirement; grouped here next to `CodeCopy` so the brain
      // script-only transformers stay co-located. See
      // `quartz/plugins/transformers/emailThread.ts` for the inject
      // contract and `quartz/static/emailThread.js` for the runtime
      // logic.
      Plugin.EmailThreadReader(),
      // brain: inject the ETag-aware reload watcher (`/static/reload.js`)
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
      // brain: P4.1 — strip home-page `<li>`s whose link resolves to an
      // empty folder (today: `daily/` when the user hasn't created any
      // daily notes yet). Generic — extend the `folders` option to
      // protect future top-level subdirs (e.g. a hypothetical
      // `workbench/`) from the same 404. Must run AFTER `CrawlLinks` so
      // the rehype-stage `<a href>`s have already been resolved to the
      // shape the matcher expects (`daily/`, `daily/index`, …); without
      // that ordering the matcher's normalizer sees raw markdown URLs
      // and mis-classifies. The transformer reads `ctx.argv.directory`
      // once per build to compute which folders are empty.
      Plugin.EmptyDoorFilter(),
      Plugin.Description(),
      // strict: "ignore" quiets KaTeX's strict-mode `unicodeTextInMathMode`
      // report for Latin-1 typographic chars. It does NOT silence the
      // `No character metrics for '<c>' in style 'Main-Regular'` warnings —
      // those come from an unconditional console.warn in katex's makeSymbol
      // and no option gates them. The earlier comment here claimed otherwise;
      // it was wrong, and the warnings kept accumulating in the build log.
      //
      // The actual cause of those warnings was single-dollar inline math
      // swallowing ordinary prose between two dollar amounts. That is fixed in
      // the overlay's own quartz/plugins/transformers/latex.ts, which passes
      // `singleDollarTextMath: false` to remarkMath — see the comment there.
      // `$$…$$` block math still works.
      Plugin.Latex({ renderEngine: "katex", katexOptions: { strict: "ignore" } }),
    ],
    // brain-extension (F6): TWO filters, deliberately separate.
    //
    // `RemoveDrafts` is upstream and means "not ready to show".
    // `RemoveConfidential` is ours and means "must not leak". They share a
    // mechanism (`shouldPublish`, which drops the file before ANY emitter
    // runs — page, RSS feed, tag listings, contentIndex, contentBodies) but
    // not a branch, because sharing a branch is how a publish *guarantee*
    // got assumed rather than checked: filtering only the contentIndex
    // emitter left confidential bodies live at `<slug>.html`, in `index.xml`,
    // and on `tags/<tag>.html`. See the plugin's own file for the measurement.
    filters: [Plugin.RemoveDrafts(), RemoveConfidential()],
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
