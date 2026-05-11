// Brain hide-if-empty door filter — Phase 4.1 of the Wiki UX Overhaul.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/transformers/emptyDoorFilter.ts` by
// `brain vault render --overlay`. It does NOT compile or run from the
// brain repo itself; the imports below resolve against the
// dependencies Quartz pulls into the cloned workspace via `npm
// install`.
//
// What this transformer does, and why: the home note (`index.md`) ships
// a hand-curated set of "doors" — folder-style links like
// `[Daily notes](daily/)` that take the user into a top-level subdir
// of the vault. When the destination subdir is empty, the door
// resolves to a 404. The fix has two layers:
//
//   1. A brain CLI step that auto-generates `<subdir>/index.md` when
//      the subdir has notes (see `src/brain/vault/daily_index.py` for
//      the daily case). When the subdir is non-empty the door
//      resolves cleanly.
//
//   2. This transformer. When the subdir is empty (no `*.md` files
//      other than a stale `index.md` itself), strip the door's
//      enclosing `<li>` from the rendered HTML so the user never
//      sees a link to a 404. Generic — a single `folders` option
//      controls which subdirs to check, so a future "Workbench"
//      door (or any other future top-level subdir) gets the same
//      treatment by appending its name to the option.
//
// Restricted to the home page by default (`pageSlugs: ["index"]`) —
// stripping random `<li>`s on every page would be too aggressive.
// The brief case is the home navigation; if a future PR needs the
// behaviour elsewhere, extend the option.
//
// Tested against Quartz v4.5.x (April 2026). The rehype tree shape
// (hast `Element` with `tagName` / `properties` / `children`) is
// stable across versions; the `Plugin.CrawlLinks` integration this
// depends on (resolving folder refs to relative URLs) has been
// in place since v4.0.

import * as fs from "node:fs"
import * as path from "node:path"

import type { Element, Root, RootContent } from "hast"

import { QuartzTransformerPlugin } from "../types"

export interface EmptyDoorFilterOptions {
  // brain: list of folder names (relative to the vault root) to check
  // for emptiness. The transformer compares each link's resolved href
  // against this list and strips the enclosing `<li>` if the folder
  // has zero matchable `.md` files. `daily` is the only case that
  // motivated this transformer; future top-level subdirs (e.g. a
  // hypothetical `workbench/`) get the same treatment by appending
  // their name here.
  folders: string[]
  // brain: page slugs the transformer fires on. Default `["index"]`
  // — the home note. Restricting prevents accidentally stripping
  // legitimate navigation `<li>`s from other pages.
  pageSlugs: string[]
}

const defaultOptions: EmptyDoorFilterOptions = {
  folders: ["daily"],
  pageSlugs: ["index"],
}

// brain: a folder counts as "non-empty" if it contains at least one
// markdown file whose name is NOT `index.md`. The `index.md` exclusion
// stops a stale auto-generated index from masking an otherwise-empty
// folder. Recursive walk so a year-folded ``daily/<YYYY>/<note>.md``
// layout is handled (the brain CLI nests dailies under year folders).
function folderHasNotes(folderPath: string): boolean {
  if (!fs.existsSync(folderPath)) return false
  let stack: string[] = [folderPath]
  while (stack.length > 0) {
    const dir = stack.pop()
    if (dir === undefined) break
    let entries: fs.Dirent[]
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true })
    } catch {
      // Permission errors / race deletes — treat as empty for the
      // affected subtree rather than crashing the build.
      continue
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        stack.push(full)
        continue
      }
      if (!entry.isFile()) continue
      if (!entry.name.endsWith(".md")) continue
      if (entry.name === "index.md") continue
      return true
    }
  }
  return false
}

// brain: tolerant href→folder match. `Plugin.CrawlLinks` rewrites
// internal links to relative URLs; the home note's `daily/` link
// might resolve to `./daily/`, `daily/`, or `daily/index` depending
// on `markdownLinkResolution`. We strip a leading `./`, drop a
// trailing `/`, and trim a trailing `/index` so the comparator
// against the bare folder name catches every shape.
function hrefMatchesFolder(href: string, emptyFolders: Set<string>): boolean {
  if (href.length === 0) return false
  // Skip absolute / external URLs cheaply — folder refs are always
  // relative.
  if (/^[a-z]+:/i.test(href)) return false
  let cleaned = href.split("#")[0]
  cleaned = cleaned.replace(/^\.\//, "")
  cleaned = cleaned.replace(/\/$/, "")
  cleaned = cleaned.replace(/\/index$/, "")
  // Folder name is the last path segment for nested doors (none today,
  // but a future `tools/scratch/` would need this).
  return emptyFolders.has(cleaned)
}

// brain: walks an `<li>` subtree to determine if any descendant `<a>`
// points at one of the empty folders. Hast doesn't ship a visitor
// helper this targeted (`hast-util-visit` would work but adds an
// import for what's a 12-line walk), so we recurse manually.
function listItemPointsAtEmptyFolder(
  li: Element,
  emptyFolders: Set<string>,
): boolean {
  const stack: RootContent[] = [...li.children]
  while (stack.length > 0) {
    const node = stack.pop()
    if (node === undefined) break
    if (node.type !== "element") continue
    const el = node as Element
    if (el.tagName === "a") {
      const href = (el.properties ?? {})["href"]
      if (typeof href === "string" && hrefMatchesFolder(href, emptyFolders)) {
        return true
      }
    }
    if (Array.isArray(el.children)) {
      for (const child of el.children) {
        stack.push(child)
      }
    }
  }
  return false
}

// brain: in-place tree edit — splice every `<li>` that points at an
// empty folder. Walks recursively so a `<li>` nested inside a `<ul>`
// inside a `<details>` (or any wrapper) is still reachable. We mutate
// `node.children` to a filtered list; preact reconciles fine with
// hast tree mutation since rehype-stringify reads the final shape
// after every transformer plugin runs.
function stripEmptyDoorListItems(
  tree: Root | Element,
  emptyFolders: Set<string>,
): void {
  const children = tree.children
  if (!Array.isArray(children)) return
  const filtered: typeof children = []
  for (const child of children) {
    if (child.type === "element") {
      const el = child as Element
      if (el.tagName === "li" && listItemPointsAtEmptyFolder(el, emptyFolders)) {
        // Drop the entire `<li>` — including any nested children we
        // would otherwise have recursed into. The whole door (link +
        // surrounding context — emoji, em-dash subtitle, etc.) is
        // expected to live inside the same `<li>`, which matches the
        // home note's authored shape (`- 📅 [Daily notes](daily/) — …`).
        continue
      }
      stripEmptyDoorListItems(el, emptyFolders)
    }
    filtered.push(child)
  }
  tree.children = filtered as typeof tree.children
}

export const EmptyDoorFilter: QuartzTransformerPlugin<
  Partial<EmptyDoorFilterOptions>
> = (userOpts) => {
  const opts: EmptyDoorFilterOptions = { ...defaultOptions, ...userOpts }
  return {
    name: "EmptyDoorFilter",
    htmlPlugins(ctx) {
      // brain: cache empty-folder set per build. The vault filesystem
      // is read once per `htmlPlugins` invocation (Quartz calls it once
      // per build); rendering 1000+ pages doesn't re-stat the same
      // dirs. The `Set` is the comparator the per-page walker hits.
      const vaultRoot = ctx.argv.directory
      const emptyFolders = new Set<string>()
      for (const folder of opts.folders) {
        if (!folderHasNotes(path.join(vaultRoot, folder))) {
          emptyFolders.add(folder)
        }
      }
      const pageSlugSet = new Set(opts.pageSlugs)
      return [
        () => async (tree: Root, file) => {
          // brain: short-circuit when nothing's empty — the vast
          // majority of builds will land here, since the home note's
          // doors point at populated dirs.
          if (emptyFolders.size === 0) return
          const slug = (file.data as { slug?: string }).slug
          if (typeof slug !== "string" || !pageSlugSet.has(slug)) return
          stripEmptyDoorListItems(tree, emptyFolders)
        },
      ]
    },
  }
}
