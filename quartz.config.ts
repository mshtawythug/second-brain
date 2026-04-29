// Sample Quartz v4 configuration for brain vaults.
//
// This file is a TEMPLATE. Copy it into your Quartz workspace as
// `<vault>/.quartz/quartz.config.ts` (the directory `npx quartz create`
// scaffolds for you) — it does NOT compile or run from the brain repo
// itself; the imports below resolve against the dependencies Quartz
// pulls into your workspace, not against any package brain ships.
//
// Tested against Quartz v4.x (April 2026). If a future Quartz version
// renames a plugin or its option shape, copy the latest config from
// https://quartz.jzhao.xyz/ and re-apply the brain-specific tweaks
// flagged below with `// brain:` comments.

import { QuartzConfig } from "./quartz/cfg"
import * as Plugin from "./quartz/plugins"

const config: QuartzConfig = {
  configuration: {
    pageTitle: "Second Brain",
    pageTitleSuffix: "",
    enableSPA: true,
    enablePopovers: true,
    // brain: analytics off by default — flip this to your provider if
    // you deploy the rendered site somewhere public. The brain vault is
    // private by default; we don't phone home from `brain vault render`.
    analytics: null,
    locale: "en-US",
    baseUrl: "localhost:8080",
    // brain: skip tooling-managed folders. `_templates` is brain's
    // template scaffold dir; `_attachments` holds binaries referenced
    // by notes; `.git` keeps version-control metadata out of the wiki.
    ignorePatterns: ["_templates", "_attachments", ".git", ".obsidian"],
    defaultDateType: "created",
    theme: {
      cdnCaching: true,
      typography: {
        header: "Schibsted Grotesk",
        body: "Source Sans Pro",
        code: "IBM Plex Mono",
      },
      colors: {
        lightMode: {
          light: "#faf8f8",
          lightgray: "#e5e5e5",
          gray: "#b8b8b8",
          darkgray: "#4e4e4e",
          dark: "#2b2b2b",
          secondary: "#284b63",
          tertiary: "#84a59d",
          highlight: "rgba(143, 159, 169, 0.15)",
          textHighlight: "#fff236aa",
        },
        darkMode: {
          light: "#161618",
          lightgray: "#393639",
          gray: "#646464",
          darkgray: "#d4d4d4",
          dark: "#ebebec",
          secondary: "#7b97aa",
          tertiary: "#84a59d",
          highlight: "rgba(143, 159, 169, 0.15)",
          textHighlight: "#b3aa0288",
        },
      },
    },
  },
  plugins: {
    transformers: [
      Plugin.FrontMatter(),
      Plugin.CreatedModifiedDate({
        priority: ["frontmatter", "filesystem"],
      }),
      Plugin.SyntaxHighlighting({
        theme: { light: "github-light", dark: "github-dark" },
        keepBackground: false,
      }),
      // brain: the vault uses Obsidian-flavored wiki-links; this
      // transformer parses `[[Title]]`, `![[embed]]`, and pipe aliases
      // exactly the way `brain vault sync` does.
      Plugin.ObsidianFlavoredMarkdown({ enableInHtmlEmbed: false }),
      Plugin.GitHubFlavoredMarkdown(),
      Plugin.TableOfContents(),
      // brain: shortest match keeps `[[person-x]]` working even when the
      // file lives in a nested folder.
      Plugin.CrawlLinks({ markdownLinkResolution: "shortest" }),
      Plugin.Description(),
      Plugin.Latex({ renderEngine: "katex" }),
    ],
    filters: [
      Plugin.RemoveDrafts(),
      Plugin.ExplicitPublish(),
    ],
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
      // brain: graph view is the headline reason to use Quartz over a
      // plain MkDocs setup — keep this enabled.
      Plugin.Graph(),
    ],
  },
}

export default config
