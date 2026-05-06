// Brain related-docs sidebar component — Phase 5.2 of the Wiki UX Overhaul.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/RelatedDocs.tsx` by `brain vault
// render --overlay`. It does NOT hydrate from the brain repo itself;
// dynamic behavior lives in `scripts/relatedDocs.inline.ts` and is
// attached through `RelatedDocs.afterDOMLoaded`.
//
// The backend precomputes `/static/related/<slug>.json` during the
// atomic wiki build. This component renders the stable sidebar hooks;
// the inline script lazy-fetches the JSON for the current page and
// fills the list if related notes exist.

import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
// @ts-ignore — esbuild-loader rewrites this to a bundled string.
import script from "./scripts/relatedDocs.inline"
import { classNames } from "../util/lang"

const RelatedDocs: QuartzComponent = ({ displayClass, fileData }: QuartzComponentProps) => {
  return (
    <section
      class={classNames(displayClass, "brain-related-docs")}
      data-brain-related-slug={fileData.slug}
      aria-label="Related notes"
      hidden
    >
      <h3>Related</h3>
      <ol class="brain-related-docs-list"></ol>
      <p class="brain-related-docs-empty" hidden>
        No related notes.
      </p>
    </section>
  )
}

RelatedDocs.afterDOMLoaded = script

export default (() => RelatedDocs) satisfies QuartzComponentConstructor
