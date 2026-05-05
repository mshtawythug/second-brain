// Brain wiki — transformer barrel re-export with brain extensions wired in.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/transformers/index.ts` by `brain
// vault render --overlay`, OVERWRITING the stock Quartz barrel. The
// overlay is a 1:1 file copy (`src/brain/vault/quartz_overlay.py`),
// so this is the canonical seam for ensuring the brain-extension
// transformer files in this directory get re-exported under the
// `Plugin.*` namespace consumed by `quartz.config.ts`.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz
// version adds a new stock transformer, append its export below; the
// ones that already exist are sourced verbatim from
// https://github.com/jackyzha0/quartz/blob/v4/quartz/plugins/transformers/index.ts
// and the brain extensions are appended at the bottom in their own
// block.
//
// brain: stock Quartz transformer exports — keep in lock-step with
// the upstream `index.ts` linked above. If you upgrade Quartz, diff
// upstream against this block and apply additions.
export { FrontMatter } from "./frontmatter"
export { GitHubFlavoredMarkdown } from "./gfm"
export { Citations } from "./citations"
export { CreatedModifiedDate } from "./lastmod"
export { Latex } from "./latex"
export { Description } from "./description"
export { CrawlLinks } from "./links"
export { ObsidianFlavoredMarkdown } from "./ofm"
export { OxHugoFlavouredMarkdown } from "./oxhugofm"
export { SyntaxHighlighting } from "./syntax"
export { TableOfContents } from "./toc"
export { HardLineBreaks } from "./linebreaks"
export { RoamFlavoredMarkdown } from "./roam"

// brain-extension: brain-only transformers added by the overlay. Each
// is documented in its own file's top-of-file comment.
//   * DerivedFenceMark — stamps Phase D fence links with
//     `data-brain-derived` so the contentIndex emitter and the Lane B
//     link classifier can recognise them as evidence edges.
//   * ReloadSignal     — injects the polling reload watcher when
//     `BRAIN_WIKI_RELOAD=1` (dev path); see the blue-green serve plan.
//   * LinkKindMark     — Lane B redesign — stamps every `<a>` with
//     `data-brain-link-kind` so the redesigned link styles can
//     distinguish wiki / external / tag / ingested / derived kinds.
//   * LinkSourceTag    — Lane B redesign — injects
//     `/static/linkSourceTag.js` to add `data-brain-source` to
//     ingested links at runtime (source identity is in the URL path
//     but extraction at build-time would require a doc lookup).
//   * CodeCopy         — Lane C redesign — injects
//     `/static/codeCopy.js` to add a brain-themed copy button to
//     every `<pre>` in the article (supersedes stock Quartz's
//     `.clipboard-button`, which `_code.scss` hides) and to lift
//     the `data-language` attribute from inner `<code>` to outer
//     `<pre>` so the CSS-only language label can render via
//     `attr()`.
export { DerivedFenceMark } from "./derivedFenceMark"
export { ReloadSignal } from "./reloadSignal"
export { LinkKindMark } from "./linkKindMark"
export { LinkSourceTag } from "./linkSourceTag"
export { CodeCopy } from "./codeCopy"
//   * EmptyDoorFilter   — P4.1 — strips home-page list-items whose
//     internal link resolves to an empty folder (e.g. `daily/` when
//     no daily notes exist). Pairs with the brain CLI's auto-generated
//     `<folder>/index.md` so the door appears iff the folder has
//     content.
export { EmptyDoorFilter } from "./emptyDoorFilter"
