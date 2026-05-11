// Brain wiki redesign — code-block copy-button transformer plugin (Lane C).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/transformers/codeCopy.ts`
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
// What this transformer does, and why: the Lane C redesign supersedes
// stock Quartz's `.clipboard-button` (wired by `Body.tsx`) with a
// brain-themed `.brain-code-copy` pill that matches the redesign
// palette and adds a small-caps language label. The injection logic
// lives in `quartz/static/codeCopy.js` (a tiny vanilla-JS script);
// this transformer's only job is to emit a `<script src=...>` tag in
// the page `<head>` so every page picks it up.
//
// brain: this transformer ALWAYS injects the script — it's part of
// the production redesign and must run whether the build is dev
// (`bin/brain-up`) or prod (`brain vault render`). The injected
// script is small (~50 lines, ~2 KB) and runs once per page load +
// once per SPA navigation (re-injects after the article DOM gets
// swapped by micromorph).
//
// brain: cross-references for the code-copy contract:
//   * `quartz_overrides/quartz/static/codeCopy.js` — the runtime
//     script this transformer points page `<head>` at.
//   * `quartz_overrides/quartz/styles/brain/_code.scss` — the SCSS
//     consumer that styles `.brain-code-copy` + the language label.
//     Also hides the stock `.clipboard-button` so the two don't
//     render side-by-side.
//
// Registration: this transformer has NO ordering requirement among
// the other transformers — it doesn't touch the mdast tree, only
// contributes a `<script>` tag via `externalResources()`. Place it
// anywhere in the `transformers: [...]` list; the brain
// `quartz.config.ts` template wires it next to `LinkSourceTag` so
// the brain script-only transformers stay co-located.

import { QuartzTransformerPlugin } from "../types"

// brain-extension: the path the page `<script src=...>` tag points
// at. Leading slash is intentional — Quartz emits pages at arbitrary
// slug depths (e.g. `/notes/foo/index.html`) and a relative
// `static/codeCopy.js` would break for any non-root page.
// Server-rooted `/static/codeCopy.js` resolves consistently
// regardless of which page the user landed on. This works because
// Caddy's `file_server` (and Quartz's stock `file_server` in dev)
// serves the build dir as the site root
// (`<vault>/.quartz/current/static/codeCopy.js`).
const SCRIPT_SRC = "/static/codeCopy.js"

// brain-extension: the transformer plugin itself. Empty options
// shape — the script path is part of the contract documented above
// and the script body is fixed at the static-asset level. Keeping
// the signature `(opts?: never)` documents that no configuration is
// expected; if a knob is genuinely needed later (e.g. a per-vault
// disable flag), widen the type at that point rather than guessing
// now.
export const CodeCopy: QuartzTransformerPlugin = (_opts?: never) => {
  return {
    name: "CodeCopy",
    externalResources() {
      return {
        js: [
          {
            // brain: `afterDOMReady` so the script runs after
            // `</body>` (see Quartz's `renderPage.tsx` for the
            // dispatch). The injector reads + writes DOM and must
            // see the article element; afterDOMReady guarantees that.
            loadTime: "afterDOMReady",
            // brain-extension: external script (URL src) rather than
            // inline. The codeCopy.js file is bundled into every
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
