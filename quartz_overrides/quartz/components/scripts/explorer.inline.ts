// Brain explorer inline script — overlay-replacement of Quartz's stock
// `quartz/components/scripts/explorer.inline.ts`.
//
// This file is installed at
// `<vault>/.quartz/quartz/components/scripts/explorer.inline.ts` by
// `brain vault render --overlay` (`src/brain/vault/quartz_overlay.py`),
// 1:1 file overwrite. The stock `Explorer.tsx` imports
// `./scripts/explorer.inline`, so this override is the canonical seam
// for changing explorer client-side behaviour without forking the
// component itself.
//
// brain delta vs upstream (Quartz v4.5.x, April 2026):
//   * `countFiles(node)` — recursive count of leaf (non-folder)
//     descendants under a folder node.
//   * `createFolderNode` — appends a `<span class="folder-count">N</span>`
//     immediately after the folder title (whether title is a button or
//     anchor, depending on `folderClickBehavior`). Style lives in
//     `quartz_overrides/quartz/styles/brain/_sidebar.scss`.
//   * P4.2 (Wiki UX Overhaul, item 7) — "Show ingested" toggle button
//     above the explorer tree. State persists in `localStorage` under
//     `brain.explorer.showIngested` as a JSON boolean. Default = `false`
//     (OFF, hides the `_ingested/` top-level folder + descendants from
//     the rendered tree, which also drops them from `.folder-count`
//     totals because the count is computed from the post-filter trie).
//     Click flips state, re-renders the tree, and updates the button
//     label. The injection is script-side (rather than SSR-side via an
//     Explorer.tsx override) to keep the upstream component unforked —
//     same pattern as the `.folder-count` badge above.
//
// Everything else is verbatim from upstream. When upgrading Quartz,
// diff this file against the new
// https://github.com/jackyzha0/quartz/blob/v4/quartz/components/scripts/explorer.inline.ts
// and re-apply the brain deltas above.

import { FileTrieNode } from "../../util/fileTrie"
import { FullSlug, resolveRelative, simplifySlug } from "../../util/path"
import { ContentDetails } from "../../plugins/emitters/contentIndex"

// brain (P4.2): localStorage key for the "Show ingested" toggle.
// Mirrors the `brain.search.activeSources` idiom from search.inline.ts —
// dotted prefix `brain.<feature>.<setting>` so future brain-prefixed
// preferences cluster cleanly under one namespace inspectable via
// devtools.
const SHOW_INGESTED_KEY = "brain.explorer.showIngested"

// brain (P4.2): top-level folder segment to hide when the toggle is
// OFF. Pinned as a constant (rather than inlined in the predicate) so
// a future brain rename of the ingested-mirror tree is a one-line
// change here. The value matches the path-form heuristic used by the
// contentIndex emitter (`slug.startsWith("_ingested/")`) and the
// `inferSource()` helper in search.inline.ts — keeping the literal in
// one place per file documents the cross-file coordination.
const INGESTED_FOLDER_SEGMENT = "_ingested"

// brain (P4.2): button-label strings, factored out so the static
// source-test can pin them and a future i18n pass has a single seam
// to retarget.
const SHOW_INGESTED_LABELS = {
  show: "Show ingested",
  hide: "Hide ingested",
} as const

type MaybeHTMLElement = HTMLElement | undefined

// brain (P4.2): read the persisted "Show ingested" preference. Default
// (no key, missing localStorage, parse failure, non-boolean) is
// `false` so a fresh visitor sees the curated vault tree without the
// ingested noise. Try/catch covers private-mode Safari which can
// throw on `localStorage.getItem`.
function loadShowIngested(): boolean {
  if (typeof localStorage === "undefined") return false
  try {
    const raw = localStorage.getItem(SHOW_INGESTED_KEY)
    if (raw === null) return false
    const parsed = JSON.parse(raw)
    return typeof parsed === "boolean" ? parsed : false
  } catch {
    return false
  }
}

// brain (P4.2): persist the toggle state. Errors are swallowed —
// localStorage may throw (`QuotaExceededError` in private mode); a
// failed write should not tear down the explorer. Worst case the user
// flips the chip again next session.
function persistShowIngested(value: boolean): void {
  if (typeof localStorage === "undefined") return
  try {
    localStorage.setItem(SHOW_INGESTED_KEY, JSON.stringify(value))
  } catch {
    // intentional: see docstring
  }
}

interface ParsedOptions {
  folderClickBehavior: "collapse" | "link"
  folderDefaultState: "collapsed" | "open"
  useSavedState: boolean
  sortFn: (a: FileTrieNode, b: FileTrieNode) => number
  filterFn: (node: FileTrieNode) => boolean
  mapFn: (node: FileTrieNode) => void
  order: "sort" | "filter" | "map"[]
}

type FolderState = {
  path: string
  collapsed: boolean
}

let currentExplorerState: Array<FolderState>
function toggleExplorer(this: HTMLElement) {
  const nearestExplorer = this.closest(".explorer") as HTMLElement
  if (!nearestExplorer) return
  const explorerCollapsed = nearestExplorer.classList.toggle("collapsed")
  nearestExplorer.setAttribute(
    "aria-expanded",
    nearestExplorer.getAttribute("aria-expanded") === "true" ? "false" : "true",
  )

  if (!explorerCollapsed) {
    // Stop <html> from being scrollable when mobile explorer is open
    document.documentElement.classList.add("mobile-no-scroll")
  } else {
    document.documentElement.classList.remove("mobile-no-scroll")
  }
}

function toggleFolder(evt: MouseEvent) {
  evt.stopPropagation()
  const target = evt.target as MaybeHTMLElement
  if (!target) return

  // Check if target was svg icon or button
  const isSvg = target.nodeName === "svg"

  // corresponding <ul> element relative to clicked button/folder
  const folderContainer = (
    isSvg
      ? // svg -> div.folder-container
        target.parentElement
      : // button.folder-button -> div -> div.folder-container
        target.parentElement?.parentElement
  ) as MaybeHTMLElement
  if (!folderContainer) return
  const childFolderContainer = folderContainer.nextElementSibling as MaybeHTMLElement
  if (!childFolderContainer) return

  childFolderContainer.classList.toggle("open")

  // Collapse folder container
  const isCollapsed = !childFolderContainer.classList.contains("open")
  setFolderState(childFolderContainer, isCollapsed)

  const currentFolderState = currentExplorerState.find(
    (item) => item.path === folderContainer.dataset.folderpath,
  )
  if (currentFolderState) {
    currentFolderState.collapsed = isCollapsed
  } else {
    currentExplorerState.push({
      path: folderContainer.dataset.folderpath as FullSlug,
      collapsed: isCollapsed,
    })
  }

  const stringifiedFileTree = JSON.stringify(currentExplorerState)
  localStorage.setItem("fileTree", stringifiedFileTree)
}

function createFileNode(currentSlug: FullSlug, node: FileTrieNode): HTMLLIElement {
  const template = document.getElementById("template-file") as HTMLTemplateElement
  const clone = template.content.cloneNode(true) as DocumentFragment
  const li = clone.querySelector("li") as HTMLLIElement
  const a = li.querySelector("a") as HTMLAnchorElement
  a.href = resolveRelative(currentSlug, node.slug)
  a.dataset.for = node.slug
  a.textContent = node.displayName

  if (currentSlug === node.slug) {
    a.classList.add("active")
  }

  return li
}

// brain delta: recursive leaf-count for folder nodes. A "leaf" is any
// node where `isFolder === false` — `FileTrieNode` is the post-filter
// trie (Quartz strips `tags/` via the default `filterFn`), so the
// count reflects what the user actually sees in the explorer rather
// than the raw contentIndex.
function countFiles(node: FileTrieNode): number {
  let count = 0
  for (const child of node.children) {
    if (child.isFolder) {
      count += countFiles(child)
    } else {
      count += 1
    }
  }
  return count
}

function createFolderNode(
  currentSlug: FullSlug,
  node: FileTrieNode,
  opts: ParsedOptions,
): HTMLLIElement {
  const template = document.getElementById("template-folder") as HTMLTemplateElement
  const clone = template.content.cloneNode(true) as DocumentFragment
  const li = clone.querySelector("li") as HTMLLIElement
  const folderContainer = li.querySelector(".folder-container") as HTMLElement
  const titleContainer = folderContainer.querySelector("div") as HTMLElement
  const folderOuter = li.querySelector(".folder-outer") as HTMLElement
  const ul = folderOuter.querySelector("ul") as HTMLUListElement

  const folderPath = node.slug
  folderContainer.dataset.folderpath = folderPath

  if (currentSlug === folderPath) {
    folderContainer.classList.add("active")
  }

  if (opts.folderClickBehavior === "link") {
    // Replace button with link for link behavior
    const button = titleContainer.querySelector(".folder-button") as HTMLElement
    const a = document.createElement("a")
    a.href = resolveRelative(currentSlug, folderPath)
    a.dataset.for = folderPath
    a.className = "folder-title"
    a.textContent = node.displayName
    button.replaceWith(a)
  } else {
    const span = titleContainer.querySelector(".folder-title") as HTMLElement
    span.textContent = node.displayName
  }

  // brain delta: append a `.folder-count` badge after the folder
  // title showing the recursive file count under this folder. We
  // inject post-title (rather than into the template) so the upstream
  // template stays unchanged — keeps Explorer.tsx unforked.
  const fileCount = countFiles(node)
  if (fileCount > 0) {
    const titleEl = titleContainer.querySelector(".folder-title") as HTMLElement | null
    if (titleEl) {
      const countEl = document.createElement("span")
      countEl.className = "folder-count"
      countEl.textContent = String(fileCount)
      countEl.setAttribute("aria-label", `${fileCount} notes`)
      titleEl.insertAdjacentElement("afterend", countEl)
    }
  }

  // if the saved state is collapsed or the default state is collapsed
  const isCollapsed =
    currentExplorerState.find((item) => item.path === folderPath)?.collapsed ??
    opts.folderDefaultState === "collapsed"

  // if this folder is a prefix of the current path we
  // want to open it anyways
  const simpleFolderPath = simplifySlug(folderPath)
  const folderIsPrefixOfCurrentSlug =
    simpleFolderPath === currentSlug.slice(0, simpleFolderPath.length)

  if (!isCollapsed || folderIsPrefixOfCurrentSlug) {
    folderOuter.classList.add("open")
  }

  for (const child of node.children) {
    const childNode = child.isFolder
      ? createFolderNode(currentSlug, child, opts)
      : createFileNode(currentSlug, child)
    ul.appendChild(childNode)
  }

  return li
}

// brain (P4.2): refresh the toggle button's visible state to match
// the current `showIngested` preference. Idempotent — safe to call
// multiple times. Updates label text, `aria-pressed`, and the
// `[data-active]` attribute that drives CSS state.
function refreshIngestedToggleState(button: HTMLButtonElement, showIngested: boolean): void {
  button.textContent = showIngested ? SHOW_INGESTED_LABELS.hide : SHOW_INGESTED_LABELS.show
  button.setAttribute("aria-pressed", showIngested ? "true" : "false")
  button.dataset["active"] = showIngested ? "true" : "false"
}

// brain (P4.2): build the trie + render the explorer tree DOM. Split
// off the original `setupExplorer` body so we can call it twice — once
// at SPA-nav time (initial render) and once on toggle click (re-render
// without re-attaching listeners). All folder-button / folder-icon
// click handlers are wired ON every call because the new tree DOM is a
// fresh set of elements; the toggle button's own click handler stays
// wired across re-renders since it lives outside the explorer-ul.
function renderExplorerTree(
  explorer: HTMLElement,
  opts: ParsedOptions,
  currentSlug: FullSlug,
  data: Record<FullSlug, ContentDetails>,
): void {
  // Get folder state from local storage. Carries the user's per-
  // folder collapse preferences across renders so toggling the
  // ingested chip doesn't reset every other folder's open state.
  const storageTree = localStorage.getItem("fileTree")
  const serializedExplorerState = storageTree && opts.useSavedState ? JSON.parse(storageTree) : []
  const oldIndex = new Map<string, boolean>(
    serializedExplorerState.map((entry: FolderState) => [entry.path, entry.collapsed]),
  )

  const entries = [...Object.entries(data)] as [FullSlug, ContentDetails][]
  const trie = FileTrieNode.fromEntries(entries)

  // Apply functions in order
  for (const fn of opts.order) {
    switch (fn) {
      case "filter":
        if (opts.filterFn) trie.filter(opts.filterFn)
        break
      case "map":
        if (opts.mapFn) trie.map(opts.mapFn)
        break
      case "sort":
        if (opts.sortFn) trie.sort(opts.sortFn)
        break
    }
  }

  // brain (P4.2): conditional `_ingested/` filter pass. Runs AFTER
  // upstream's user filter so a vault that already excludes
  // `_ingested/` via a custom filterFn doesn't see this filter as a
  // no-op error — the trie is just unaffected. The countFiles helper
  // operates on the post-filter trie, so the `.folder-count` badges
  // automatically exclude ingested descendants when the toggle is OFF.
  if (!loadShowIngested()) {
    trie.filter((node) => node.slugSegment !== INGESTED_FOLDER_SEGMENT)
  }

  // Get folder paths for state management
  const folderPaths = trie.getFolderPaths()
  currentExplorerState = folderPaths.map((path) => {
    const previousState = oldIndex.get(path)
    return {
      path,
      collapsed:
        previousState === undefined ? opts.folderDefaultState === "collapsed" : previousState,
    }
  })

  const explorerUl = explorer.querySelector(".explorer-ul")
  if (!explorerUl) return

  // brain (P4.2): clear stale tree nodes from a previous render before
  // appending the new ones. Required for the toggle re-render path —
  // otherwise the second pass would double the visible tree. We
  // preserve the `.overflow-end` sentinel `<li>` because the
  // OverflowList script relies on it for the gradient-edge intersection
  // observer (see `OverflowList.tsx`'s `overflowListAfterDOMLoaded`).
  for (const child of Array.from(explorerUl.children)) {
    if (!(child instanceof HTMLElement) || !child.classList.contains("overflow-end")) {
      child.remove()
    }
  }

  // Create and insert new content
  const fragment = document.createDocumentFragment()
  for (const child of trie.children) {
    const node = child.isFolder
      ? createFolderNode(currentSlug, child, opts)
      : createFileNode(currentSlug, child)

    fragment.appendChild(node)
  }
  explorerUl.insertBefore(fragment, explorerUl.firstChild)

  // restore explorer scrollTop position if it exists
  const scrollTop = sessionStorage.getItem("explorerScrollTop")
  if (scrollTop) {
    ;(explorerUl as HTMLElement).scrollTop = parseInt(scrollTop)
  } else {
    // try to scroll to the active element if it exists
    const activeElement = explorerUl.querySelector(".active")
    if (activeElement) {
      activeElement.scrollIntoView({ behavior: "smooth" })
    }
  }

  // Set up folder click handlers — re-attached on every render
  // because the folder DOM is freshly built each time.
  if (opts.folderClickBehavior === "collapse") {
    const folderButtons = explorer.getElementsByClassName(
      "folder-button",
    ) as HTMLCollectionOf<HTMLElement>
    for (const button of folderButtons) {
      button.addEventListener("click", toggleFolder)
      window.addCleanup(() => button.removeEventListener("click", toggleFolder))
    }
  }

  const folderIcons = explorer.getElementsByClassName(
    "folder-icon",
  ) as HTMLCollectionOf<HTMLElement>
  for (const icon of folderIcons) {
    icon.addEventListener("click", toggleFolder)
    window.addCleanup(() => icon.removeEventListener("click", toggleFolder))
  }
}

// brain (P4.2): inject the "Show ingested" toggle button into the
// explorer DOM. Idempotent — if the button already exists (from a
// prior SPA nav whose explorer DOM happens to be reused), refresh its
// visible state and skip re-injection. The button is inserted as the
// FIRST child of `.explorer-content` so it sits inside the
// collapsible region but above the explorer-ul.
function ensureIngestedToggle(
  explorer: HTMLElement,
  onClick: (button: HTMLButtonElement) => void,
): HTMLButtonElement | null {
  const content = explorer.querySelector(".explorer-content")
  if (!(content instanceof HTMLElement)) return null

  let button = explorer.querySelector(
    ".brain-explorer-ingested-toggle",
  ) as HTMLButtonElement | null
  if (button === null) {
    button = document.createElement("button")
    button.type = "button"
    button.className = "brain-explorer-ingested-toggle"
    // Insert at the top of the collapsible explorer body so the
    // toggle is visible whenever the explorer is open.
    content.insertBefore(button, content.firstChild)
  }

  refreshIngestedToggleState(button, loadShowIngested())

  // Wire the click handler on every call. `window.addCleanup` from
  // the previous nav has already torn down the prior listener; the
  // upstream pattern is to register the cleanup-and-rebind cycle on
  // each nav, and we follow it.
  const handler = (event: MouseEvent) => {
    // Don't bubble into the desktop-explorer collapse handler — that
    // handler closes the entire explorer when its title button is
    // clicked, which would defeat the toggle.
    event.stopPropagation()
    onClick(button as HTMLButtonElement)
  }
  button.addEventListener("click", handler)
  window.addCleanup(() => (button as HTMLButtonElement).removeEventListener("click", handler))
  return button
}

async function setupExplorer(currentSlug: FullSlug) {
  const allExplorers = document.querySelectorAll("div.explorer") as NodeListOf<HTMLElement>

  for (const explorer of allExplorers) {
    const dataFns = JSON.parse(explorer.dataset.dataFns || "{}")
    const opts: ParsedOptions = {
      folderClickBehavior: (explorer.dataset.behavior || "collapse") as "collapse" | "link",
      folderDefaultState: (explorer.dataset.collapsed || "collapsed") as "collapsed" | "open",
      useSavedState: explorer.dataset.savestate === "true",
      order: dataFns.order || ["filter", "map", "sort"],
      sortFn: new Function("return " + (dataFns.sortFn || "undefined"))(),
      filterFn: new Function("return " + (dataFns.filterFn || "undefined"))(),
      mapFn: new Function("return " + (dataFns.mapFn || "undefined"))(),
    }

    const data = (await fetchData) as Record<FullSlug, ContentDetails>

    // Initial render with current `showIngested` preference applied.
    renderExplorerTree(explorer, opts, currentSlug, data)

    // brain (P4.2): inject the toggle button + wire its click. The
    // handler flips the persisted preference and triggers a re-render.
    ensureIngestedToggle(explorer, (button) => {
      const next = !loadShowIngested()
      persistShowIngested(next)
      refreshIngestedToggleState(button, next)
      renderExplorerTree(explorer, opts, currentSlug, data)
    })

    // Set up explorer collapse handlers (top-level, mobile + desktop
    // toggle buttons). Wired ONCE per nav since these DOM elements
    // are SSR'd and persist across renders.
    const explorerButtons = explorer.getElementsByClassName(
      "explorer-toggle",
    ) as HTMLCollectionOf<HTMLElement>
    for (const button of explorerButtons) {
      button.addEventListener("click", toggleExplorer)
      window.addCleanup(() => button.removeEventListener("click", toggleExplorer))
    }
  }
}

document.addEventListener("prenav", async () => {
  // save explorer scrollTop position
  const explorer = document.querySelector(".explorer-ul")
  if (!explorer) return
  sessionStorage.setItem("explorerScrollTop", explorer.scrollTop.toString())
})

document.addEventListener("nav", async (e: CustomEventMap["nav"]) => {
  const currentSlug = e.detail.url
  await setupExplorer(currentSlug)

  // if mobile hamburger is visible, collapse by default
  for (const explorer of document.getElementsByClassName("explorer")) {
    const mobileExplorer = explorer.querySelector(".mobile-explorer")
    if (!mobileExplorer) return

    if (mobileExplorer.checkVisibility()) {
      explorer.classList.add("collapsed")
      explorer.setAttribute("aria-expanded", "false")

      // Allow <html> to be scrollable when mobile explorer is collapsed
      document.documentElement.classList.remove("mobile-no-scroll")
    }

    mobileExplorer.classList.remove("hide-until-loaded")
  }
})

window.addEventListener("resize", function () {
  // Desktop explorer opens by default, and it stays open when the window is resized
  // to mobile screen size. Applies `no-scroll` to <html> in this edge case.
  const explorer = document.querySelector(".explorer")
  if (explorer && !explorer.classList.contains("collapsed")) {
    document.documentElement.classList.add("mobile-no-scroll")
    return
  }
})

function setFolderState(folderElement: HTMLElement, collapsed: boolean) {
  return collapsed ? folderElement.classList.remove("open") : folderElement.classList.add("open")
}
