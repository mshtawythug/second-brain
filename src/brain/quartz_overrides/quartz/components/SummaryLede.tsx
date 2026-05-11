// Brain summary-lede component — wave Q2-SUMMARY-WIKI.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/SummaryLede.tsx` by `brain vault
// render --overlay`. It does NOT compile or run from the brain repo
// itself; the imports below resolve against the dependencies Quartz
// pulls into the cloned workspace via `npm install`, not against any
// package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). The component shape
// mirrors stock Quartz components — see `Backlinks.tsx` (also a
// brain-overlayed component) for the canonical pattern. If a future
// Quartz version restructures `QuartzComponent` / `QuartzComponentProps`,
// pull the latest reference component from
// https://github.com/jackyzha0/quartz/blob/v4/quartz/components/
// and re-apply the brain tweaks below.
//
// Strategy — render `fileData.frontmatter.summary` as an inline TL;DR
// above the article body. The Q1-D ``OllamaEnricher`` writes
// ``documents.summary``; the Q2 vault export pipeline plumbs that
// value into ``summary:`` frontmatter on the mirror file. This
// component reads that frontmatter key and renders an `<aside>` block
// with a soft "AI summary" eyebrow.
//
// When the frontmatter doesn't carry a ``summary`` (vault-tier notes
// the enricher hasn't touched, or short docs below the
// ``BRAIN_ENRICH_MIN_TOKENS`` threshold), the component renders
// `null` — no empty aside, no visual leak.
//
// Pin: this component is registered in `quartz.layout.ts` under
// `defaultContentPageLayout.beforeBody`, immediately after
// `Component.TagList()` and before `Component.Breadcrumbs()`. That
// places the lede above the article body but below the title /
// content meta strip, which is the natural place for a TL;DR.
//
// Responsibility (CLAUDE.md rule 8): this file owns the Preact
// component wrapper. Visual rules live in
// `../styles/brain/_summary_lede.scss`. No inline script — the lede
// is a static SSR'd block with no interactive behavior.

import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
import { classNames } from "../util/lang"

const SummaryLede: QuartzComponent = ({ fileData, displayClass }: QuartzComponentProps) => {
  // brain: defensive type-narrowing — Quartz's `fileData.frontmatter`
  // is typed as `Record<string, unknown>` because authors can write
  // arbitrary YAML. Only a string `summary` is renderable; a list,
  // object, or null falls through to `null` (no aside).
  const raw = fileData.frontmatter?.summary
  if (typeof raw !== "string") {
    return null
  }
  const summary = raw.trim()
  if (summary.length === 0) {
    return null
  }

  return (
    <aside class={classNames(displayClass, "brain-summary-lede")} aria-label="AI summary">
      <span class="brain-summary-lede-eyebrow" aria-hidden="true">
        AI summary
      </span>
      <p class="brain-summary-lede-body">{summary}</p>
    </aside>
  )
}

// brain: no component-css here — visuals live in the global
// `_summary_lede.scss`, which is `@use`-imported via `custom.scss`. That
// ensures the styles ship with EVERY page in the build.

export default (() => SummaryLede) satisfies QuartzComponentConstructor
