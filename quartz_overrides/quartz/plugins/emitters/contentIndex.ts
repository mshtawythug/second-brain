// Sample Quartz v4 content-index emitter for brain vaults.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/emitters/contentIndex.ts` by
// `brain vault render --overlay`. As part of the same overlay step,
// the stock Quartz emitter shipped at
// `<vault>/.quartz/quartz/plugins/emitters/contentIndex.tsx` is
// renamed to `_upstreamContentIndex.tsx` (idempotent — a no-op on
// repeat runs). That rename is what lets the `./_upstreamContentIndex`
// import below resolve; without it the wrapper has nothing to wrap.
// If you are copying this file into a Quartz workspace by hand
// instead of going through `brain vault render`, do the rename
// first.
//
// It does NOT compile or run from the brain repo itself; the imports
// below resolve against the dependencies Quartz pulls into the
// cloned workspace via `npm install`, not against any package brain
// ships.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz
// version restructures the `ContentIndex` plugin, the on-disk JSON
// artifact path, or the `ContentDetails` shape, pull the latest
// emitter from
// https://github.com/jackyzha0/quartz/blob/v4/quartz/plugins/emitters/contentIndex.tsx
// and re-apply the brain tweaks flagged below — `// brain:` for
// value/structural choices on upstream-supported fields, and
// `// brain-extension:` for keys/types that don't exist in stock
// Quartz.
//
// What this wrapper does, and why: stock Quartz emits a
// `static/contentIndex.json` artifact keyed by slug, where each
// entry carries `{slug, filePath, title, links, tags, content}`. The
// brain graph renderer needs two extra signals — a `tier` and
// `source` facet from the doc's frontmatter (used for color
// clustering), and a structured per-link record carrying
// `kind: "wiki" | "derived"` plus optional `rule` / `weight`
// metadata for derived edges (used for dashed-edge styling and
// hover tooltips). Rather than duplicate upstream's emit body, this
// file calls `UpstreamContentIndex(opts)`, lets it write the JSON
// to disk, then reads the artifact back and grafts the
// brain-extension fields onto each entry before rewriting it. Tiny
// diff, survives upstream churn around sitemap/RSS handling.

import * as fs from "node:fs/promises"
import * as path from "node:path"

import type { Root, Element } from "hast"
import type { VFile } from "vfile"

import { ContentIndex as UpstreamContentIndex } from "./_upstreamContentIndex"
import { SimpleSlug } from "../../util/path"
import { QuartzEmitterPlugin } from "../types"

// brain: defensive guard for the case where this file was copied by
// hand without the matching upstream rename. With the overlay
// applied correctly, `UpstreamContentIndex` is a function. A bare
// undefined here means the rename step was skipped — surface that
// as a clear error at module load instead of letting the build die
// further down the call stack.
if (typeof UpstreamContentIndex !== "function") {
  throw new Error(
    "brain contentIndex wrapper expects upstream emitter at `./_upstreamContentIndex` (the overlay-renamed stock `contentIndex.tsx`); was the overlay rename step applied?",
  )
}

// brain-extension: structured per-link record. Stock Quartz emits a
// flat `SimpleSlug[]` for each doc; the custom graph renderer needs
// to know whether a link is an authored wiki-link or a Phase D
// derived-edge so it can apply the dashed/translucent styling and
// surface `rule` / `weight` in the hover tooltip.
export interface BrainLinkRecord {
  target: SimpleSlug
  kind: "wiki" | "derived"
  // Populated only for `kind: "derived"`. Mirrors the
  // `*(rule:... weight=...)*` suffix Phase D writes inside the
  // BRAIN_DERIVED fence in `_ingested/` bodies; the
  // `derivedFenceMark` transformer surfaces these as AST attributes
  // that `classifyLink` reads.
  rule?: string
  weight?: number
}

// brain-extension: shape of the augmented JSON entries written to
// `static/contentIndex.json`. Upstream Quartz fields pass through
// the index signature unchanged so this template tolerates minor
// upstream additions without code changes; the brain delta is the
// three explicit optionals.
export type BrainContentDetails = Record<string, unknown> & {
  tier?: string
  source?: string
  // brain-extension: ISO date string lifted from frontmatter
  // (`date` > `created` > `published` > `updated`). Consumed by
  // `Search.tsx` (P3.2) for the date column and `TagContent.tsx`
  // (P3.3) for the per-row date stamp. Optional — missing-frontmatter
  // docs leave the field unset and consumers render an empty column.
  date?: string
  linkRecords?: BrainLinkRecord[]
  // brain-extension: 240-char snippet of the body, populated by the
  // P3.1 slim transform. Search.tsx prefers this over `content` when
  // building the result-row preview; TagContent.tsx renders its own
  // tag-aware snippet from `description`, so this field is informative
  // for it (not authoritative).
  snippet?: string
}

// brain-extension: classification context handed to `classifyLink`.
// The current implementation ignores it (every link is "wiki"); the
// next iteration of this helper inspects `tree` / `file.data` to
// detect derived-fenced links.
export interface ClassifyContext {
  tree: Root
  file: VFile
}

// brain-extension: the single localized point where a link's
// classification is computed. The `derivedFenceMark` transformer
// (see `quartz_overrides/plugins/transformers/derivedFenceMark.ts`)
// stamps `data-brain-derived="true"` / `data-brain-rule` attributes
// onto `<a>` tags inside Phase D fences; this helper walks the
// post-rendered hast tree to find each link by its `href` (the
// slug-normalized target) and reads the attributes back. Keeping
// the rule in one function (and only one function) makes future
// format tweaks (e.g. a third "rule" kind, or adding `weight` once
// Phase E surfaces it) a localized change rather than a sweep
// through the wrapper.
//
// brain: fence membership is the truth-source for "this link is
// derived" — links whose `data-brain-rule` is absent (because the
// transformer's strict parser couldn't pin a rule down) still come
// back as `kind: "derived"`, just without the `rule` field. The
// `BrainLinkRecord` type's optional `weight` field is reserved for
// a future Phase E enhancement; nothing in the current pipeline
// produces it, so this classifier doesn't read it. When Phase E
// lands, extend the transformer and this classifier together.
//
// brain: lookup is "first <a> whose href matches the target slug".
// A doc that links the same partner twice (e.g. once authored,
// once via the Phase D fence) will resolve both records to the
// first match — but for the brain graph, the union shape is the
// same (one edge per (src, dst) pair), so the duplicate's
// classification doesn't actually change graph behavior. If a
// future renderer needs per-occurrence classification, switch to
// the index-based pairing approach (parallel walk of `details.links`
// and the hast tree's `<a>` elements in order).
export function classifyLink(target: SimpleSlug, ctx: ClassifyContext): BrainLinkRecord {
  const anchor = findAnchor(ctx.tree, target)
  if (anchor === null) {
    return { target, kind: "wiki" }
  }
  const props = (anchor.properties ?? {}) as Record<string, unknown>
  // brain-extension: hast property names are camelCased by hast-util-
  // from-html (``data-brain-derived`` → ``dataBrainDerived``); we
  // read both shapes pending empirical verification of which form
  // Quartz's rehype pipeline actually emits in practice. The
  // overlay's parse-smoke test confirms the live shape against a
  // freshly-rendered fixture vault; the unused branch can be
  // dropped at that point. Until then the dual-read is correct
  // documentation, not redundancy.
  if (props["dataBrainDerived"] !== "true" && props["data-brain-derived"] !== "true") {
    return { target, kind: "wiki" }
  }
  const record: BrainLinkRecord = { target, kind: "derived" }
  const rule = props["dataBrainRule"] ?? props["data-brain-rule"]
  if (typeof rule === "string" && rule.length > 0) {
    record.rule = rule
  }
  return record
}

// brain: depth-first hast walker that returns the first `<a>` whose
// `href` matches the slug-normalized target. The brain emitter feeds
// us a `SimpleSlug` (Quartz's resolved-and-normalized internal slug
// type) that already corresponds to the value remark-rehype writes
// into the rendered link's `href`. Recursion stops at the first
// match — see the duplicate-link note on `classifyLink` above.
function findAnchor(tree: Root, target: SimpleSlug): Element | null {
  const wanted = String(target)
  const stack: (Root | Element)[] = [tree]
  while (stack.length > 0) {
    const node = stack.pop()
    if (node === undefined) break
    if ("children" in node && Array.isArray(node.children)) {
      for (const child of node.children) {
        if (child.type === "element") {
          if (child.tagName === "a") {
            const href = (child.properties ?? {})["href"]
            if (typeof href === "string" && hrefMatchesSlug(href, wanted)) {
              return child
            }
          }
          stack.push(child)
        }
      }
    }
  }
  return null
}

// brain: tolerant href↔slug match. Quartz rewrites internal links
// to relative URLs that may include leading `./` or `../` segments
// and a trailing fragment (`#section`); the `SimpleSlug` we receive
// from the emitter is the bare slug. Strip both ends before
// comparing so matches survive the relative-resolver pass.
function hrefMatchesSlug(href: string, slug: string): boolean {
  const fragmentless = href.split("#")[0]
  // Quartz resolves wiki-links to relative paths like
  // `../partner-stem` or `./partner-stem`; trim leading `./` or
  // `../` segments before comparing the tail.
  const trimmed = fragmentless.replace(/^(?:\.\.\/)+/, "").replace(/^\.\//, "")
  if (trimmed === slug) return true
  // Slug may not include the doc's own folder prefix; allow a
  // suffix match so `folder/partner-stem` still matches a
  // `partner-stem` slug. Anchored on `/` to avoid matching
  // `other-partner-stem`.
  return trimmed.endsWith(`/${slug}`)
}

// brain: thin compat helper for downstream Quartz code that only
// cares about the slug list. Stock consumers (sitemap, RSS, third-
// party plugins) read `doc.links` directly as `SimpleSlug[]` — that
// upstream shape is preserved on disk, and this helper exists for
// callers that prefer to start from the record array. Co-located
// with the extension types so future maintainers see the trade-off
// in one place.
export function linkSlugs(records: BrainLinkRecord[] | undefined): SimpleSlug[] {
  return (records ?? []).map((r) => r.target)
}

// brain: relative path of the upstream contentIndex.json artifact
// inside Quartz's output directory. Pinned as a constant so a
// future upstream reshuffle (e.g. moving the file out of `static/`)
// is a single-line fix rather than a hunt through this file.
const CONTENT_INDEX_RELPATH = path.join("static", "contentIndex.json")

// brain-extension: relative directory where the slim post-processor
// writes one `<slug>.json` body file per surviving entry. The Search
// component (P3.2) lazy-fetches `static/contentBodies/<slug>.json`
// when a result is selected for the preview pane; the slim
// `contentIndex.json` only carries a 240-char snippet. Splitting body
// out cuts the index from ~19 MB → well under 2 MB gzipped, which is
// the budget enforced by `scripts/check_index_size.py`.
const CONTENT_BODIES_RELDIR = path.join("static", "contentBodies")

// brain-extension: snippet character budget. The slim
// `contentIndex.json` keeps a `snippet` (and rewrites `details.content`
// to the same 240-char prefix as a backwards-compat fallback) so the
// search popover can render result rows without round-tripping to the
// per-slug body file. 240 chars ≈ 2-3 lines of result preview, which
// matches the upstream Search component's render budget.
const SNIPPET_LENGTH = 240

// brain-extension (P3.6 fix-3): allowlist for slug values that are safe
// to interpolate into a filesystem path (`contentBodies/<slug>.json`)
// or a fetch URL. Slugs come from Quartz's trusted slugify, so practical
// exploitability is near-zero — but defense in depth: a future upstream
// change that loosened slug normalisation, or a malicious frontmatter
// `permalink` override that bypasses slugify, would otherwise leak path
// traversal into both the emitter's `fs.writeFile` and the inline
// script's `fetch`. The character allowlist alone permits `..` (both
// `.` and the path separator are individually safe), so `isSafeSlug`
// also rejects any `..` segment to close the path-traversal loophole.
// The same regex + helper are duplicated in
// `quartz_overrides/quartz/components/scripts/search.inline.ts` (rather
// than imported) because the inline script is bundled separately and
// can't take a runtime import from this server-side emitter.
//
// brain: allowlist composition — each char was justified against the
// real live-vault corpus, NOT picked from RFC 3986 wholesale:
//   * a-zA-Z0-9   bulk of slug chars
//   * `_`         underscore prefix on `_ingested/`
//   * `.`         file extension separators (`README.md` style)
//   * `-`         the canonical word separator slugify emits
//   * `/`         path component separator
//   * `,`         e.g. `_ingested/gmail/Tue,-7-Apr-...` (subject dates)
//   * `:`         e.g. `_ingested/krisp/2026-05-02-krisp:au-auto`
// What's NOT in the list and why:
//   * `<>"'` `&`  HTML metachars — would let titles/snippets break out
//                 of the row markup
//   * ` `         spaces — every URL-safe slug encodes them as `-`
//   * `?#&=`      query/fragment delimiters — would corrupt the
//                 `fetch(url)` shape
//   * `\`         Windows path separator + escape sequence on POSIX
//   * `\x00..1F`  control chars + null bytes — header smuggling
//   * `;`         shell metachar (defense in depth on path joins)
// Adding a new char here should require a documented live-vault slug
// shape that needs it AND a check it can't enable path traversal or
// HTML/URL injection downstream.
export const SAFE_SLUG_RE = /^[a-zA-Z0-9._/,:-]+$/

// brain (P3.6 fix-3): char-allowlist plus segment-shape rejection. A
// bare `..` segment in a slug would let the joined path
// (`<output>/static/contentBodies/../etc/passwd.json`) escape the
// `contentBodies/` directory, even though every character in
// `../etc/passwd` is individually in the allowlist. Empty segments
// (leading slash, double slash) are also rejected — Quartz's slugify
// never produces them, and a leading slash on the URL side would
// resolve against the site root rather than the static dir.
export function isSafeSlug(slug: string): boolean {
  if (!SAFE_SLUG_RE.test(slug)) return false
  const segments = slug.split("/")
  for (const segment of segments) {
    if (segment === "" || segment === "..") return false
  }
  return true
}

// brain-extension (P3.6 fix-1): normalise a frontmatter date value to an
// ISO `YYYY-MM-DD` string. gray-matter / js-yaml parses bare YAML dates
// (`date: 2026-04-12`) into JS `Date` objects, so the previous
// `typeof X === "string"` check silently missed them. We accept both
// shapes — strings pass through verbatim (so an ISO datetime keeps its
// time/zone suffix); `Date` instances render as the date-only `YYYY-MM-
// DD` slice (the time-of-day component is meaningless when the source
// value was a date-only YAML literal). Invalid Date instances and other
// non-{string, Date} values return `undefined` so the lookup chain can
// fall through to the next candidate field.
export function liftDate(v: unknown): string | undefined {
  if (typeof v === "string" && v.length > 0) return v
  if (v instanceof Date && !Number.isNaN(v.getTime())) {
    return v.toISOString().slice(0, 10)
  }
  return undefined
}

type Opts = Parameters<typeof UpstreamContentIndex>[0]

export const ContentIndex: QuartzEmitterPlugin<Opts> = (opts) => {
  const upstream = UpstreamContentIndex(opts)

  // brain: drop `partialEmit` from the upstream pass-through. Quartz's
  // watch-mode incremental emit, when wired up upstream, would call
  // `partialEmit` directly and bypass our wrapped `emit` — meaning
  // the brain post-processor never runs and the JSON loses its
  // tier/source/linkRecords augmentation. Forcing watch mode through
  // `emit` keeps the augmentation consistent across full-build and
  // watch paths. The leading-underscore name signals "intentionally
  // discarded".
  const { partialEmit: _partialEmit, ...rest } = upstream

  return {
    ...rest,
    // brain: keep upstream's registered name so Quartz's plugin
    // lookup, watch handler, and any third-party code referring to
    // "ContentIndex" still resolve to this wrapper after we spread
    // upstream's other fields.
    name: "ContentIndex",
    async *emit(ctx, content) {
      // Pass every artifact upstream emits — sitemap.xml, the RSS
      // feed, and the contentIndex.json — through unaltered. The
      // post-processor below rewrites the JSON in place once the
      // upstream generator has finished.
      for await (const fp of upstream.emit(ctx, content)) {
        yield fp
      }

      // brain: post-processor. Read back the JSON upstream just
      // wrote, graft brain-extension fields onto each entry, and
      // rewrite. Operating on the on-disk artifact (rather than
      // reimplementing upstream's emit body) keeps the wrapper tiny
      // and immune to upstream churn around sitemap/RSS handling.
      // The whole sequence is wrapped in try/catch so any I/O or
      // parse failure surfaces as a brain-attributable error
      // (`ENOENT`, `SyntaxError`, etc. by themselves give the user
      // no clue the failure came from this wrapper).
      const targetPath = path.join(ctx.argv.output, CONTENT_INDEX_RELPATH)
      try {
        const raw = await fs.readFile(targetPath, "utf-8")
        const parsed = JSON.parse(raw) as Record<string, BrainContentDetails>

        // Pair each JSON entry with its source `[Root, VFile]` so
        // the augmentation has access to frontmatter (for
        // tier/source) and the rendered AST (for derived-edge
        // classification).
        const sourceBySlug = new Map<string, [Root, VFile]>()
        for (const [tree, file] of content) {
          const slug = file.data?.slug as string | undefined
          if (slug) {
            sourceBySlug.set(slug, [tree, file])
          }
        }

        for (const [slug, details] of Object.entries(parsed)) {
          const source = sourceBySlug.get(slug)
          const fm = (source?.[1].data?.frontmatter ?? {}) as Record<string, unknown>

          // brain-extension: draft / seed quarantine. When the source
          // file's frontmatter carries `draft: true` (set by `brain
          // mark-draft <id>` and mirrored via `vault.export`), drop the
          // entry from `contentIndex.json` entirely. The doc still
          // exists on disk and in the DB; this only hides it from the
          // wiki — Explorer tree, graph view, full-text search — by
          // removing the slug from the index. Minimum-blast-radius
          // filter: every consumer (Search component, Graph component,
          // Explorer) reads from `contentIndex.json`, so dropping the
          // entry quarantines the doc across the whole site without
          // touching individual components.
          if (fm.draft === true) {
            delete parsed[slug]
            continue
          }

          // brain-extension: surface the vault `tier`
          // (vault | ingested) and ingest `source`
          // (krisp / slack / gmail / manual) so the graph renderer
          // can color-cluster nodes without round-tripping through
          // the brain CLI. Both are optional; missing-frontmatter
          // docs simply skip the field rather than emit `undefined`.
          //
          // Brain vault frontmatter persists tier under `kind:`
          // (with values `vault` / `ingested`); we still accept a
          // legacy `tier:` key for any pre-2026-04-29 export that
          // wrote it under that name.
          if (typeof fm.kind === "string") {
            details.tier = fm.kind
          } else if (typeof fm.tier === "string") {
            details.tier = fm.tier
          }
          if (typeof fm.source === "string") {
            details.source = fm.source
          }

          // brain: backstop — infer tier + source from the slug when
          // frontmatter is missing them. Symmetric: the path-based
          // classifier in `brain.vault.sync` does the same — anything
          // under `_ingested/...` is ingested-tier, everything else is
          // vault-tier. This guarantees the graph's tier filter has a
          // populated value on every node so toggling `vault` /
          // `ingested` chips actually filters; without this, missing-
          // frontmatter docs silently survive every tier filter.
          if (!details.tier && typeof slug === "string") {
            details.tier = slug.startsWith("_ingested/") ? "ingested" : "vault"
          }
          if (!details.source && typeof slug === "string") {
            const match = slug.match(/^_ingested\/([^/]+)\//)
            if (match) {
              details.source = match[1]
            }
          }

          // brain-extension: surface the doc's date so the Search row
          // (P3.2) and tag-content row (P3.3) can render
          // `… · 2026-04-12 · …` without each consumer re-deriving the
          // date from `dates` / `frontmatter`. Lookup order mirrors
          // the brain frontmatter writer (`src/brain/vault/export.py:
          // _build_frontmatter`) which always writes `created` /
          // `updated` (ISO strings) and accepts a forward-looking
          // `date` / `published` for any future authoring tool that
          // wants to override the export-derived value. We pick the
          // most user-meaningful field first (`date` if explicitly
          // authored, then `created` as the canonical ingest time,
          // then `published` as a legacy alias) and fall back to
          // `updated` only as a last resort. Missing dates leave
          // `details.date` undefined — Search.tsx and TagContent
          // already handle that gracefully.
          //
          // brain (P3.6 fix-1): YAML `date: 2026-04-12` is parsed by
          // gray-matter / js-yaml's default schema as a JS `Date`
          // object, NOT a string. The original `typeof X === "string"`
          // checks silently dropped Date instances, leaving
          // `details.date` empty for every doc whose authored
          // frontmatter used the bare YAML date form. The `liftDate`
          // helper accepts both shapes (string or Date) and normalises
          // to an ISO `YYYY-MM-DD` string for consumers.
          const lifted =
            liftDate(fm.date) ??
            liftDate(fm.created) ??
            liftDate(fm.published) ??
            liftDate(fm.updated)
          if (lifted !== undefined) {
            details.date = lifted
          }

          // brain: kept upstream's `links: SimpleSlug[]` shape
          // unchanged; added `linkRecords` as a parallel field
          // carrying the kind/rule/weight metadata. The plan
          // originally proposed replacing `links` with records, but
          // additive is safer for cross-version compat with
          // downstream Quartz consumers (sitemap, RSS, third-party
          // plugins) that read `doc.links` directly as `string[]`.
          // `details.links` is typed `unknown` via the index
          // signature; runtime-narrow with Array.isArray rather than
          // a blind cast.
          const slugs: SimpleSlug[] = Array.isArray(details.links)
            ? (details.links as SimpleSlug[])
            : []
          const ctxClassify: ClassifyContext | undefined = source
            ? { tree: source[0], file: source[1] }
            : undefined
          details.linkRecords = slugs.map((s) =>
            ctxClassify ? classifyLink(s, ctxClassify) : { target: s, kind: "wiki" },
          )

          // brain-extension: slim transform. Capture the full body,
          // write it out to `static/contentBodies/<slug>.json` so the
          // Search component (P3.2) can lazy-fetch on selection, then
          // overwrite `details.content` with a snippet so the index
          // itself stays small. We keep `content` populated with the
          // snippet (rather than dropping the field) as the
          // backwards-compat fallback documented in the plan — any
          // consumer that hasn't been taught about lazy-fetching still
          // sees a usable preview, just truncated. `details.snippet` is
          // the canonical name for forward-looking consumers (P3.2's
          // Search.tsx will branch on `snippet ?? content`). Slugs may
          // contain `/` separators (e.g. `_ingested/gmail/<id>`); the
          // mkdir-recursive call ensures the nested directory exists
          // before each write.
          //
          // brain (P3.6 fix-3): defense-in-depth slug guard. Slugs that
          // fall outside `isSafeSlug` skip both the body-file write
          // and the slim transform — the entry stays in the index with
          // its full `content`, which the search popover already knows
          // how to render as a fallback. We log a warning so a
          // legitimate slug shape we forgot to whitelist surfaces as
          // build noise rather than silent data loss.
          if (!isSafeSlug(slug)) {
            console.warn(
              `brain contentIndex: skipping unsafe slug ${JSON.stringify(slug)} ` +
                `(failed isSafeSlug check) — body file not written, ` +
                `entry retained with full content`,
            )
            continue
          }
          const body = typeof details.content === "string" ? details.content : ""
          const snippet = body.slice(0, SNIPPET_LENGTH)
          const bodyTarget = path.join(
            ctx.argv.output,
            CONTENT_BODIES_RELDIR,
            `${slug}.json`,
          )
          await fs.mkdir(path.dirname(bodyTarget), { recursive: true })
          await fs.writeFile(bodyTarget, JSON.stringify({ slug, content: body }))
          details.content = snippet
          details.snippet = snippet
        }

        await fs.writeFile(targetPath, JSON.stringify(parsed))
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err)
        throw new Error(
          `brain contentIndex post-processor failed at ${targetPath}: ${message}`,
          { cause: err },
        )
      }
    },
  }
}
