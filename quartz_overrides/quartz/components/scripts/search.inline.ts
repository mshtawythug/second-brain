// Brain Search runtime — Phase 3.2 of the Wiki UX Overhaul.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/scripts/search.inline.ts` by
// `brain vault render --overlay`. esbuild-loader inside the cloned
// Quartz workspace bundles it into a string that `Search.tsx` exports
// as `afterDOMLoaded`. The string is then injected at `</body>` time
// by Quartz's `renderPage.tsx`. The script does NOT compile or run
// from the brain repo itself.
//
// Brain customizations layered on top of stock Quartz's search
// runtime:
//
//   1. Source icon prefix on every result row — driven by
//      `details.source` (or, when missing, a fallback path-form
//      heuristic on the slug). Glyph table is read once at boot from
//      the `data-brain-source-icons` JSON attribute on the chip rail
//      so the component (`Search.tsx`) is the only place that hard-
//      codes the table.
//
//   2. Chip filter row (krisp / slack / gmail / manual / vault). Click
//      a chip to toggle its membership in the active set. The "All"
//      chip resets to the full vocabulary. Active set persists in
//      `localStorage` under `brain.search.activeSources` so the user's
//      preferred slice survives SPA navigation and reload.
//
//   3. Lazy preview pane — when a result is selected (hover or arrow
//      key), fetch `static/contentBodies/<slug>.json` (the per-slug
//      body file P3.1's contentIndex emitter writes) and render the
//      `content` field in the preview pane. On error (404, network
//      failure) we fall back to `details.snippet ?? details.content`
//      from the loaded index. Bodies are cached in a per-popover
//      `Map<slug, string>` so re-selecting the same row doesn't
//      refetch.
//
//   4. Date column on each row, formatted relative ("3d ago") for the
//      past week and ISO ("2026-04-12") otherwise.
//
// Snippet highlighting reuses the upstream `<mark>`-equivalent
// technique — wrap matched terms in a `<span class="highlight">` so
// the existing `_search.scss` accent rules kick in. We keep the same
// `highlight()` helper as upstream (with the trim-window logic) so the
// snippet column is dense + recognisable.

import FlexSearch, { DefaultDocumentSearchResults } from "flexsearch"
import { ContentDetails } from "../../plugins/emitters/contentIndex"
import { registerEscapeHandler, removeAllChildren } from "./util"
import { FullSlug, pathToRoot, resolveRelative } from "../../util/path"

interface Item {
  id: number
  slug: FullSlug
  title: string
  content: string
  tags: string[]
  source?: string
  tier?: string
  date?: number
  [key: string]: unknown
}

// brain: minimal subset of `BrainContentDetails` we read off the
// loaded `contentIndex.json`. Avoids importing the brain emitter type
// (which carries server-side hast types we don't need at runtime).
interface BrainEntry {
  title?: string
  content?: string
  snippet?: string
  tags?: string[]
  source?: string
  tier?: string
  date?: string | number
}

// brain: chip/persistence constants. Pinned at module scope so the
// active set survives `setupSearch` re-entry (per SPA `nav` event).
const ACTIVE_SOURCES_KEY = "brain.search.activeSources"
const CONTENT_BODIES_RELDIR = "static/contentBodies"
// Glyph fallback when the chip rail lacks a `data-brain-source-icons`
// attribute (defensive — the SSR path always sets it). Mirrors the
// `SOURCE_ICONS` table in `Search.tsx`.
const FALLBACK_SOURCE_ICONS: Record<string, string> = {
  gmail: "📧",
  krisp: "🎙️",
  slack: "💬",
  manual: "✍️",
  vault: "🌱",
}

// Can be expanded with things like "term" in the future
type SearchType = "basic" | "tags"
let searchType: SearchType = "basic"
let currentSearchTerm: string = ""

// brain: chip filter state at module scope so the user's selection
// survives SPA navigation. Default = full vocabulary (everything
// visible). On first load we hydrate from localStorage; subsequent
// chip clicks rewrite the same key.
const DEFAULT_ACTIVE_SOURCES = new Set<string>(Object.keys(FALLBACK_SOURCE_ICONS))
let activeSources: Set<string> = loadActiveSources()

function loadActiveSources(): Set<string> {
  if (typeof localStorage === "undefined") return new Set(DEFAULT_ACTIVE_SOURCES)
  try {
    const raw = localStorage.getItem(ACTIVE_SOURCES_KEY)
    if (raw === null) return new Set(DEFAULT_ACTIVE_SOURCES)
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set(DEFAULT_ACTIVE_SOURCES)
    // brain: filter parsed values down to the known vocabulary so a
    // stale localStorage from an older release (e.g. before a renamed
    // source) doesn't wedge filtering into a permanently-empty state.
    const filtered = parsed.filter((v) => typeof v === "string")
    return new Set<string>(filtered)
  } catch {
    return new Set(DEFAULT_ACTIVE_SOURCES)
  }
}

function persistActiveSources(): void {
  if (typeof localStorage === "undefined") return
  try {
    localStorage.setItem(ACTIVE_SOURCES_KEY, JSON.stringify([...activeSources]))
  } catch {
    // brain: localStorage may throw in private-mode Safari; swallow so
    // a quota exception doesn't tear down the search popover.
  }
}

const encoder = (str: string): string[] => {
  const tokens: string[] = []
  let bufferStart = -1
  let bufferEnd = -1
  const lower = str.toLowerCase()

  let i = 0
  for (const char of lower) {
    const code = char.codePointAt(0)!

    const isCJK =
      (code >= 0x3040 && code <= 0x309f) ||
      (code >= 0x30a0 && code <= 0x30ff) ||
      (code >= 0x4e00 && code <= 0x9fff) ||
      (code >= 0xac00 && code <= 0xd7af) ||
      (code >= 0x20000 && code <= 0x2a6df)

    const isWhitespace = code === 32 || code === 9 || code === 10 || code === 13

    if (isCJK) {
      if (bufferStart !== -1) {
        tokens.push(lower.slice(bufferStart, bufferEnd))
        bufferStart = -1
      }
      tokens.push(char)
    } else if (isWhitespace) {
      if (bufferStart !== -1) {
        tokens.push(lower.slice(bufferStart, bufferEnd))
        bufferStart = -1
      }
    } else {
      if (bufferStart === -1) bufferStart = i
      bufferEnd = i + char.length
    }

    i += char.length
  }

  if (bufferStart !== -1) {
    tokens.push(lower.slice(bufferStart))
  }

  return tokens
}

let index = new FlexSearch.Document<Item>({
  encode: encoder,
  document: {
    id: "id",
    tag: "tags",
    index: [
      {
        field: "title",
        tokenize: "forward",
      },
      {
        field: "content",
        tokenize: "forward",
      },
      {
        field: "tags",
        tokenize: "forward",
      },
    ],
  },
})

const p = new DOMParser()
const contextWindowWords = 30
const numSearchResults = 8
const numTagResults = 5

const tokenizeTerm = (term: string) => {
  const tokens = term.split(/\s+/).filter((t) => t.trim() !== "")
  const tokenLen = tokens.length
  if (tokenLen > 1) {
    for (let i = 1; i < tokenLen; i++) {
      tokens.push(tokens.slice(0, i + 1).join(" "))
    }
  }

  return tokens.sort((a, b) => b.length - a.length) // always highlight longest terms first
}

function highlight(searchTerm: string, text: string, trim?: boolean) {
  const tokenizedTerms = tokenizeTerm(searchTerm)
  let tokenizedText = text.split(/\s+/).filter((t) => t !== "")

  let startIndex = 0
  let endIndex = tokenizedText.length - 1
  if (trim) {
    const includesCheck = (tok: string) =>
      tokenizedTerms.some((term) => tok.toLowerCase().startsWith(term.toLowerCase()))
    const occurrencesIndices = tokenizedText.map(includesCheck)

    let bestSum = 0
    let bestIndex = 0
    for (let i = 0; i < Math.max(tokenizedText.length - contextWindowWords, 0); i++) {
      const window = occurrencesIndices.slice(i, i + contextWindowWords)
      const windowSum = window.reduce((total, cur) => total + (cur ? 1 : 0), 0)
      if (windowSum >= bestSum) {
        bestSum = windowSum
        bestIndex = i
      }
    }

    startIndex = Math.max(bestIndex - contextWindowWords, 0)
    endIndex = Math.min(startIndex + 2 * contextWindowWords, tokenizedText.length - 1)
    tokenizedText = tokenizedText.slice(startIndex, endIndex)
  }

  const slice = tokenizedText
    .map((tok) => {
      // see if this tok is prefixed by any search terms
      for (const searchTok of tokenizedTerms) {
        if (tok.toLowerCase().includes(searchTok.toLowerCase())) {
          const regex = new RegExp(searchTok.toLowerCase(), "gi")
          // brain: emit `<mark>` so the brain accent CSS in
          // `_search.scss` can paint per-result highlights without
          // colliding with the upstream `.highlight` class (which is
          // also reused by the preview pane's HTML highlighter).
          return tok.replace(regex, `<mark>$&</mark>`)
        }
      }
      return tok
    })
    .join(" ")

  return `${startIndex === 0 ? "" : "..."}${slice}${
    endIndex === tokenizedText.length - 1 ? "" : "..."
  }`
}

// brain: pull the source icon table out of the chip rail's data
// attribute. Falls back to the inline default when the attribute is
// missing (defensive — the SSR markup always sets it). Parsing
// failures fall back to the default rather than throwing so a stale
// rail from a previous build doesn't bork the search popover.
function readSourceIcons(rail: HTMLElement | null): Record<string, string> {
  if (rail === null) return { ...FALLBACK_SOURCE_ICONS }
  const attr = rail.dataset["brainSourceIcons"]
  if (typeof attr !== "string" || attr.length === 0) {
    return { ...FALLBACK_SOURCE_ICONS }
  }
  try {
    const parsed = JSON.parse(attr) as unknown
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { ...FALLBACK_SOURCE_ICONS }
    }
    const out: Record<string, string> = {}
    for (const [key, value] of Object.entries(parsed)) {
      if (typeof value === "string") out[key] = value
    }
    return Object.keys(out).length > 0 ? out : { ...FALLBACK_SOURCE_ICONS }
  } catch {
    return { ...FALLBACK_SOURCE_ICONS }
  }
}

// brain: fall back to a slug-based source classification when the
// emitter didn't graft a `source` field (e.g. legacy entries). Mirrors
// the path-form heuristic the contentIndex post-processor uses
// (`slug.startsWith("_ingested/")` ⇒ ingested-tier; the next segment
// is the source).
function inferSource(slug: string, source: string | undefined): string {
  if (typeof source === "string" && source.length > 0) return source
  const match = slug.match(/^_ingested\/([^/]+)\//)
  if (match) return match[1]
  return "vault"
}

function sourceIcon(icons: Record<string, string>, source: string): string {
  return icons[source] ?? icons["vault"] ?? FALLBACK_SOURCE_ICONS["vault"]
}

// brain: format a contentIndex date stamp for the right-aligned column
// on each row. ISO strings, numeric epoch millis, and `undefined` are
// all tolerated (matching upstream `Date | undefined` plus the JSON
// round-trip that turns Date into a string). Past 7 days renders as
// "Nd ago" / "today"; older renders as "YYYY-MM-DD".
function formatRelativeDate(raw: unknown): string {
  if (raw === null || raw === undefined) return ""
  let ts: number
  if (typeof raw === "number") {
    if (!Number.isFinite(raw)) return ""
    ts = raw
  } else if (typeof raw === "string") {
    const parsed = Date.parse(raw)
    if (Number.isNaN(parsed)) return ""
    ts = parsed
  } else {
    return ""
  }
  const now = Date.now()
  const diffMs = now - ts
  const day = 86_400_000
  if (diffMs < 0) return new Date(ts).toISOString().slice(0, 10)
  if (diffMs < day) return "today"
  if (diffMs < 7 * day) {
    const days = Math.floor(diffMs / day)
    return `${days}d ago`
  }
  return new Date(ts).toISOString().slice(0, 10)
}

function highlightHTML(searchTerm: string, el: HTMLElement) {
  const p = new DOMParser()
  const tokenizedTerms = tokenizeTerm(searchTerm)
  const html = p.parseFromString(el.innerHTML, "text/html")

  const createHighlightSpan = (text: string) => {
    const span = document.createElement("span")
    span.className = "highlight"
    span.textContent = text
    return span
  }

  const highlightTextNodes = (node: Node, term: string) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const nodeText = node.nodeValue ?? ""
      const regex = new RegExp(term.toLowerCase(), "gi")
      const matches = nodeText.match(regex)
      if (!matches || matches.length === 0) return
      const spanContainer = document.createElement("span")
      let lastIndex = 0
      for (const match of matches) {
        const matchIndex = nodeText.indexOf(match, lastIndex)
        spanContainer.appendChild(document.createTextNode(nodeText.slice(lastIndex, matchIndex)))
        spanContainer.appendChild(createHighlightSpan(match))
        lastIndex = matchIndex + match.length
      }
      spanContainer.appendChild(document.createTextNode(nodeText.slice(lastIndex)))
      node.parentNode?.replaceChild(spanContainer, node)
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      if ((node as HTMLElement).classList.contains("highlight")) return
      Array.from(node.childNodes).forEach((child) => highlightTextNodes(child, term))
    }
  }

  for (const term of tokenizedTerms) {
    highlightTextNodes(html.body, term)
  }

  return html.body
}

// brain: HTML-escape plain text so a result title containing `<` or
// `&` can't break out of the row markup. The upstream Search component
// builds rows with `innerHTML` so escaping is mandatory anywhere we
// embed user-supplied content (titles, snippets that bypass the
// `highlight()` mark-injection path).
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;")
}

async function setupSearch(searchElement: Element, currentSlug: FullSlug, data: ContentIndex) {
  const container = searchElement.querySelector(".search-container") as HTMLElement
  if (!container) return

  const sidebar = container.closest(".sidebar") as HTMLElement | null

  const searchButton = searchElement.querySelector(".search-button") as HTMLButtonElement
  if (!searchButton) return

  const searchBar = searchElement.querySelector(".search-bar") as HTMLInputElement
  if (!searchBar) return

  const searchLayout = searchElement.querySelector(".search-layout") as HTMLElement
  if (!searchLayout) return

  const chipsRail = searchElement.querySelector(".brain-search-chips") as HTMLElement | null
  const sourceIcons = readSourceIcons(chipsRail)

  // brain: per-popover lazy-fetch cache. Keyed by slug, value is the
  // resolved body string (or `null` for a known fetch failure so we
  // don't re-hammer a 404). Lives at function scope rather than module
  // scope because the cache should be reset between SPA nav events
  // (a fresh `setupSearch` call gets a fresh map).
  const fetchContentCache: Map<FullSlug, string | null> = new Map()

  const idDataMap = Object.keys(data) as FullSlug[]
  const appendLayout = (el: HTMLElement) => {
    searchLayout.appendChild(el)
  }

  const enablePreview = searchLayout.dataset.preview === "true"
  let preview: HTMLDivElement | undefined = undefined
  let previewInner: HTMLDivElement | undefined = undefined
  const results = document.createElement("div")
  results.className = "results-container"
  appendLayout(results)

  if (enablePreview) {
    preview = document.createElement("div")
    preview.className = "preview-container brain-search-preview"
    appendLayout(preview)
  }

  // brain: reflect the persisted active set onto the chip rail's
  // `data-active` attributes. The "All" pseudo-chip is active iff the
  // active set equals (or is a superset of) the full vocabulary —
  // following the same UX rule the graph chips use.
  function refreshChipState(): void {
    if (chipsRail === null) return
    const chips = chipsRail.querySelectorAll<HTMLButtonElement>(".brain-search-chip")
    const knownValues = chips.length === 0 ? [] : Array.from(chips)
      .map((b) => b.dataset["brainSource"])
      .filter((v): v is string => typeof v === "string" && v !== "__all__")
    const allActive = knownValues.length > 0 && knownValues.every((v) => activeSources.has(v))
    chips.forEach((chip) => {
      const value = chip.dataset["brainSource"]
      if (value === "__all__") {
        chip.dataset["active"] = allActive ? "true" : "false"
        return
      }
      if (typeof value !== "string") return
      chip.dataset["active"] = activeSources.has(value) ? "true" : "false"
    })
  }

  function bindChipHandlers(): void {
    if (chipsRail === null) return
    const chips = chipsRail.querySelectorAll<HTMLButtonElement>(".brain-search-chip")
    if (chips.length === 0) return
    const allValues = Array.from(chips)
      .map((b) => b.dataset["brainSource"])
      .filter((v): v is string => typeof v === "string" && v !== "__all__")
    chips.forEach((chip) => {
      const value = chip.dataset["brainSource"]
      const handler = () => {
        if (value === "__all__") {
          activeSources = new Set<string>(allValues)
        } else if (typeof value === "string") {
          if (activeSources.has(value)) {
            activeSources.delete(value)
          } else {
            activeSources.add(value)
          }
        }
        persistActiveSources()
        refreshChipState()
        // brain: re-run the current query through the filter so chip
        // toggles reflect immediately. `onType` reads `searchBar.value`,
        // so we just refire its handler with the existing input.
        void onType({ target: searchBar } as unknown as InputEvent)
      }
      chip.addEventListener("click", handler)
      window.addCleanup(() => chip.removeEventListener("click", handler))
    })
  }

  refreshChipState()
  bindChipHandlers()

  function hideSearch() {
    container.classList.remove("active")
    searchBar.value = "" // clear the input when we dismiss the search
    if (sidebar) sidebar.style.zIndex = ""
    removeAllChildren(results)
    if (preview) {
      removeAllChildren(preview)
    }
    searchLayout.classList.remove("display-results")
    searchType = "basic" // reset search type after closing
    searchButton.focus()
  }

  function showSearch(searchTypeNew: SearchType) {
    searchType = searchTypeNew
    if (sidebar) sidebar.style.zIndex = "1"
    container.classList.add("active")
    searchBar.focus()
  }

  let currentHover: HTMLInputElement | null = null
  async function shortcutHandler(e: HTMLElementEventMap["keydown"]) {
    if (e.key === "k" && (e.ctrlKey || e.metaKey) && !e.shiftKey) {
      e.preventDefault()
      const searchBarOpen = container.classList.contains("active")
      searchBarOpen ? hideSearch() : showSearch("basic")
      return
    } else if (e.shiftKey && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      // Hotkey to open tag search
      e.preventDefault()
      const searchBarOpen = container.classList.contains("active")
      searchBarOpen ? hideSearch() : showSearch("tags")

      // add "#" prefix for tag search
      searchBar.value = "#"
      return
    }

    if (currentHover) {
      currentHover.classList.remove("focus")
    }

    // If search is active, then we will render the first result and display accordingly
    if (!container.classList.contains("active")) return
    if (e.key === "Enter" && !e.isComposing) {
      // If result has focus, navigate to that one, otherwise pick first result
      if (results.contains(document.activeElement)) {
        const active = document.activeElement as HTMLInputElement
        if (active.classList.contains("no-match")) return
        await displayPreview(active)
        active.click()
      } else {
        const anchor = document.getElementsByClassName("result-card")[0] as HTMLInputElement | null
        if (!anchor || anchor.classList.contains("no-match")) return
        await displayPreview(anchor)
        anchor.click()
      }
    } else if (e.key === "ArrowUp" || (e.shiftKey && e.key === "Tab")) {
      e.preventDefault()
      if (results.contains(document.activeElement)) {
        const currentResult = currentHover
          ? currentHover
          : (document.activeElement as HTMLInputElement | null)
        const prevResult = currentResult?.previousElementSibling as HTMLInputElement | null
        currentResult?.classList.remove("focus")
        prevResult?.focus()
        if (prevResult) currentHover = prevResult
        await displayPreview(prevResult)
      }
    } else if (e.key === "ArrowDown" || e.key === "Tab") {
      e.preventDefault()
      if (document.activeElement === searchBar || currentHover !== null) {
        const firstResult = currentHover
          ? currentHover
          : (document.getElementsByClassName("result-card")[0] as HTMLInputElement | null)
        const secondResult = firstResult?.nextElementSibling as HTMLInputElement | null
        firstResult?.classList.remove("focus")
        secondResult?.focus()
        if (secondResult) currentHover = secondResult
        await displayPreview(secondResult)
      }
    }
  }

  const formatForDisplay = (term: string, id: number): Item => {
    const slug = idDataMap[id]
    const entry = data[slug] as BrainEntry & ContentDetails
    const rawSnippet =
      typeof entry.snippet === "string" && entry.snippet.length > 0
        ? entry.snippet
        : (entry.content ?? "")
    return {
      id,
      slug,
      title: searchType === "tags" ? entry.title ?? "" : highlight(term, entry.title ?? ""),
      content: highlight(term, rawSnippet, true),
      tags: highlightTags(term.substring(1), entry.tags ?? []) as unknown as string[],
      source: inferSource(slug as string, entry.source),
      tier: entry.tier,
      date:
        typeof entry.date === "number"
          ? entry.date
          : typeof entry.date === "string"
            ? Date.parse(entry.date) || undefined
            : undefined,
    }
  }

  function highlightTags(term: string, tags: string[]): string[] {
    if (!tags || searchType !== "tags") {
      return []
    }

    return tags
      .map((tag) => {
        if (tag.toLowerCase().includes(term.toLowerCase())) {
          return `<li><p class="match-tag">#${escapeHtml(tag)}</p></li>`
        } else {
          return `<li><p>#${escapeHtml(tag)}</p></li>`
        }
      })
      .slice(0, numTagResults)
  }

  function resolveUrl(slug: FullSlug): URL {
    return new URL(resolveRelative(currentSlug, slug), location.toString())
  }

  // brain: build the per-row markup. Source icon → title → date →
  // snippet, with the `<mark>` highlights from `highlight()` left
  // intact (we innerHTML-set so the spans render as elements rather
  // than literal text).
  const resultToHTML = (item: Item): HTMLAnchorElement => {
    const { slug, title, content, tags, source, date } = item
    const htmlTags =
      tags.length > 0 ? `<ul class="tags brain-search-tags">${tags.join("")}</ul>` : ``
    const itemTile = document.createElement("a")
    itemTile.classList.add("result-card", "brain-search-row")
    itemTile.id = slug
    itemTile.href = resolveUrl(slug).toString()
    const icon = sourceIcon(sourceIcons, source ?? "vault")
    const dateLabel = formatRelativeDate(date)
    itemTile.dataset["brainSource"] = source ?? "vault"
    itemTile.innerHTML = `
      <span class="brain-search-icon" aria-hidden="true">${escapeHtml(icon)}</span>
      <span class="brain-search-title">${title}</span>
      <span class="brain-search-date">${escapeHtml(dateLabel)}</span>
      ${htmlTags}
      <div class="brain-search-snippet">${content}</div>
    `
    itemTile.addEventListener("click", (event) => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
      hideSearch()
    })

    const handler = (event: MouseEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
      hideSearch()
    }

    async function onMouseEnter(ev: MouseEvent) {
      if (!ev.target) return
      const target = ev.target as HTMLInputElement
      await displayPreview(target)
    }

    itemTile.addEventListener("mouseenter", onMouseEnter)
    window.addCleanup(() => itemTile.removeEventListener("mouseenter", onMouseEnter))
    itemTile.addEventListener("click", handler)
    window.addCleanup(() => itemTile.removeEventListener("click", handler))

    return itemTile
  }

  async function displayResults(finalResults: Item[]) {
    removeAllChildren(results)
    if (finalResults.length === 0) {
      results.innerHTML = `<a class="result-card no-match">
          <h3>No results.</h3>
          <p>Try another search term?</p>
      </a>`
    } else {
      results.append(...finalResults.map(resultToHTML))
    }

    if (finalResults.length === 0 && preview) {
      // no results, clear previous preview
      removeAllChildren(preview)
    } else {
      // focus on first result, then also dispatch preview immediately
      const firstChild = results.firstElementChild as HTMLElement
      firstChild.classList.add("focus")
      currentHover = firstChild as HTMLInputElement
      await displayPreview(firstChild)
    }
  }

  // brain: lazy-fetch the per-slug body file P3.1 emitted under
  // `static/contentBodies/<slug>.json`. Resolved relative to the
  // current page via `pathToRoot(currentSlug)` + the static path so
  // the URL is correct for nested slugs (e.g. `_ingested/gmail/<id>`).
  // On any failure we cache `null` and fall back to the snippet from
  // the loaded contentIndex entry.
  async function fetchBody(slug: FullSlug): Promise<string | null> {
    if (fetchContentCache.has(slug)) {
      return fetchContentCache.get(slug) ?? null
    }
    const root = pathToRoot(currentSlug)
    const url = `${root}/${CONTENT_BODIES_RELDIR}/${slug}.json`
    try {
      const res = await fetch(url)
      if (!res.ok) {
        fetchContentCache.set(slug, null)
        return null
      }
      const payload = (await res.json()) as { content?: unknown }
      const body = typeof payload.content === "string" ? payload.content : null
      fetchContentCache.set(slug, body)
      return body
    } catch {
      fetchContentCache.set(slug, null)
      return null
    }
  }

  async function displayPreview(el: HTMLElement | null) {
    if (!searchLayout || !enablePreview || !el || !preview) return
    const slug = el.id as FullSlug
    const body = await fetchBody(slug)
    let html: string
    if (body !== null && body.length > 0) {
      // brain: render the lazy-fetched full body into the preview
      // pane. The body is a plain markdown-ish string (Quartz's
      // `details.content` is the HTML-stripped body), so we run the
      // same `highlight()` pass the snippet column uses to surface
      // search matches in context.
      const highlighted = highlight(currentSearchTerm, body, true)
      html = `<div class="preview-inner brain-search-preview-body">${highlighted}</div>`
    } else {
      // brain: fallback path — when the lazy fetch fails (404 in dev,
      // network error, truncated body), surface the snippet from the
      // already-loaded index. Same shape so the preview pane never
      // ends up empty just because the bodies dir wasn't deployed.
      const entry = data[slug] as BrainEntry & ContentDetails
      const snippet =
        typeof entry?.snippet === "string" && entry.snippet.length > 0
          ? entry.snippet
          : entry?.content ?? ""
      const highlighted = highlight(currentSearchTerm, snippet, true)
      html = `<div class="preview-inner brain-search-preview-fallback">${highlighted}</div>`
    }
    previewInner = document.createElement("div")
    previewInner.classList.add("preview-inner")
    previewInner.innerHTML = html
    preview.replaceChildren(previewInner)

    // scroll to longest highlight (mark or .highlight span)
    const marks = [...preview.querySelectorAll("mark, .highlight")].sort(
      (a, b) => (b as HTMLElement).innerHTML.length - (a as HTMLElement).innerHTML.length,
    )
    ;(marks[0] as HTMLElement | undefined)?.scrollIntoView({ block: "start" })
  }

  // brain: source filter. Returns true when the entry's source (or
  // path-form fallback) is in the active set. When the active set is
  // empty (every chip toggled off) we treat it as a wildcard so the
  // popover doesn't render permanently empty — same UX rule as the
  // graph chips. Note: the SSR rail boots with every chip active, so
  // the default state matches the wildcard semantics naturally.
  function passesChipFilter(slug: string, source: string | undefined): boolean {
    if (activeSources.size === 0) return true
    return activeSources.has(inferSource(slug, source))
  }

  async function onType(e: HTMLElementEventMap["input"]) {
    if (!searchLayout || !index) return
    currentSearchTerm = (e.target as HTMLInputElement).value
    searchLayout.classList.toggle("display-results", currentSearchTerm !== "")
    searchType = currentSearchTerm.startsWith("#") ? "tags" : "basic"

    let searchResults: DefaultDocumentSearchResults<Item>
    if (searchType === "tags") {
      currentSearchTerm = currentSearchTerm.substring(1).trim()
      const separatorIndex = currentSearchTerm.indexOf(" ")
      if (separatorIndex != -1) {
        const tag = currentSearchTerm.substring(0, separatorIndex)
        const query = currentSearchTerm.substring(separatorIndex + 1).trim()
        searchResults = await index.searchAsync({
          query: query,
          limit: Math.max(numSearchResults, 10000),
          index: ["title", "content"],
          tag: { tags: tag },
        })
        for (let searchResult of searchResults) {
          searchResult.result = searchResult.result.slice(0, numSearchResults)
        }
        searchType = "basic"
        currentSearchTerm = query
      } else {
        searchResults = await index.searchAsync({
          query: currentSearchTerm,
          limit: numSearchResults,
          index: ["tags"],
        })
      }
    } else {
      // brain: pull a wider window from flexsearch so the chip filter
      // has room to keep `numSearchResults` after dropping non-matching
      // sources. Without the wider window a strict chip selection
      // (e.g. only "krisp" active) could ship 0 results even when a
      // krisp doc matches the query — the slice above would have
      // already trimmed it before the filter runs.
      searchResults = await index.searchAsync({
        query: currentSearchTerm,
        limit: Math.max(numSearchResults * 5, 50),
        index: ["title", "content"],
      })
    }

    const getByField = (field: string): number[] => {
      const results = searchResults.filter((x) => x.field === field)
      return results.length === 0 ? [] : ([...results[0].result] as number[])
    }

    // order titles ahead of content
    const allIds: Set<number> = new Set([
      ...getByField("title"),
      ...getByField("content"),
      ...getByField("tags"),
    ])
    // brain: chip filter — drop ids whose entry's source isn't in the
    // active set, then cap at the upstream-equivalent display budget.
    const filteredIds: number[] = []
    for (const id of allIds) {
      const slug = idDataMap[id]
      const entry = data[slug] as BrainEntry & ContentDetails
      if (passesChipFilter(slug as string, entry?.source)) {
        filteredIds.push(id)
      }
      if (filteredIds.length >= numSearchResults) break
    }
    const finalResults = filteredIds.map((id) => formatForDisplay(currentSearchTerm, id))
    await displayResults(finalResults)
  }

  document.addEventListener("keydown", shortcutHandler)
  window.addCleanup(() => document.removeEventListener("keydown", shortcutHandler))
  searchButton.addEventListener("click", () => showSearch("basic"))
  window.addCleanup(() => searchButton.removeEventListener("click", () => showSearch("basic")))
  searchBar.addEventListener("input", onType)
  window.addCleanup(() => searchBar.removeEventListener("input", onType))

  registerEscapeHandler(container, hideSearch)
  await fillDocument(data)
}

/**
 * Fills flexsearch document with data.
 */
let indexPopulated = false
async function fillDocument(data: ContentIndex) {
  if (indexPopulated) return
  let id = 0
  const promises: Array<Promise<unknown>> = []
  for (const [slug, fileData] of Object.entries<ContentDetails>(data)) {
    const entry = fileData as BrainEntry & ContentDetails
    // brain: feed the index with the snippet (P3.1 contract) when
    // available, falling back to the legacy `content` field. Keeping
    // both branches lets the search component cope with a stale
    // `contentIndex.json` that pre-dates the slim transform.
    const indexable =
      typeof entry.snippet === "string" && entry.snippet.length > 0
        ? entry.snippet
        : entry.content ?? ""
    promises.push(
      index.addAsync(id++, {
        id,
        slug: slug as FullSlug,
        title: entry.title ?? "",
        content: indexable,
        tags: entry.tags ?? [],
      }),
    )
  }

  await Promise.all(promises)
  indexPopulated = true
}

// brain: hooked off Quartz's SPA `nav` event — fires on first page
// load + every SPA navigation. The fetch is shared via Quartz's
// inline `fetchData` global (see commandPalette.inline.ts L88-94 for
// the contract).
declare const fetchData: Promise<ContentIndex>

document.addEventListener("nav", async (e: CustomEventMap["nav"]) => {
  const currentSlug = e.detail.url
  const data = await fetchData
  const searchElement = document.getElementsByClassName("search")
  for (const element of searchElement) {
    await setupSearch(element, currentSlug, data)
  }
})
