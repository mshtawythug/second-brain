// Brain related-docs sidebar runtime — Phase 5.2 of the Wiki UX Overhaul.
//
// The server-side Python build writes per-page JSON files under
// `/static/related/<slug>.json`. This browser script resolves the file
// for the current Quartz slug, fetches it lazily, and renders a compact
// source-icon list in the right sidebar. It deliberately fails closed:
// missing JSON, invalid rows, and empty related sets hide the panel.

import { FullSlug, pathToRoot, resolveRelative } from "../../util/path"
import { inferSource, sourceIconFor } from "../../util/sourceIcons"

interface RelatedDoc {
  slug: string
  title: string
  score: number
  source: string
  snippet: string
}

const RELATED_DOCS_RELDIR = "static/related"
const ROOT_SELECTOR = ".brain-related-docs"
const LIST_SELECTOR = ".brain-related-docs-list"
const EMPTY_SELECTOR = ".brain-related-docs-empty"
const SAFE_SLUG_RE = /^[a-zA-Z0-9._/,:-]+$/

function isSafeSlug(slug: string): boolean {
  if (!SAFE_SLUG_RE.test(slug)) return false
  for (const segment of slug.split("/")) {
    if (segment === "" || segment === "..") return false
  }
  return true
}

function normalizeRow(row: unknown): RelatedDoc | null {
  if (typeof row !== "object" || row === null) return null
  const candidate = row as Partial<Record<keyof RelatedDoc, unknown>>
  if (typeof candidate.slug !== "string" || !isSafeSlug(candidate.slug)) return null
  if (typeof candidate.title !== "string" || candidate.title.length === 0) return null
  if (typeof candidate.source !== "string") return null
  const score = typeof candidate.score === "number" ? candidate.score : 0
  const snippet = typeof candidate.snippet === "string" ? candidate.snippet : ""
  return {
    slug: candidate.slug,
    title: candidate.title,
    score,
    source: candidate.source,
    snippet,
  }
}

function hidePanel(panel: HTMLElement, list: HTMLOListElement, empty: HTMLElement): void {
  list.innerHTML = ""
  empty.setAttribute("hidden", "")
  panel.setAttribute("hidden", "")
}

function panelStillMatchesSlug(panel: HTMLElement, slug: string): boolean {
  const currentSlug = panel.dataset["brainRelatedSlug"] ?? document.body.dataset["slug"]
  return panel.isConnected && currentSlug === slug
}

function renderRows(
  panel: HTMLElement,
  list: HTMLOListElement,
  empty: HTMLElement,
  currentSlug: FullSlug,
  rows: RelatedDoc[],
): void {
  list.innerHTML = ""

  if (rows.length === 0) {
    hidePanel(panel, list, empty)
    return
  }

  for (const row of rows) {
    const item = document.createElement("li")
    item.className = "brain-related-docs-item"

    const link = document.createElement("a")
    link.href = resolveRelative(currentSlug, row.slug as FullSlug)

    const source = inferSource(row.slug, row.source)
    const icon = document.createElement("span")
    icon.className = "brain-related-docs-source"
    icon.setAttribute("aria-hidden", "true")
    icon.dataset["source"] = source
    icon.textContent = sourceIconFor(source)
    link.appendChild(icon)

    const body = document.createElement("span")
    body.className = "brain-related-docs-body"

    const title = document.createElement("span")
    title.className = "brain-related-docs-title"
    title.textContent = row.title
    body.appendChild(title)

    if (row.snippet.length > 0) {
      const snippet = document.createElement("span")
      snippet.className = "brain-related-docs-snippet"
      snippet.textContent = row.snippet
      body.appendChild(snippet)
    }

    link.appendChild(body)
    item.appendChild(link)
    list.appendChild(item)
  }

  empty.setAttribute("hidden", "")
  panel.removeAttribute("hidden")
}

async function setupRelatedDocs(): Promise<void> {
  const panels = document.querySelectorAll<HTMLElement>(ROOT_SELECTOR)
  for (const panel of panels) {
    const list = panel.querySelector<HTMLOListElement>(LIST_SELECTOR)
    const empty = panel.querySelector<HTMLElement>(EMPTY_SELECTOR)
    const rawSlug = panel.dataset["brainRelatedSlug"] ?? document.body.dataset["slug"]
    if (list === null || empty === null || typeof rawSlug !== "string" || !isSafeSlug(rawSlug)) {
      continue
    }

    const currentSlug = rawSlug as FullSlug
    const root = pathToRoot(currentSlug)
    const url = `${root}/${RELATED_DOCS_RELDIR}/${rawSlug}.json`

    try {
      const res = await fetch(url)
      if (!panelStillMatchesSlug(panel, rawSlug)) return
      if (!res.ok) {
        hidePanel(panel, list, empty)
        continue
      }
      const payload = await res.json()
      if (!panelStillMatchesSlug(panel, rawSlug)) return
      if (!Array.isArray(payload)) {
        hidePanel(panel, list, empty)
        continue
      }
      const rows = payload.map(normalizeRow).filter((row): row is RelatedDoc => row !== null)
      if (!panelStillMatchesSlug(panel, rawSlug)) return
      renderRows(panel, list, empty, currentSlug, rows)
    } catch {
      if (panelStillMatchesSlug(panel, rawSlug)) {
        hidePanel(panel, list, empty)
      }
    }
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    void setupRelatedDocs()
  })
} else {
  void setupRelatedDocs()
}

document.addEventListener("nav", () => {
  void setupRelatedDocs()
})
