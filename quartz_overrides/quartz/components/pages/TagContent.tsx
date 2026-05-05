// Brain TagContent — Phase 3.3 of the Wiki UX Overhaul.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/pages/TagContent.tsx` by `brain
// vault render --overlay`, OVERWRITING the stock Quartz page renderer
// for tag listing pages. It does NOT compile or run from the brain
// repo itself; imports are resolved by the cloned Quartz workspace.
//
// Why this override exists: stock Quartz's tag pages render via
// `<PageList>` which produces a flat list with date + title + tag
// pills. The brain corpus mixes ingested transcripts (krisp / slack /
// gmail) with authored notes (manual / vault), so a tag listing that
// doesn't surface the source is visually indistinguishable. P3.3
// adds:
//
//   * Source-icon prefix per row — same vocabulary as the search
//     popover (krisp 🎙️ / slack 💬 / gmail 📧 / manual ✍️ / vault 🌱).
//   * 1-line snippet around the first occurrence of the tag in the
//     doc's auto-generated description (falls back to the leading
//     200 chars when the tag word is not present in the description).
//   * Explicit `tagged: #<tag1> #<tag2>` footer enumerating all the
//     doc's tags — replaces the upstream tag-pill cluster with a
//     single denser footer line.
//
// Upstream's index-mode (the `tags === "/"` overview page listing all
// tags) is preserved verbatim — only the per-tag listing path is
// overridden. The brain customisation is the rendering of each doc
// row inside the `else` branch.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz
// version restructures the `TagContent` component (e.g. moves the
// per-tag rendering into its own file, or changes the
// `QuartzComponentProps` shape), pull the latest from
// https://github.com/jackyzha0/quartz/blob/v4/quartz/components/pages/TagContent.tsx
// and re-apply the brain delta marked with `// brain:` below.

import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "../types"
import style from "../styles/listPage.scss"
import { Date as QuartzDate, getDate } from "../Date"
import { PageList, SortFn, byDateAndAlphabeticalFolderFirst } from "../PageList"
import { FullSlug, getAllSegmentPrefixes, resolveRelative, simplifySlug } from "../../util/path"
import { QuartzPluginData } from "../../plugins/vfile"
import { Root } from "hast"
import { htmlToJsx } from "../../util/jsx"
import { i18n } from "../../i18n"
import { ComponentChildren } from "preact"
import { concatenateResources } from "../../util/resources"
import { inferSource, sourceIconFor } from "../../util/sourceIcons"

interface TagContentOptions {
  sort?: SortFn
  numPages: number
  // brain-extension: chars on either side of the first tag-occurrence
  // in the description when constructing the per-row snippet. Pinned
  // here so a future tweak (e.g. expanding to 240 chars to match the
  // P3.1 contentIndex snippet budget) is a single-line change.
  snippetWindow: number
}

const defaultOptions: TagContentOptions = {
  numPages: 10,
  snippetWindow: 100,
}

// brain: trim a single line of whitespace-collapsed text from ``description``
// centred on the first occurrence of ``tag``. When the tag word is not
// found we return the leading ``2 * window`` chars so the row still
// shows context. Whitespace is collapsed (multi-newline + tabs → single
// space) so the row stays a clean one-liner regardless of how the
// upstream description plugin built it.
//
// Match is case-insensitive on the tag's leaf segment (the part after
// the last `/` in nested tags like `interview/take-home`) — that's the
// word users actually expect to see highlighted in body text.
export function computeTagSnippet(
  description: string,
  tag: string,
  window: number,
): string {
  const collapsed = description.replace(/\s+/g, " ").trim()
  if (collapsed.length === 0) return ""
  const leaf = tag.includes("/") ? tag.slice(tag.lastIndexOf("/") + 1) : tag
  const idx = collapsed.toLowerCase().indexOf(leaf.toLowerCase())
  if (idx < 0) {
    return collapsed.slice(0, window * 2)
  }
  const start = Math.max(0, idx - window)
  const end = Math.min(collapsed.length, idx + leaf.length + window)
  const prefix = start > 0 ? "…" : ""
  const suffix = end < collapsed.length ? "…" : ""
  return `${prefix}${collapsed.slice(start, end)}${suffix}`
}

export default ((opts?: Partial<TagContentOptions>) => {
  const options: TagContentOptions = { ...defaultOptions, ...opts }

  const TagContent: QuartzComponent = (props: QuartzComponentProps) => {
    const { tree, fileData, allFiles, cfg } = props
    const slug = fileData.slug

    if (!(slug?.startsWith("tags/") || slug === "tags")) {
      throw new Error(`Component "TagContent" tried to render a non-tag page: ${slug}`)
    }

    const tag = simplifySlug(slug.slice("tags/".length) as FullSlug)
    const allPagesWithTag = (t: string) =>
      allFiles.filter((file) =>
        (file.frontmatter?.tags ?? []).flatMap(getAllSegmentPrefixes).includes(t),
      )

    const content = (
      (tree as Root).children.length === 0
        ? fileData.description
        : htmlToJsx(fileData.filePath!, tree)
    ) as ComponentChildren
    const cssClasses: string[] = fileData.frontmatter?.cssclasses ?? []
    const classes = cssClasses.join(" ")

    if (tag === "/") {
      // brain: index-mode (tags listing all tags) — preserved verbatim
      // from upstream. The brain delta is only applied to per-tag
      // pages where the row count is small enough to render the dense
      // icon + snippet + footer markup without overwhelming the
      // viewport. The aggregate index page lists every tag with a
      // PageList preview underneath, where the upstream rendering is
      // the right fit.
      const tags = [
        ...new Set(
          allFiles.flatMap((data) => data.frontmatter?.tags ?? []).flatMap(getAllSegmentPrefixes),
        ),
      ].sort((a, b) => a.localeCompare(b))
      const tagItemMap: Map<string, QuartzPluginData[]> = new Map()
      for (const t of tags) {
        tagItemMap.set(t, allPagesWithTag(t))
      }
      return (
        <div class="popover-hint">
          <article class={classes}>
            <p>{content}</p>
          </article>
          <p>{i18n(cfg.locale).pages.tagContent.totalTags({ count: tags.length })}</p>
          <div>
            {tags.map((t) => {
              const pages = tagItemMap.get(t)!
              const listProps = {
                ...props,
                allFiles: pages,
              }

              const contentPage = allFiles.filter((file) => file.slug === `tags/${t}`).at(0)

              const root = contentPage?.htmlAst
              const tagContent =
                !root || root?.children.length === 0
                  ? contentPage?.description
                  : htmlToJsx(contentPage.filePath!, root)

              const tagListingPage = `/tags/${t}` as FullSlug
              const href = resolveRelative(fileData.slug!, tagListingPage)

              return (
                <div>
                  <h2>
                    <a class="internal tag-link" href={href}>
                      {t}
                    </a>
                  </h2>
                  {tagContent && <p>{tagContent}</p>}
                  <div class="page-listing">
                    <p>
                      {i18n(cfg.locale).pages.tagContent.itemsUnderTag({ count: pages.length })}
                      {pages.length > options.numPages && (
                        <>
                          {" "}
                          <span>
                            {i18n(cfg.locale).pages.tagContent.showingFirst({
                              count: options.numPages,
                            })}
                          </span>
                        </>
                      )}
                    </p>
                    <PageList limit={options.numPages} {...listProps} sort={options?.sort} />
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )
    }

    // brain: per-tag mode — replace the upstream `<PageList>` with a
    // brain-styled row list. Sort uses the same default Quartz uses
    // (`byDateAndAlphabeticalFolderFirst`) so the relative ordering
    // matches user expectations (newest-first, undated trailing).
    const sorter = options.sort ?? byDateAndAlphabeticalFolderFirst(cfg)
    const pages = allPagesWithTag(tag).sort(sorter)

    return (
      <div class="popover-hint">
        <article class={classes}>{content}</article>
        <div class="page-listing">
          <p>{i18n(cfg.locale).pages.tagContent.itemsUnderTag({ count: pages.length })}</p>
          <ul class="section-ul brain-tag-results">
            {pages.map((page) => {
              const title = page.frontmatter?.title ?? page.slug ?? ""
              const tags = page.frontmatter?.tags ?? []
              const fmSource =
                typeof page.frontmatter?.source === "string"
                  ? (page.frontmatter.source as string)
                  : undefined
              const source = inferSource(page.slug ?? "", fmSource)
              const icon = sourceIconFor(source)
              const description = page.description ?? ""
              const snippet = computeTagSnippet(description, tag, options.snippetWindow)
              const dateObj = getDate(cfg, page)
              const href = resolveRelative(fileData.slug!, page.slug!)

              return (
                <li class="section-li brain-tag-row" data-brain-source={source}>
                  <a class="internal brain-tag-row-link" href={href}>
                    <span
                      class="brain-tag-icon"
                      aria-hidden="true"
                      data-brain-source={source}
                    >
                      {icon}
                    </span>
                    <span class="brain-tag-title">{title}</span>
                    {dateObj && (
                      <span class="brain-tag-date">
                        <QuartzDate date={dateObj} locale={cfg.locale} />
                      </span>
                    )}
                    {snippet && <span class="brain-tag-snippet">{snippet}</span>}
                  </a>
                  {tags.length > 0 && (
                    <p class="brain-tag-footer">
                      <span class="brain-tag-footer-label">tagged:</span>{" "}
                      {tags.map((rowTag, i) => (
                        // brain (P3.6 fix-6): bare `<>` fragments inside
                        // a `.map()` trip Preact's runtime warning
                        // ("Each child in a list should have a unique
                        // key prop"). Promote to a span with a stable
                        // `key={rowTag}` so the warning goes away and
                        // the reconciler can match nodes correctly when
                        // the tags list mutates between renders.
                        <span key={rowTag} class="brain-tag-footer-fragment">
                          {i > 0 ? " " : ""}
                          <a
                            class="internal tag-link brain-tag-footer-link"
                            href={resolveRelative(fileData.slug!, `tags/${rowTag}` as FullSlug)}
                          >
                            #{rowTag}
                          </a>
                        </span>
                      ))}
                    </p>
                  )}
                </li>
              )
            })}
          </ul>
        </div>
      </div>
    )
  }

  TagContent.css = concatenateResources(style, PageList.css)
  return TagContent
}) satisfies QuartzComponentConstructor
