// Brain wiki — live relative-date transformer plugin (Recent rail).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/transformers/relativeDate.ts`
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
// What this transformer does, and why: the home-page Recent rail
// (`brain.wiki.build_homepage`) used to bake a relative date string
// ("today", "3d ago") into `index.md` at build time. That string decays
// because the home note isn't re-rendered daily, so a doc ingested 3
// days before a build still reads "3d ago" weeks later. The rail now
// emits the ABSOLUTE date as a `data-date` attribute on a
// `<span class="brain-rel-date">` plus a non-decaying "Jun 10"-style
// fallback as the span's text. We inject a tiny client script
// (`/static/relativeDate.js`, also installed by the overlay) that reads
// each `data-date` and recomputes the relative text live on every page
// load (and on Quartz SPA `nav` events), keeping the rail honest.
//
// brain: this transformer ALWAYS injects the script — unlike
// `Plugin.ReloadSignal` which is gated on `BRAIN_WIKI_RELOAD=1`, the
// live relative date is part of the production behavior and must run
// whether the build is dev (`bin/brain-up`) or prod (`brain vault
// render`). The injected script is tiny (~30 lines) and runs once per
// page load + once per SPA navigation.
//
// brain: cross-references for the live-relative-date contract:
//   * `quartz_overrides/quartz/static/relativeDate.js` — the runtime
//     recomputer this transformer points page `<head>` at; its bucket
//     logic mirrors `build_homepage._format_relative_date`.
//   * `src/brain/wiki/build_homepage.py` — the server side that emits
//     `<span class="brain-rel-date" data-date="<ISO>">{absolute}</span>`
//     into the Recent fence (`_render_bullets` / `_format_absolute_date`).
//
// Registration: this transformer has NO ordering requirement among
// the other transformers — it doesn't touch the mdast tree, only
// contributes a `<script>` tag via `externalResources()`. Place it
// anywhere in the `transformers: [...]` list; the brain
// `quartz.config.ts` template wires it next to `LinkSourceTag` so the
// script-only brain transformers stay grouped.

import { QuartzTransformerPlugin } from "../types"

// brain-extension: the path the page `<script src=...>` tag points at.
// Leading slash is intentional — Quartz emits pages at arbitrary slug
// depths (e.g. `/notes/foo/index.html`) and a relative
// `static/relativeDate.js` would break for any non-root page.
// Server-rooted `/static/relativeDate.js` resolves consistently
// regardless of which page the user landed on. (In practice only the
// home page carries `.brain-rel-date` spans today, but injecting site-
// wide keeps the contract simple and future-proof if other surfaces
// adopt the span.) This works because Caddy's `file_server` (and
// Quartz's stock `file_server` in dev) serves the build dir as the site
// root (`<vault>/.quartz/current/static/relativeDate.js`).
const SCRIPT_SRC = "/static/relativeDate.js"

// brain-extension: the transformer plugin itself. Empty options shape —
// the script path is part of the contract documented above and the
// script body is fixed at the static-asset level. Keeping the signature
// `(opts?: never)` documents that no configuration is expected; if a
// knob is genuinely needed later, widen the type at that point rather
// than guessing now.
export const RelativeDate: QuartzTransformerPlugin = (_opts?: never) => {
  return {
    name: "RelativeDate",
    externalResources() {
      return {
        js: [
          {
            // brain: `afterDOMReady` so the script runs after `</body>`
            // (see Quartz's `renderPage.tsx` for the dispatch). The
            // recomputer reads + writes DOM text and uses its own
            // `DOMContentLoaded` listener as a fallback for the
            // loading-state edge case.
            loadTime: "afterDOMReady",
            // brain-extension: external script (URL src) rather than
            // inline. The relativeDate.js file is bundled into every
            // build's `static/` dir by Quartz's stock `Plugin.Static()`
            // emitter; pointing at it by URL keeps the script body out
            // of every page's HTML and lets the browser cache it.
            contentType: "external",
            src: SCRIPT_SRC,
          },
        ],
      }
    },
  }
}
