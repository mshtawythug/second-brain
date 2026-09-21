// Brain wiki redesign — link-kind classifier transformer plugin (Lane B).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/transformers/linkKindMark.ts`
// by `brain vault render --overlay`. It does NOT compile or run from
// the brain repo itself; the imports below resolve against the
// dependencies Quartz pulls into the cloned workspace via
// `npm install`, not against any package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz
// version restructures the transformer plugin shape or the mdast
// types it uses, pull the latest transformer reference from
// https://github.com/jackyzha0/quartz/tree/v4/quartz/plugins/transformers
// and re-apply the brain tweaks flagged below — `// brain:` for
// value/structural choices on upstream-supported fields, and
// `// brain-extension:` for keys/types that don't exist in stock
// Quartz.
//
// What this transformer does, and why: the 2026 redesign (Lane B)
// fixes the "every link looks identical" complaint by giving five
// distinct visual treatments to the five link kinds the brain wiki
// emits. The styling lives in
// `quartz_overrides/quartz/styles/brain/_links.scss` and keys off the
// `data-brain-link-kind` attribute this transformer stamps onto
// every `<a>` rendered from a markdown `link` mdast node.
//
// Classification, in priority order:
//
//   1. derived  — already stamped by `derivedFenceMark` with
//                 `data-brain-derived="true"`. We do NOT re-walk the
//                 fence; we just trust the upstream stamp and label
//                 the kind as `derived`. This keeps fence-membership
//                 truth-source ownership in `derivedFenceMark.ts` and
//                 prevents drift if the fence shape ever changes.
//   2. tag      — link URL starts with `tags/` (relative) or contains
//                 `/tags/` (absolute / slug-prefixed). Quartz's stock
//                 OFM transformer renders `#tag` syntax + `[[tags/x]]`
//                 wiki links into mdast `link` nodes whose `url`
//                 already carries the slug — so a string-prefix check
//                 is sufficient and cheaper than a slug normaliser.
//   3. external — URL starts with `http://`, `https://`, or
//                 `mailto:`. When the host config sets
//                 `openLinksInNewTab: true`, Quartz adds
//                 `target="_blank" rel="noopener"` via its CrawlLinks
//                 pass; the brain config leaves the default off.
//                 Either way, the classifier reads the URL directly
//                 so behavior is independent of that flag and of
//                 plugin ordering.
//   4. ingested — URL starts with `_ingested/` (vault-internal but
//                 pointing to mirrored ingested content from
//                 `brain ingest*`). Source-tinting (krisp/slack/gmail/
//                 manual) is applied at runtime by `linkSourceTag.js`
//                 because the source identity lives in the target
//                 doc's frontmatter, which a transformer can't easily
//                 look up at build time.
//   5. wiki     — fallback for everything else. Matches both Quartz's
//                 stock `internal` class (resolved wiki links) and
//                 anything that didn't fit the above buckets.
//
// brain: registration order matters. This transformer must run
// AFTER `Plugin.ObsidianFlavoredMarkdown()` (so `[[wiki-link]]` syntax
// has been converted into mdast `link` nodes) AND AFTER
// `Plugin.DerivedFenceMark()` (so `data-brain-derived="true"` is
// already stamped by the time we read it). The brain
// `quartz.config.ts` template wires it up in the correct slot — see
// the `transformers: [...]` list there.

import type { Root, RootContent, Link } from "mdast"

import { QuartzTransformerPlugin } from "../types"

// brain-extension: stable string set the SCSS in `_links.scss` keys
// off. Adding a new kind requires (a) extending this union, (b)
// extending the classifier, (c) adding a `[data-brain-link-kind="x"]`
// rule in `_links.scss`. All three live in the same Lane B PR.
type LinkKind = "derived" | "tag" | "external" | "ingested" | "wiki"

// brain-extension: the attribute the SCSS in `_links.scss` reads.
// Namespaced `data-brain-` prefix — same convention as
// `data-brain-derived` in `derivedFenceMark.ts` — so we never collide
// with a Quartz-internal `data-*` attribute.
const KIND_ATTR = "data-brain-link-kind"

// brain-extension: the upstream stamp from `derivedFenceMark.ts` that
// signals "this link lives inside a Phase D fence." Mirrored from
// that file's `stampDerived` function. If the fence stamp ever
// changes shape, update both files together.
const DERIVED_ATTR = "data-brain-derived"

// brain: external-link URL prefixes. `mailto:` is included because
// markdown autolinks (`<fixture@example.com>`) become mdast `link` nodes with
// `mailto:` URLs; the SCSS's `↗` indicator suits them. `tel:` is
// not included — current vault content has none, and adding it later
// is a one-line change.
const EXTERNAL_PREFIXES = ["http://", "https://", "mailto:"]

// brain: ingested-tier URL prefix. The vault layout puts every
// ingested doc under `_ingested/<source>/<slug>/`, and Quartz's
// CrawlLinks resolves wiki links to those targets with that exact
// prefix. Both the leading-slash form (`/_ingested/...`) and the
// relative form (`_ingested/...`) appear in real builds depending on
// link-resolution mode, so the classifier checks both.
const INGESTED_PREFIXES = ["_ingested/", "/_ingested/", "./_ingested/"]

// brain: tag-link URL patterns. Quartz emits tag links with URL
// `tags/<slug>` (relative, default `markdownLinkResolution: "shortest"`)
// or `/tags/<slug>` (absolute) depending on render context. A `tags/`
// prefix on a non-tag doc is unlikely (the slug `tags` is reserved by
// Quartz's TagPage emitter) so we don't over-constrain.
const TAG_PREFIXES = ["tags/", "/tags/", "./tags/"]

// brain: the INFIX form, and it is not redundant with the list above.
// A tag URL rendered host-qualified — `https://<host>/tags/<slug>`,
// which is what a site served under a real origin emits — carries the
// tag segment mid-string, so no prefix in TAG_PREFIXES can reach it.
// This constant is what makes the header's stated contract ("starts
// with `tags/` ... or CONTAINS `/tags/`") true of the code.
//
// FIXED 2026-08-20, and the code was the wrong half. Until this line
// existed the header two screens above documented the infix reading
// while `classifyLink` only ever prefix-matched, so
// `https://host/tags/retro` classified as `external`. The Python port
// `src/brain/ui/render.py` (`_TAG_INFIX`, `_is_tag_url`) had already
// taken the DOCUMENTED reading and pinned it with a test
// (`tests/test_ui_render_link_kinds.py`,
// `test_tag_wins_over_external_for_an_absolute_tag_url`), so the two
// implementations of one classifier disagreed. Two
// reasons the documented reading is the correct one, not merely the
// louder one: (1) a host-qualified link to this site's own tag page IS
// a tag link, and prefix-only silently mislabels it; (2) under
// prefix-only the tag/external precedence below is INERT — the two
// prefix sets are disjoint, so no reordering of those two lines can
// change any answer, and an ordering nothing can observe is an
// ordering no test can defend. The cost is the mirror case, a genuinely
// external `https://elsewhere/tags/x`, which is now labelled `tag`;
// that is the trade the header already declared with "we don't
// over-constrain".
const TAG_INFIX = "/tags/"

// brain: case-insensitive prefix match. URLs SHOULD be lowercase but
// hand-edited markdown can carry mixed-case `MailTo:` / `HTTPS://`,
// so we normalise the comparison to be defensive.
function startsWithAny(url: string, prefixes: readonly string[]): boolean {
  const lowered = url.toLowerCase()
  for (const prefix of prefixes) {
    if (lowered.startsWith(prefix.toLowerCase())) return true
  }
  return false
}

// brain: the tag test, as the header states it — prefix OR infix. Kept
// as its own function rather than folded into `classifyLink` so the
// contract has one name on both sides of the port: `render.py`'s
// `_is_tag_url` is this function.
function isTagUrl(url: string): boolean {
  if (startsWithAny(url, TAG_PREFIXES)) return true
  return url.toLowerCase().includes(TAG_INFIX)
}

// brain: classify a single mdast Link node into one of the five
// kinds. Returns a string from the `LinkKind` union. The classifier
// is pure (no mutation, no I/O) so it can be unit-tested in isolation
// if we ever add an overlay-side TS test runner.
//
// brain: classification order matters — `derived` wins over every
// other kind because a link inside a Phase D fence is conceptually
// "an evidence edge to a related doc," not "a wiki link to that
// doc." The italic-dashed styling in `_links.scss` reflects that
// distinction. After `derived`, `tag` MUST precede `external`: an
// absolute `https://<host>/tags/<slug>` satisfies both tests, and
// testing `tag` first is what keeps it a tag. (That precedence became
// load-bearing only when `TAG_INFIX` landed — see its note above for
// why it was inert, and therefore untestable, before.) `ingested` is a
// refinement of `wiki` (both are vault-internal) so the ingested check
// runs before the wiki fallback.
function classifyLink(link: Link): LinkKind {
  const data = link.data as Record<string, unknown> | undefined
  const props = data?.hProperties as Record<string, unknown> | undefined
  if (props !== undefined && props[DERIVED_ATTR] === "true") {
    return "derived"
  }
  const url = link.url ?? ""
  if (isTagUrl(url)) return "tag"
  if (startsWithAny(url, EXTERNAL_PREFIXES)) return "external"
  if (startsWithAny(url, INGESTED_PREFIXES)) return "ingested"
  return "wiki"
}

// brain: in-place attribute stamper. Same `data.hProperties` extension
// point used by `derivedFenceMark.stampDerived` — remark-rehype copies
// these onto the corresponding hast element's properties, which then
// become real HTML attributes after stringification. Idempotent: if
// the attribute is already set (e.g. a future plugin pre-stamps it),
// we don't overwrite — first writer wins, and the attribute's
// presence is what the SCSS keys off, not its origin.
function stampKind(link: Link, kind: LinkKind): void {
  const data = (link.data ??= {}) as Record<string, unknown>
  const props = (data.hProperties ??= {}) as Record<string, string>
  if (props[KIND_ATTR] !== undefined) return
  props[KIND_ATTR] = kind
}

// brain: top-level mdast walker. Recursively visits every node;
// stamps any `link` it encounters. Mirrors the `"children" in node`
// recursion pattern in `derivedFenceMark.annotateFence` so links
// nested inside lists, headings, blockquotes, and table cells are
// all reached. No fence-membership tracking is needed here because
// `derivedFenceMark` runs first and has already stamped
// `data-brain-derived` on every fence link by the time we walk.
function annotate(tree: Root): void {
  const visit = (node: RootContent | Root): void => {
    if (node.type === "link") {
      stampKind(node, classifyLink(node))
    }
    if ("children" in node && Array.isArray(node.children)) {
      for (const child of node.children as RootContent[]) {
        visit(child)
      }
    }
  }
  visit(tree)
}

// brain-extension: the transformer plugin itself. Empty options
// object — the only knobs would be the kind set or the URL prefix
// lists, both of which are part of the redesign contract documented
// at the top of this file. Keeping the signature `(opts?: never)`
// documents that no configuration is expected; if a knob is genuinely
// needed later (e.g. a per-vault tag prefix override), widen the type
// at that point rather than guessing now.
export const LinkKindMark: QuartzTransformerPlugin = (_opts?: never) => {
  return {
    name: "LinkKindMark",
    markdownPlugins() {
      return [
        () => {
          return (tree: Root) => {
            annotate(tree)
          }
        },
      ]
    },
  }
}
