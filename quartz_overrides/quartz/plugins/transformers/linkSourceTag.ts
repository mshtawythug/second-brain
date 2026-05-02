// Brain wiki redesign — link source-tagger transformer plugin (Lane B).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/transformers/linkSourceTag.ts`
// by `brain vault render --overlay`. It does NOT compile or run from
// the brain repo itself; the imports below resolve against the
// dependencies Quartz pulls into the cloned workspace via
// `npm install`, not against any package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz
// version restructures the transformer plugin shape or the
// `externalResources()` hook contract, pull the latest transformer
// reference from
// https://github.com/jackyzha0/quartz/tree/v4/quartz/plugins/transformers
// and re-apply the brain tweaks flagged below — `// brain:` for
// value/structural choices on upstream-supported fields, and
// `// brain-extension:` for keys/types that don't exist in stock
// Quartz.
//
// What this transformer does, and why: the companion `linkKindMark`
// transformer stamps `data-brain-link-kind="ingested"` on every `<a>`
// pointing into `_ingested/<source>/<slug>`, but it can't determine
// the source segment without a build-time document lookup that's
// expensive and brittle. We instead inject a tiny client script
// (`/static/linkSourceTag.js`, also installed by the overlay) that
// extracts the source from the href on `DOMContentLoaded` and stamps
// `data-brain-source="krisp"` (or slack/gmail/manual). `_links.scss`
// reads that attribute via `&[data-brain-source="krisp"]` rules to
// pick the source-tinted left-rail color from the `--brain-source-*`
// token palette.
//
// brain: this transformer ALWAYS injects the script — unlike
// `Plugin.ReloadSignal` which is gated on `BRAIN_WIKI_RELOAD=1`,
// source tagging is part of the production redesign and must run
// whether the build is dev (`bin/brain-up`) or prod (`brain vault
// render`). The injected script is tiny (~25 lines, ~1.2 KB) and
// runs once per page load + once per SPA navigation.
//
// brain: cross-references for the source-tagging contract:
//   * `quartz_overrides/quartz/static/linkSourceTag.js` — the runtime
//     tagger this transformer points page `<head>` at.
//   * `quartz_overrides/quartz/plugins/transformers/linkKindMark.ts`
//     — the build-time classifier whose `data-brain-link-kind="ingested"`
//     stamp this script complements with `data-brain-source="..."`.
//   * `quartz_overrides/quartz/styles/brain/_links.scss` — the SCSS
//     consumer that reads both attributes to render the source-tinted
//     left rail on ingested links.
//   * `quartz_overrides/quartz/styles/brain/_tokens.scss` — defines
//     the `--brain-source-{krisp,slack,gmail,manual}` palette both the
//     graph and link styles share.
//
// Registration: this transformer has NO ordering requirement among
// the other transformers — it doesn't touch the mdast tree, only
// contributes a `<script>` tag via `externalResources()`. Place it
// anywhere in the `transformers: [...]` list; the brain
// `quartz.config.ts` template wires it next to `LinkKindMark` so the
// Lane B plugins stay grouped.

import { QuartzTransformerPlugin } from "../types"

// brain-extension: the path the page `<script src=...>` tag points
// at. Leading slash is intentional — Quartz emits pages at arbitrary
// slug depths (e.g. `/notes/foo/index.html`) and a relative
// `static/linkSourceTag.js` would break for any non-root page.
// Server-rooted `/static/linkSourceTag.js` resolves consistently
// regardless of which page the user landed on. This works because
// Caddy's `file_server` (and Quartz's stock `file_server` in dev)
// serves the build dir as the site root
// (`<vault>/.quartz/current/static/linkSourceTag.js`).
const SCRIPT_SRC = "/static/linkSourceTag.js"

// brain-extension: the transformer plugin itself. Empty options
// shape — the script path is part of the contract documented above
// and the script body is fixed at the static-asset level. Keeping
// the signature `(opts?: never)` documents that no configuration is
// expected; if a knob is genuinely needed later (e.g. a per-vault
// source-allowlist override), widen the type at that point rather
// than guessing now.
export const LinkSourceTag: QuartzTransformerPlugin = (_opts?: never) => {
  return {
    name: "LinkSourceTag",
    externalResources() {
      return {
        js: [
          {
            // brain: `afterDOMReady` so the script runs after
            // `</body>` (see Quartz's `renderPage.tsx` for the
            // dispatch). The tagger reads + writes DOM attributes
            // and uses its own `DOMContentLoaded` listener as a
            // fallback for the loading-state edge case.
            loadTime: "afterDOMReady",
            // brain-extension: external script (URL src) rather than
            // inline. The linkSourceTag.js file is bundled into every
            // build's `static/` dir by Quartz's stock `Plugin.Static()`
            // emitter; pointing at it by URL keeps the script body
            // out of every page's HTML and lets the browser cache it.
            contentType: "external",
            src: SCRIPT_SRC,
          },
        ],
      }
    },
  }
}
